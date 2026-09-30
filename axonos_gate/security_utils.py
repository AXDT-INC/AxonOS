import fcntl
import hashlib
import hmac
import ipaddress
import os
import re
import stat
import struct
import time
import zlib
from typing import Optional, Set, Tuple
from urllib.parse import unquote, urlsplit


_TERMINAL_WS_LOG_QUERY_RE = re.compile(
    r"(/api/terminal/ws)\?[^\s'\"<>]*"
)
_QUERY_FIELD_LOG_RE = re.compile(
    r"([?&])([^=&\s'\"<>#]*)(=)([^&\s'\"<>#]*)",
    re.IGNORECASE,
)
_SENSITIVE_QUERY_NAMES = frozenset(
    {"auth_token", "invite", "twclid", "x_capi_context", "x_capi_handoff"}
)
_MAX_CANONICAL_LOG_QUERY_NAME = 256


def _canonical_log_query_name(value: str) -> str:
    """Decode a query name defensively before deciding whether it is secret.

    Browsers and HTTP frameworks percent-decode query names before application
    lookup.  Matching the raw request line therefore misses spellings such as
    ``twcl%69d``.  Decode repeatedly to make the logging boundary conservative
    even when an upstream layer has decoded a value zero or one times already.
    Invalid percent escapes are left unchanged by ``unquote``.
    """
    current = value
    # Every successful percent-decoding pass strictly shortens at least one
    # ``%HH`` sequence. Therefore input length is a hard termination bound and
    # no arbitrary nesting depth can bypass the log boundary.
    for _ in range(len(value) + 1):
        decoded = unquote(current, errors="replace")
        if decoded == current:
            break
        current = decoded
    return current.casefold()


def _log_query_name_is_sensitive(value: str) -> bool:
    """Recognize exact and malformed/extended spellings conservatively.

    ``urllib.parse.unquote`` deliberately leaves malformed escapes in place.
    A name such as ``%74wclid%ZZ`` therefore canonicalizes to
    ``twclid%zz`` rather than the exact string ``twclid``.  Treating a known
    sensitive name anywhere in the canonical field name as sensitive prevents
    a valid escape next to an invalid one from restoring the raw value to an
    access log.  Query names are not an application data channel, so the small
    risk of over-redacting a name such as ``old_twclid_note`` is preferable to
    disclosing its value.
    """
    # Repeated percent decoding is intentionally conservative, but a crafted
    # name can otherwise make it quadratic in the request-line length. Query
    # names this large have no application use; redact their values without
    # attempting canonicalization so access logging cannot become a CPU-DoS
    # path for unrelated authentication/session requests.
    if len(value) > _MAX_CANONICAL_LOG_QUERY_NAME:
        return True
    canonical = _canonical_log_query_name(value)
    return any(name in canonical for name in _SENSITIVE_QUERY_NAMES)


def _redact_sensitive_query_field(match: re.Match) -> str:
    separator, raw_name, equals, _raw_value = match.groups()
    if _log_query_name_is_sensitive(raw_name):
        return f"{separator}{raw_name}{equals}[redacted]"
    return match.group(0)


def redact_terminal_websocket_query(value):
    """Remove query-carried capabilities from log-bound text.

    Websockify logs both ``self.path`` and HTTP request lines. Terminal tickets
    are one-use capabilities, so retain the useful route while removing the
    entire query in either representation. RFB can also authenticate with an
    ``auth_token`` query parameter, and initial demo navigation carries a
    single-use ``invite`` secret. Advertising click/handoff identifiers must
    also be removed before access logging. Redact every such value while
    preserving non-secret query fields for useful access logs.
    """
    if not isinstance(value, str):
        return value
    terminal_safe = _TERMINAL_WS_LOG_QUERY_RE.sub(r"\1?[query-redacted]", value)
    return _QUERY_FIELD_LOG_RE.sub(_redact_sensitive_query_field, terminal_safe)


def parse_cors_allowlist(value: Optional[str]) -> Tuple[bool, Set[str]]:
    """
    Parse AXGT_CORS_ORIGINS.
    - Empty/None => no cross-origin allowed (same-origin does not require CORS headers)
    - "*" => allow any origin
    - Comma-separated list => allow only those origins (exact match), e.g. "https://axondao.io,http://localhost:6080"
    """
    if not value:
        return False, set()
    raw = value.strip()
    if raw == "*":
        return True, set()
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return False, set(parts)


