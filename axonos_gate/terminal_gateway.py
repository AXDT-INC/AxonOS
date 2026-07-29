"""Security and protocol helpers for the browser terminal gateway.

The public browser never connects to a tenant container directly and never
receives the per-session agent secret.  A wallet-authenticated HTTP request
receives a short-lived, one-use opaque ticket.  The WebSocket upgrade consumes
that ticket, revalidates the exact active SSH session, and only then connects to
the deterministic private agent endpoint on the session's isolated network.

Browser and agent WebSocket/TCP payloads share a deliberately small framed
protocol.  Each message/frame is::

    1 byte ASCII type | 4 byte big-endian payload length | payload

Client types are I (input), R (resize JSON), P (ping), and C (close).  Agent
types are O (output), X (exit JSON), and E (error JSON).
"""

from __future__ import annotations

import hashlib
import errno
import json
import os
import secrets
import struct
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, Optional, Tuple
from urllib.parse import urlsplit


TICKET_PATH = "/api/terminal/ticket"
WEBSOCKET_PATH = "/api/terminal/ws"
FRAME_HEADER = struct.Struct("!cI")
MAX_CLIENT_INPUT = 64 * 1024
MAX_RESIZE_PAYLOAD = 1024
MAX_AGENT_OUTPUT = 256 * 1024
MAX_AGENT_CONTROL = 4096
MAX_HANDSHAKE_LINE = 4096
TERMINAL_AGENT_PORT = 8791
CONNECT_RETRY_DELAY_SECONDS = 0.2
CONNECT_ATTEMPT_MAX_SECONDS = 1.0


class TerminalGatewayError(RuntimeError):
    """Expected, client-safe terminal gateway failure."""

    def __init__(self, message: str, status: int = 403, code: str = "forbidden"):
        super().__init__(message)
        self.message = message
        self.status = int(status)
        self.code = code


@dataclass(frozen=True)
class TerminalContext:
    wallet_address: str
    session_id: int
    origin: str
    target_host: str
    target_port: int
    agent_secret: str


@dataclass(frozen=True)
class _TicketRecord:
    wallet_address: str
    session_id: int
    origin: str
    expires_at: float


class OneUseTicketStore:
    """In-memory ticket store used by focused tests and single-process tools."""

    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._lock = threading.Lock()
        self._records: Dict[str, _TicketRecord] = {}

    @staticmethod
    def _digest(ticket: str) -> str:
        return hashlib.sha256(ticket.encode("utf-8")).hexdigest()

    def issue(self, wallet_address: str, session_id: int, origin: str, ttl: int) -> str:
        now = self._clock()
        ticket = secrets.token_urlsafe(32)
        record = _TicketRecord(
            wallet_address=wallet_address,
            session_id=int(session_id),
            origin=origin,
            expires_at=now + int(ttl),
        )
        with self._lock:
            self._records = {
                digest: value
                for digest, value in self._records.items()
                if value.expires_at > now
            }
            self._records[self._digest(ticket)] = record
        return ticket

    def consume(self, ticket: str, origin: str) -> _TicketRecord:
        # Pop under one lock: two simultaneous upgrades can never both win.
        digest = self._digest(ticket)
        with self._lock:
            record = self._records.pop(digest, None)
        now = self._clock()
        if record is None or record.expires_at <= now:
            raise TerminalGatewayError(
                "Invalid or expired terminal ticket",
                status=403,
                code="invalid_ticket",
            )
        if not secrets.compare_digest(record.origin, origin):
            raise TerminalGatewayError(
                "Invalid or expired terminal ticket",
                status=403,
                code="invalid_ticket",
            )
        return record


