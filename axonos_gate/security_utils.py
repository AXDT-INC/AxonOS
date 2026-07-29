import os
import re
import time
from typing import Optional, Set, Tuple


_TERMINAL_WS_LOG_QUERY_RE = re.compile(
    r"(/api/terminal/ws)\?[^\s'\"<>]*"
)


def redact_terminal_websocket_query(value):
    """Remove terminal capability queries from log-bound text.

    Websockify logs both ``self.path`` and HTTP request lines. Terminal tickets
    are one-use capabilities, so retain the useful route while removing the
    entire query in either representation. Other routes are left unchanged.
    """
    if not isinstance(value, str):
        return value
    return _TERMINAL_WS_LOG_QUERY_RE.sub(r"\1?[query-redacted]", value)


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

    # Same-origin heuristic: if Origin ends with Host (scheme + host[:port]).
    # This avoids emitting "*" while still working for typical same-origin POSTs.
    if origin and host and origin.endswith(host):
        return origin

    return None


class SimpleRateLimiter:
    """Best-effort, in-memory per-key rate limiter: N requests per window seconds."""

    def __init__(self, limit: int, window_seconds: int):
        self.limit = max(1, int(limit))
        self.window = max(1, int(window_seconds))
        self._buckets: dict[str, tuple[int, float]] = {}

    def allow(self, key: str) -> bool:
        now = time.time()
        count, start = self._buckets.get(key, (0, now))
        if now - start >= self.window:
            self._buckets[key] = (1, now)
            return True
        if count >= self.limit:
            self._buckets[key] = (count, start)
            return False
        self._buckets[key] = (count + 1, start)
        return True


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