def cors_origin_for_request(origin_header: Optional[str], host_header: Optional[str], allow_any: bool, allowlist: Set[str]) -> Optional[str]:
    """
    Decide which Access-Control-Allow-Origin to emit.
    - If allow_any => echo request origin (or "*" if origin missing)
    - If allowlist set => echo origin if in allowlist
    - Otherwise => allow same-origin only by matching Origin against Host.
    """
    origin = (origin_header or "").strip()
    host = (host_header or "").strip()

    if allow_any:
        return origin or "*"

    if origin and origin in allowlist:
        return origin

    # Same-origin fallback must compare parsed host/port components. Suffix
    # matching would accept an attacker origin such as evil-example.com for a
    # Host header of example.com.
    try:
        parsed_origin = urlsplit(origin)
        parsed_host = urlsplit("//" + host)
        if (
            parsed_origin.scheme in ("http", "https")
            and parsed_origin.hostname
            and not parsed_origin.username
            and not parsed_origin.password
            and not parsed_origin.path.rstrip("/")
            and not parsed_origin.query
            and not parsed_origin.fragment
            and parsed_host.hostname
            and not parsed_host.username
            and not parsed_host.password
            and not parsed_host.path
            and not parsed_host.query
            and not parsed_host.fragment
        ):
            default_port = 443 if parsed_origin.scheme == "https" else 80
            if (
                parsed_origin.hostname.casefold() == parsed_host.hostname.casefold()
                and (parsed_origin.port or default_port) == (parsed_host.port or default_port)
            ):
                return origin
    except (TypeError, ValueError):
        pass

    return None


class SimpleRateLimiter:
    """Best-effort, in-memory per-key rate limiter: N requests per window seconds."""

    def __init__(self, limit: int, window_seconds: int, max_buckets: int = 10000):
        self.limit = max(1, int(limit))
        self.window = max(1, int(window_seconds))
        self.max_buckets = max(1, int(max_buckets))
        self._buckets: dict[str, tuple[int, float]] = {}
        self._last_prune = 0.0

    def _prune(self, now: float) -> None:
        if (
            (now - self._last_prune) < self.window
            and len(self._buckets) < self.max_buckets
        ):
            return
        cutoff = now - self.window
        self._buckets = {
            key: bucket for key, bucket in self._buckets.items()
            if bucket[1] > cutoff
        }
        self._last_prune = now

    def allow(self, key: str) -> bool:
        now = time.time()
        self._prune(now)
        if key not in self._buckets and len(self._buckets) >= self.max_buckets:
            # A source-key flood must not turn a best-effort limiter into an
            # unbounded memory sink. Existing buckets continue to be enforced.
            return False
        count, start = self._buckets.get(key, (0, now))
        if now - start >= self.window:
            self._buckets[key] = (1, now)
            return True
        if count >= self.limit:
            self._buckets[key] = (count, start)
            return False
        self._buckets[key] = (count + 1, start)
        return True