class PostgresTicketStore:
    """Cross-process ticket registry for websockify's fork-per-request model."""

    TABLE = "axgt_terminal_tickets"

    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._init_lock = threading.Lock()
        self._initialized = False

    @staticmethod
    def _digest(ticket: str) -> str:
        return hashlib.sha256(ticket.encode("utf-8")).hexdigest()

    @staticmethod
    def _connect():
        url = (os.getenv("AXGT_CHALLENGE_DB_URL") or "").strip()
        if not url:
            return None
        try:
            import psycopg2

            return psycopg2.connect(url, connect_timeout=5)
        except Exception:
            return None

    def _ensure_table(self) -> bool:
        if self._initialized:
            return True
        with self._init_lock:
            if self._initialized:
                return True
            conn = self._connect()
            if conn is None:
                return False
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        CREATE TABLE IF NOT EXISTS {self.TABLE} (
                            ticket_hash TEXT PRIMARY KEY,
                            wallet_address TEXT NOT NULL,
                            session_id BIGINT NOT NULL,
                            origin TEXT NOT NULL,
                            issued_at DOUBLE PRECISION NOT NULL,
                            expires_at DOUBLE PRECISION NOT NULL
                        )
                        """
                    )
                    cur.execute(
                        f"CREATE INDEX IF NOT EXISTS idx_{self.TABLE}_expires "
                        f"ON {self.TABLE} (expires_at)"
                    )
                conn.commit()
                self._initialized = True
                return True
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                return False
            finally:
                conn.close()

    def issue(self, wallet_address: str, session_id: int, origin: str, ttl: int) -> str:
        if not self._ensure_table():
            raise TerminalGatewayError(
                "Terminal ticket service is temporarily unavailable",
                503,
                "ticket_store_unavailable",
            )
        now = self._clock()
        ticket = secrets.token_urlsafe(32)
        conn = self._connect()
        if conn is None:
            raise TerminalGatewayError(
                "Terminal ticket service is temporarily unavailable",
                503,
                "ticket_store_unavailable",
            )
        try:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {self.TABLE} WHERE expires_at <= %s", (now,))
                cur.execute(
                    f"""INSERT INTO {self.TABLE}
                        (ticket_hash, wallet_address, session_id, origin, issued_at, expires_at)
                        VALUES (%s, %s, %s, %s, %s, %s)""",
                    (
                        self._digest(ticket),
                        wallet_address,
                        int(session_id),
                        origin,
                        now,
                        now + int(ttl),
                    ),
                )
            conn.commit()
            return ticket
        except TerminalGatewayError:
            raise
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            raise TerminalGatewayError(
                "Terminal ticket service is temporarily unavailable",
                503,
                "ticket_store_unavailable",
            ) from exc
        finally:
            conn.close()

    def consume(self, ticket: str, origin: str) -> _TicketRecord:
        if not self._ensure_table():
            raise TerminalGatewayError(
                "Terminal ticket service is temporarily unavailable",
                503,
                "ticket_store_unavailable",
            )
        conn = self._connect()
        if conn is None:
            raise TerminalGatewayError(
                "Terminal ticket service is temporarily unavailable",
                503,
                "ticket_store_unavailable",
            )
        try:
            # DELETE ... RETURNING is the atomic consume.  Delete before checking
            # expiry/origin so a presented ticket can never be replayed, even if
            # its first presentation was malformed or came from a wrong origin.
            with conn.cursor() as cur:
                cur.execute(
                    f"""DELETE FROM {self.TABLE} WHERE ticket_hash = %s
                        RETURNING wallet_address, session_id, origin, expires_at""",
                    (self._digest(ticket),),
                )
                row = cur.fetchone()
            conn.commit()
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            raise TerminalGatewayError(
                "Terminal ticket service is temporarily unavailable",
                503,
                "ticket_store_unavailable",
            ) from exc
        finally:
            conn.close()
        if not row:
            raise TerminalGatewayError(
                "Invalid or expired terminal ticket", 403, "invalid_ticket"
            )
        record = _TicketRecord(
            wallet_address=str(row[0]),
            session_id=int(row[1]),
            origin=str(row[2]),
            expires_at=float(row[3]),
        )
        if record.expires_at <= self._clock() or not secrets.compare_digest(
            record.origin, origin
        ):
            raise TerminalGatewayError(
                "Invalid or expired terminal ticket", 403, "invalid_ticket"
            )
        return record


_ticket_store = PostgresTicketStore()


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = (os.getenv(name) or "").strip()
    try:
        value = int(raw) if raw else int(default)
    except ValueError:
        value = int(default)
    return max(minimum, min(maximum, value))


def terminal_enabled() -> bool:
    raw = (os.getenv("AXGT_TERMINAL_ENABLED") or "true").strip().lower()
    return raw not in ("0", "false", "no", "off")


def ticket_ttl_seconds() -> int:
    return _bounded_env_int("AXGT_TERMINAL_TICKET_TTL_SECONDS", 30, 5, 60)


def revalidate_interval_seconds() -> int:
    return _bounded_env_int("AXGT_TERMINAL_REVALIDATE_SECONDS", 5, 1, 30)


def presence_interval_seconds() -> int:
    return _bounded_env_int("AXGT_TERMINAL_PRESENCE_SECONDS", 10, 2, 30)


def connect_timeout_seconds() -> int:
    return _bounded_env_int("AXGT_TERMINAL_CONNECT_TIMEOUT_SECONDS", 5, 1, 15)


def terminal_agent_port() -> int:
    # Protocol v1 uses one fixed private port.  Every tenant has its own network
    # namespace/IP, so configurability adds mismatch risk without adding capacity.
    return TERMINAL_AGENT_PORT


def connect_terminal_agent(
    context: TerminalContext,
    create_connection: Callable,
    *,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
):
    """Connect to a newly starting private agent within one total deadline.

    Only ECONNREFUSED is retried: it is the expected state between Docker
    reporting the container as started and supervisor binding the agent port.
    DNS/routing/timeouts and every post-connect handshake failure fail
    immediately.  Keeping the handshake outside this helper is intentional:
    an authenticated rejection or busy response must never be retried.
    """
    total_timeout = float(connect_timeout_seconds())
    deadline = monotonic() + total_timeout
    last_refused = None
    while True:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("Terminal agent did not become ready in time") from last_refused
        attempt_timeout = min(CONNECT_ATTEMPT_MAX_SECONDS, remaining)
        try:
            return create_connection(
                (context.target_host, context.target_port),
                timeout=attempt_timeout,
            )
        except OSError as exc:
            if getattr(exc, "errno", None) != errno.ECONNREFUSED:
                raise
            last_refused = exc
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    "Terminal agent did not become ready in time"
                ) from last_refused
            sleep(min(CONNECT_RETRY_DELAY_SECONDS, remaining))


def _authority_parts(authority: str, scheme: str) -> Optional[Tuple[str, int]]:
    """Parse a Host-style authority without accepting credentials or paths."""
    value = (authority or "").strip()
    if (
        not value
        or any(ch in value for ch in ("/", "?", "#", "@", "\\", ","))
        or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in value)
    ):
        return None
    try:
        parsed = urlsplit(f"{scheme}://{value}")
        if not parsed.hostname or parsed.username is not None or parsed.password is not None:
            return None
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if port is None:
        port = 443 if scheme == "https" else 80
    hostname = parsed.hostname.rstrip(".").lower()
    if not hostname:
        return None
    return hostname, port


def _canonical_http_origin(value: Optional[str]) -> Optional[str]:
    """Canonicalize a root HTTP(S) origin, rejecting URL-shaped lookalikes."""
    origin = (value or "").strip()
    if not origin:
        return None
    try:
        parsed = urlsplit(origin)
        scheme = parsed.scheme.lower()
        if (
            scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            return None
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if port is None:
        port = 443 if scheme == "https" else 80
    host = parsed.hostname.rstrip(".").lower()
    if ":" in host:
        host = f"[{host}]"
    default_port = 443 if scheme == "https" else 80
    suffix = "" if port == default_port else f":{port}"
    return f"{scheme}://{host}{suffix}"


def configured_public_origin() -> Optional[str]:
    """Return the deployment's explicit public origin, when valid.

    ``AXGT_PUBLIC_BASE_URL`` is already the canonical externally visible origin
    used by this deployment's payment resources. It is also the safe terminal
    Origin anchor when a TLS terminator rewrites its upstream Host.
    """
    return _canonical_http_origin(os.getenv("AXGT_PUBLIC_BASE_URL"))


def exact_request_origin(
    origin_header: Optional[str],
    host_header: Optional[str],
    forwarded_proto: Optional[str] = None,
    request_scheme: Optional[str] = None,
    trusted_public_origin: Optional[str] = None,
) -> Optional[str]:
    """Return a canonical origin only for an exact same-origin request.

    This intentionally does not use the broader API CORS allowlist. A terminal
    ticket is a browser capability and must originate from the exact page
    origin. When an explicit public origin is configured, it is the sole
    accepted browser Origin and may stand in for Host because TLS proxies often
    rewrite Host and X-Forwarded-Proto to their internal upstream hop. Without
    that configuration, forwarded scheme and Host must agree with Origin.
    """
    canonical_origin = _canonical_http_origin(origin_header)
    if not canonical_origin:
        return None
    configured_origin = _canonical_http_origin(trusted_public_origin)
    configured_origin_matches = canonical_origin == configured_origin
    origin_scheme = urlsplit(canonical_origin).scheme
    if configured_origin is not None and not configured_origin_matches:
        # A configured deployment has one authoritative browser Origin. Host
        # and forwarding headers are request-controlled inputs and cannot
        # broaden it.
        return None
    forwarded_scheme = ((forwarded_proto or "").split(",", 1)[0]).strip().lower()
    if configured_origin is None and forwarded_scheme:
        # A supplied scheme is authoritative and can only narrow acceptance. It
        # cannot make an arbitrary Origin or Host trusted.
        if (
            forwarded_scheme not in ("http", "https")
            or forwarded_scheme != origin_scheme
        ):
            return None
    try:
        parsed = urlsplit(canonical_origin)
        if (
            parsed.scheme.lower() != origin_scheme
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            return None
        origin_port = parsed.port
    except (TypeError, ValueError):
        return None
    if origin_port is None:
        origin_port = 443 if origin_scheme == "https" else 80
    host_parts = _authority_parts(host_header or "", origin_scheme)
    if host_parts is None:
        return None
    if configured_origin_matches:
        # The exact, server-configured public Origin is the browser security
        # boundary. A syntactically valid Host proves this is one HTTP authority,
        # but its value and X-Forwarded-Proto may describe the proxy's internal
        # hop. X-Forwarded-Host is deliberately never consulted.
        return canonical_origin
    origin_authority = (parsed.hostname.rstrip(".").lower(), origin_port)
    if host_parts == origin_authority:
        # Direct requests and TLS proxies that preserve Host remain ordinary
        # exact same-origin checks. This also lets Origin supply the external
        # scheme when the proxy omits X-Forwarded-Proto.
        return canonical_origin
    return None


def _import_dependencies():
    try:
        from .axgt_verifier import get_wallet_access_status
        from .session_manager import get_session_for_wallet, session_status
    except ImportError:
        try:
            from axonos_gate.axgt_verifier import get_wallet_access_status
            from axonos_gate.session_manager import get_session_for_wallet, session_status
        except ImportError:
            from axgt_verifier import get_wallet_access_status
            from session_manager import get_session_for_wallet, session_status
    return get_wallet_access_status, get_session_for_wallet, session_status


def resolve_active_terminal_session(
    wallet_address: str,
    expected_session_id: Optional[int] = None,
) -> TerminalContext:
    """Resolve only an exact active, allocated, funded SSH session."""
    wallet = (wallet_address or "").strip().lower()
    if not wallet:
        raise TerminalGatewayError("Valid wallet address required", 400, "invalid_wallet")
    get_access, get_session, get_status = _import_dependencies()
    try:
        status = get_status(wallet)
        session = get_session(wallet)
        access = get_access(wallet, consume_usage=False)
    except Exception as exc:
        raise TerminalGatewayError(
            "Terminal session state is temporarily unavailable",
            503,
            "state_unavailable",
        ) from exc

    if not isinstance(status, dict) or status.get("reason") in (
        "Session DB unavailable",
        "Internal error",
    ):
        raise TerminalGatewayError(
            "Terminal session state is temporarily unavailable",
            503,
            "state_unavailable",
        )
    if not isinstance(session, dict):
        raise TerminalGatewayError(
            "No active SSH session is available for this wallet",
            409,
            "no_active_session",
        )

    try:
        session_id = int(session.get("id"))
        owner_session_id = int(status.get("owner_session_id"))
    except (TypeError, ValueError):
        raise TerminalGatewayError(
            "No active SSH session is available for this wallet",
            409,
            "no_active_session",
        )
    if session_id <= 0 or owner_session_id != session_id:
        # get_session_for_wallet also includes credit-grace rows; the owner ID is
        # emitted only for an active row, so this comparison excludes grace.
        raise TerminalGatewayError(
            "No active SSH session is available for this wallet",
            409,
            "no_active_session",
        )
    if expected_session_id is not None and session_id != int(expected_session_id):
        raise TerminalGatewayError(
            "The terminal ticket session is no longer active",
            403,
            "session_changed",
        )
    if (session.get("wallet_address") or "").strip().lower() != wallet:
        raise TerminalGatewayError("Terminal session ownership changed", 403, "ownership_changed")
    if session.get("allocation_status") != "allocated" or status.get(
        "owner_allocation_status"
    ) != "allocated":
        raise TerminalGatewayError(
            "The SSH session is not allocated",
            409,
            "session_not_allocated",
        )
    if session.get("ssh_enabled") is not True or status.get("owner_ssh_enabled") is not True:
        raise TerminalGatewayError(
            "The active session is not an SSH session",
            409,
            "not_ssh_session",
        )
    now = time.time()
    try:
        expires_at = float(session.get("expires_at"))
    except (TypeError, ValueError):
        expires_at = 0.0
    hard_expires_at = session.get("hard_expires_at")
    if hard_expires_at is not None:
        try:
            hard_expires_at = float(hard_expires_at)
        except (TypeError, ValueError) as exc:
            raise TerminalGatewayError(
                "Terminal session state is temporarily unavailable",
                503,
                "state_unavailable",
            ) from exc
    if expires_at <= now or (hard_expires_at is not None and hard_expires_at <= now):
        raise TerminalGatewayError("The SSH session has expired", 403, "session_expired")
    if not isinstance(access, dict):
        raise TerminalGatewayError(
            "Wallet credit state is temporarily unavailable",
            503,
            "credit_unavailable",
        )
    try:
        remaining_minutes = float(access.get("remaining_minutes") or 0)
    except (TypeError, ValueError):
        remaining_minutes = 0.0
    if access.get("verified") is not True or remaining_minutes <= 0:
        raise TerminalGatewayError(
            "Prepaid credit is required for terminal access",
            403,
            "credit_exhausted",
        )
    agent_secret = (session.get("files_key") or "").strip()
    container_id = (session.get("container_id") or "").strip()
    if not agent_secret or not container_id or container_id == "shared-desktop":
        raise TerminalGatewayError(
            "This SSH session does not support the web terminal; restart it and try again",
            409,
            "terminal_agent_unavailable",
        )

    return TerminalContext(
        wallet_address=wallet,
        session_id=session_id,
        origin="",
        # Never derive routing from a client field or arbitrary container_id.
        target_host=f"axgt-session-{session_id}",
        target_port=terminal_agent_port(),
        agent_secret=agent_secret,
    )


def issue_terminal_ticket(wallet_address: str, origin: str) -> dict:
    if not terminal_enabled():
        raise TerminalGatewayError("Web terminal is disabled", 503, "disabled")
    context = resolve_active_terminal_session(wallet_address)
    ttl = ticket_ttl_seconds()
    ticket = _ticket_store.issue(
        context.wallet_address,
        context.session_id,
        origin,
        ttl,
    )
    return {
        "ok": True,
        "ticket": ticket,
        "expires_in_seconds": ttl,
        "websocket_path": f"{WEBSOCKET_PATH}?ticket={ticket}",
    }


def consume_terminal_ticket(ticket: str, origin: str) -> TerminalContext:
    if not terminal_enabled():
        raise TerminalGatewayError("Web terminal is disabled", 503, "disabled")
    value = (ticket or "").strip()
    if not value or len(value) > 256:
        raise TerminalGatewayError(
            "Invalid or expired terminal ticket", 403, "invalid_ticket"
        )
    record = _ticket_store.consume(value, origin)
    current = resolve_active_terminal_session(
        record.wallet_address,
        expected_session_id=record.session_id,
    )
    return TerminalContext(
        wallet_address=current.wallet_address,
        session_id=current.session_id,
        origin=origin,
        target_host=current.target_host,
        target_port=current.target_port,
        agent_secret=current.agent_secret,
    )


def terminal_context_is_authorized(context: TerminalContext) -> bool:
    try:
        current = resolve_active_terminal_session(
            context.wallet_address,
            expected_session_id=context.session_id,
        )
        return (
            current.target_host == context.target_host
            and current.target_port == context.target_port
            and secrets.compare_digest(current.agent_secret, context.agent_secret)
        )
    except TerminalGatewayError:
        return False


def encode_frame(frame_type: str, payload: bytes = b"") -> bytes:
    if not isinstance(payload, bytes):
        raise TypeError("frame payload must be bytes")
    kind = frame_type.encode("ascii")
    if len(kind) != 1:
        raise ValueError("frame type must be one ASCII byte")
    return FRAME_HEADER.pack(kind, len(payload)) + payload


def _decode_json_object(payload: bytes) -> dict:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TerminalGatewayError("Malformed terminal control frame", 400, "bad_frame") from exc
    if not isinstance(value, dict):
        raise TerminalGatewayError("Malformed terminal control frame", 400, "bad_frame")
    return value


def validate_client_frame(message) -> bytes:
    """Validate one complete browser frame and return immutable bytes."""
    if not isinstance(message, (bytes, bytearray, memoryview)):
        raise TerminalGatewayError("Binary terminal frames are required", 400, "bad_frame")
    frame = bytes(message)
    if len(frame) < FRAME_HEADER.size:
        raise TerminalGatewayError("Malformed terminal frame", 400, "bad_frame")
    kind_raw, size = FRAME_HEADER.unpack(frame[: FRAME_HEADER.size])
    payload = frame[FRAME_HEADER.size :]
    if size != len(payload):
        raise TerminalGatewayError("Malformed terminal frame", 400, "bad_frame")
    kind = kind_raw.decode("ascii", "strict")
    if kind == "I":
        if size > MAX_CLIENT_INPUT:
            raise TerminalGatewayError("Terminal input frame is too large", 413, "frame_too_large")
    elif kind == "R":
        if size > MAX_RESIZE_PAYLOAD:
            raise TerminalGatewayError("Terminal resize frame is too large", 413, "frame_too_large")
        value = _decode_json_object(payload)
        cols, rows = value.get("cols"), value.get("rows")
        if (
            isinstance(cols, bool)
            or isinstance(rows, bool)
            or not isinstance(cols, int)
            or not isinstance(rows, int)
            or not (2 <= cols <= 1000)
            or not (1 <= rows <= 1000)
        ):
            raise TerminalGatewayError("Invalid terminal dimensions", 400, "bad_frame")
    elif kind in ("P", "C"):
        if size != 0:
            raise TerminalGatewayError("Malformed terminal control frame", 400, "bad_frame")
    else:
        raise TerminalGatewayError("Unsupported terminal frame type", 400, "bad_frame")
    return frame


def extract_agent_frames(buffer: bytearray) -> Iterable[bytes]:
    """Yield complete validated agent frames, leaving an incomplete tail."""
    frames = []
    while len(buffer) >= FRAME_HEADER.size:
        kind_raw, size = FRAME_HEADER.unpack(buffer[: FRAME_HEADER.size])
        try:
            kind = kind_raw.decode("ascii", "strict")
        except UnicodeDecodeError as exc:
            raise TerminalGatewayError("Malformed terminal agent frame", 502, "agent_protocol") from exc
        limit = MAX_AGENT_OUTPUT if kind == "O" else MAX_AGENT_CONTROL
        if kind not in ("O", "X", "E") or size > limit:
            raise TerminalGatewayError("Malformed terminal agent frame", 502, "agent_protocol")
        total = FRAME_HEADER.size + size
        if len(buffer) < total:
            break
        frame = bytes(buffer[:total])
        del buffer[:total]
        if kind in ("X", "E"):
            _decode_json_object(frame[FRAME_HEADER.size :])
        frames.append(frame)
    if len(buffer) > FRAME_HEADER.size + MAX_AGENT_OUTPUT:
        raise TerminalGatewayError("Terminal agent frame is too large", 502, "agent_protocol")
    return frames


def agent_handshake_payload(context: TerminalContext, cols: int = 80, rows: int = 24) -> bytes:
    payload = {
        "version": 1,
        "secret": context.agent_secret,
        "session_id": str(context.session_id),
        "cols": max(1, min(1000, int(cols))),
        "rows": max(1, min(1000, int(rows))),
    }
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(encoded) > MAX_HANDSHAKE_LINE:
        raise TerminalGatewayError("Terminal agent handshake is too large", 502, "agent_protocol")
    return encoded


def validate_agent_handshake_response(line: bytes) -> None:
    if not line or len(line) > MAX_HANDSHAKE_LINE:
        raise TerminalGatewayError("Terminal agent rejected the connection", 502, "agent_unavailable")
    value = _decode_json_object(line.rstrip(b"\r\n"))
    if value.get("ok") is not True or value.get("version") != 1:
        raise TerminalGatewayError("Terminal agent rejected the connection", 502, "agent_unavailable")