class SharedFileRateLimiter:
    """O(1), bounded fixed-window limiter shared by both public gate paths.

    A keyed tag selects one fixed-size slot. Only that slot is read/written, so
    an IPv6/source flood cannot turn each request into an O(active-clients) JSON
    rewrite. A torn record is checksummed and fails closed for that slot, not the
    entire service. The HMAC key prevents offline IPv4 reversal from this file.
    Production state is deliberately confined to the container's shared-memory
    filesystem and is never synced: rate state is disposable, and optional CAPI
    admission must not stall the shared gate event loop on durable storage I/O.
    """

    _MAGIC = b"AXCRL002"
    _HEADER = struct.Struct("!8sII")
    _RECORD = struct.Struct("!16sQII")

    def __init__(
        self,
        limit: int,
        window_seconds: int,
        *,
        path: str = "/dev/shm/axonos-x-capi/rate-limit.bin",
        max_buckets: int = 4096,
        digest_key: Optional[bytes] = None,
    ):
        self.limit = max(1, min(600, int(limit)))
        self.window = max(1, min(3600, int(window_seconds)))
        self.max_buckets = max(1, min(8192, int(max_buckets)))
        self.path = path
        self.digest_key = bytes(digest_key) if digest_key else None

    def _open(self):
        parent = os.path.dirname(self.path)
        production_paths = {
            "/dev/shm/axonos-x-capi/rate-limit.bin",
            "/dev/shm/axonos-x-capi/rate-limit-global-status.bin",
            "/dev/shm/axonos-x-capi/rate-limit-global-consent.bin",
            "/dev/shm/axonos-x-capi/rate-limit-privacy.bin",
            "/dev/shm/axonos-x-capi/rate-limit-privacy-global.bin",
        }
        if self.path not in production_paths:
            if os.getenv("X_CAPI_ALLOW_TEST_RATE_FILE") != "1" or not self.path.startswith("/tmp/"):
                raise OSError("invalid shared rate-limit path")
        os.makedirs(parent, mode=0o700, exist_ok=True)
        parent_info = os.lstat(parent)
        if (
            not stat.S_ISDIR(parent_info.st_mode)
            or stat.S_ISLNK(parent_info.st_mode)
            or parent_info.st_uid not in (0, os.geteuid(), 10001)
            or parent_info.st_mode & 0o002
        ):
            raise OSError("unsafe shared rate-limit directory")
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(self.path, flags, 0o600)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid not in (0, os.geteuid())
            or info.st_mode & 0o077
            or info.st_nlink != 1
            or info.st_size > self._HEADER.size + self._RECORD.size * 8192
        ):
            os.close(descriptor)
            raise OSError("unsafe shared rate-limit file")
        return descriptor

    def _initialize_locked(self, descriptor: int) -> None:
        total = self._HEADER.size + self._RECORD.size * self.max_buckets
        os.ftruncate(descriptor, 0)
        os.pwrite(
            descriptor,
            self._HEADER.pack(self._MAGIC, self.max_buckets, self._RECORD.size),
            0,
        )
        os.ftruncate(descriptor, total)

    @staticmethod
    def _checksum(tag: bytes, epoch: int, count: int) -> int:
        return zlib.crc32(tag + struct.pack("!QI", epoch, count) + b"AxRL") & 0xFFFFFFFF

    def allow(self, key: str, *, fail_open_on_error: bool = False) -> bool:
        if not self.digest_key:
            return bool(fail_open_on_error)
        digest = hmac.new(
            self.digest_key,
            ("x-capi-rate\x00" + str(key)).encode("utf-8"),
            hashlib.sha256,
        ).digest()
        tag = digest[:16]
        index = int.from_bytes(digest[16:24], "big") % self.max_buckets
        descriptor = None
        try:
            descriptor = self._open()
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            expected_size = self._HEADER.size + self._RECORD.size * self.max_buckets
            info = os.fstat(descriptor)
            header = os.pread(descriptor, self._HEADER.size, 0)
            if info.st_size == 0:
                self._initialize_locked(descriptor)
            elif (
                info.st_size != expected_size
                or len(header) != self._HEADER.size
                or self._HEADER.unpack(header)
                != (self._MAGIC, self.max_buckets, self._RECORD.size)
            ):
                # The file is single-owner state, not user data. Recovering its
                # fixed layout after a torn initialization is safer than a
                # permanent outage. Callers that need aggregate admission use
                # a separate scoped limiter; privacy callers use this only for
                # a redundant latency hint, never for the privacy mutation.
                self._initialize_locked(descriptor)
            now = time.time()
            epoch = int(now // self.window)
            offset = self._HEADER.size + index * self._RECORD.size
            raw = os.pread(descriptor, self._RECORD.size, offset)
            if len(raw) != self._RECORD.size:
                raise OSError("short rate-limit record")
            old_tag, old_epoch, old_count, old_checksum = self._RECORD.unpack(raw)
            empty = raw == (b"\x00" * self._RECORD.size)
            valid = empty or (
                old_checksum == self._checksum(old_tag, old_epoch, old_count)
                and 1 <= old_count <= self.limit
                and old_epoch <= epoch
            )
            if not valid:
                count = self.limit
                allowed = False
            elif not empty and old_epoch == epoch and old_tag != tag:
                # An unpredictable active-slot collision fails closed and cannot
                # be selected deliberately without the local HMAC key.
                return False
            elif not empty and old_epoch == epoch and old_tag == tag:
                count = old_count
                allowed = count < self.limit
                if allowed:
                    count += 1
            else:
                allowed = True
                count = 1
            output = self._RECORD.pack(
                tag, epoch, count, self._checksum(tag, epoch, count)
            )
            if os.pwrite(descriptor, output, offset) != len(output):
                raise OSError("short rate-limit write")
            return allowed
        except (OSError, ValueError, TypeError, UnicodeError, struct.error):
            return bool(fail_open_on_error)
        finally:
            if descriptor is not None:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except OSError:
                    pass
                os.close(descriptor)


def client_ip_for_rate_limit(
    remote_addr: Optional[str], forwarded_for: Optional[str]
) -> str:
    """Resolve a client address without trusting spoofable forwarding headers."""
    try:
        peer = ipaddress.ip_address(str(remote_addr or ""))
    except ValueError:
        return "invalid-peer"
    raw_hops = (os.getenv("X_CAPI_TRUSTED_PROXY_HOPS") or "0").strip()
    try:
        hops = int(raw_hops)
    except ValueError:
        hops = 0
    if not 1 <= hops <= 8:
        return peer.compressed
    cidrs = []
    try:
        cidrs = [
            ipaddress.ip_network(item.strip(), strict=False)
            for item in (os.getenv("X_CAPI_TRUSTED_PROXY_CIDRS") or "").split(",")
            if item.strip()
        ]
    except ValueError:
        return peer.compressed
    if not cidrs or not any(peer in network for network in cidrs):
        return peer.compressed
    raw_chain = [part.strip() for part in str(forwarded_for or "").split(",") if part.strip()]
    if len(raw_chain) < hops or len(raw_chain) > 16:
        return peer.compressed
    try:
        forwarded = [ipaddress.ip_address(part) for part in raw_chain]
    except ValueError:
        return peer.compressed
    proxy_chain = (forwarded[-(hops - 1):] if hops > 1 else []) + [peer]
    if any(not any(address in network for network in cidrs) for address in proxy_chain):
        return peer.compressed
    return forwarded[-hops].compressed


def gpc_signal_active(value: Optional[str]) -> bool:
    """Treat any affirmative token, or any malformed present GPC header, as on."""
    if value is None or not str(value).strip():
        return False
    tokens = [token.strip() for token in str(value).split(",")]
    if "1" in tokens:
        return True
    # The only standardized affirmative representation is 1. A present but
    # malformed/coalesced value fails privacy-safe instead of silently opting in.
    return True


def request_is_effectively_https(
    remote_addr: Optional[str],
    forwarded_for: Optional[str],
    forwarded_proto: Optional[str],
    direct_scheme: Optional[str],
) -> bool:
    """Accept HTTPS directly or through the explicitly trusted proxy chain."""
    if str(direct_scheme or "").strip().lower() == "https":
        return True
    try:
        peer = ipaddress.ip_address(str(remote_addr or ""))
        hops = int((os.getenv("X_CAPI_TRUSTED_PROXY_HOPS") or "0").strip())
        cidrs = [
            ipaddress.ip_network(item.strip(), strict=False)
            for item in (os.getenv("X_CAPI_TRUSTED_PROXY_CIDRS") or "").split(",")
            if item.strip()
        ]
    except (TypeError, ValueError):
        return False
    if not 1 <= hops <= 8 or not cidrs or not any(peer in network for network in cidrs):
        return False
    forwarded = [part.strip() for part in str(forwarded_for or "").split(",") if part.strip()]
    schemes = [part.strip().lower() for part in str(forwarded_proto or "").split(",") if part.strip()]
    if not hops <= len(forwarded) <= 16 or not hops <= len(schemes) <= 16:
        return False
    try:
        addresses = [ipaddress.ip_address(part) for part in forwarded]
    except ValueError:
        return False
    proxy_chain = (addresses[-(hops - 1):] if hops > 1 else []) + [peer]
    if any(not any(address in network for network in cidrs) for address in proxy_chain):
        return False
    return schemes[-hops] == "https"


def _x_capi_rate_digest_key() -> Optional[bytes]:
    try:
        try:
            import x_capi
        except ImportError:
            from axonos_gate import x_capi
        return x_capi.rate_limit_digest_key()
    except Exception:
        return None


def get_x_capi_rate_limiter() -> SharedFileRateLimiter:
    raw = (os.getenv("X_CAPI_RATE_LIMIT_PER_MIN") or "30").strip()
    try:
        limit = int(raw)
    except ValueError:
        limit = 30
    return SharedFileRateLimiter(
        limit=max(1, min(600, limit)),
        window_seconds=60,
        digest_key=_x_capi_rate_digest_key(),
    )


def get_x_capi_global_rate_limiter(scope: str) -> SharedFileRateLimiter:
    """Cap aggregate public work even when a source rotates client addresses."""
    normalized = "consent" if scope == "consent" else "status"
    raw = (os.getenv("X_CAPI_GLOBAL_RATE_LIMIT_PER_MIN") or "300").strip()
    try:
        limit = int(raw)
    except ValueError:
        limit = 300
    return SharedFileRateLimiter(
        limit=max(1, min(600, limit)),
        window_seconds=60,
        path=f"/dev/shm/axonos-x-capi/rate-limit-global-{normalized}.bin",
        max_buckets=4,
        digest_key=_x_capi_rate_digest_key(),
    )


def get_x_capi_privacy_rate_limiter() -> SharedFileRateLimiter:
    raw = (os.getenv("X_CAPI_PRIVACY_RATE_LIMIT_PER_MIN") or "60").strip()
    try:
        limit = int(raw)
    except ValueError:
        limit = 60
    return SharedFileRateLimiter(
        limit=max(1, min(600, limit)),
        window_seconds=60,
        path="/dev/shm/axonos-x-capi/rate-limit-privacy.bin",
        digest_key=_x_capi_rate_digest_key(),
    )


def get_rate_limiter_from_env() -> Optional[SimpleRateLimiter]:
    """
    AXGT_RATE_LIMIT_PER_MIN: max verify calls per minute per client (best-effort).
    Default is 60. Set to 0 to disable.
    """
    val = os.getenv("AXGT_RATE_LIMIT_PER_MIN", "60").strip()
    try:
        n = int(val)
    except ValueError:
        n = 60
    if n <= 0:
        return None
    return SimpleRateLimiter(limit=n, window_seconds=60)
