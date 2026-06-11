"""
Gate-side helpers for browser <-> desktop file transfer.

Both gate entry points (websockify_gate.py on 6080 and gate_server.py on 8889)
authenticate the wallet themselves, then use this module to resolve the
wallet's desktop target and stream the request to the file agent
(file_agent.py) running inside that desktop container:

  browser ──HTTP──> gate (auth: wallet + AXGT token) ──HTTP──> file agent

In multi-session mode the target is the session container by name on the
shared docker network (axgt-session-<id>) using the per-session key stored on
the session row. In shared single-instance mode the agent runs in this same
container on 127.0.0.1 and the key comes from AXGT_FILES_KEY_FILE.

Streaming is chunked in both directions so multi-GB transfers never buffer in
gate memory.
"""

import http.client
import logging
import os
import urllib.parse
from typing import Callable, Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)

STREAM_CHUNK = 256 * 1024

# browser route suffix -> (HTTP method, agent route)
ROUTES = {
    "list": ("GET", "/list"),
    "download": ("GET", "/download"),
    "upload-status": ("GET", "/upload-status"),
    "upload": ("PUT", "/upload"),
    "mkdir": ("POST", "/mkdir"),
    "cancel-upload": ("POST", "/cancel-upload"),
}

# Query params relayed to the agent (gate-level params like wallet are stripped).
FORWARD_QUERY_PARAMS = ("path", "offset", "total", "overwrite")
# Request headers relayed to the agent.
FORWARD_REQUEST_HEADERS = ("Range", "If-Range", "Content-Type")
# Agent response headers relayed to the browser.
FORWARD_RESPONSE_HEADERS = (
    "Content-Type",
    "Content-Length",
    "Content-Range",
    "Accept-Ranges",
    "ETag",
    "Content-Disposition",
    "Cache-Control",
)


def files_enabled() -> bool:
    raw = (os.getenv("AXGT_FILES_ENABLED") or "true").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _files_port() -> int:
    raw = (os.getenv("AXGT_FILES_PORT") or "").strip()
    try:
        return int(raw) if raw else 8767
    except ValueError:
        return 8767


def _local_agent_key() -> Optional[str]:
    key = (os.getenv("AXGT_SESSION_FILES_KEY") or "").strip()
    if key:
        return key
    path = (os.getenv("AXGT_FILES_KEY_FILE") or "/tmp/.axgt_files_key").strip()
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def _import_session_manager():
    try:
        from . import session_manager
        return session_manager
    except ImportError:
        try:
            from axonos_gate import session_manager
            return session_manager
        except ImportError:
            try:
                import session_manager
                return session_manager
            except ImportError:
                return None


def resolve_target_for_wallet(wallet: str) -> Tuple[Optional[dict], Optional[str]]:
    """Returns ({host, port, key}, None) or (None, error message).

    Paused (credit-exhausted) sessions still resolve while their container is
    alive so users can retrieve their data.
    """
    if not files_enabled():
        return None, "File transfer is disabled on this deployment"
    sm = _import_session_manager()
    session = None
    if sm is not None:
        try:
            session = sm.get_session_for_wallet(wallet)
        except Exception as exc:
            logger.warning("file_transfer: session lookup failed: %s", exc)
            return None, "Session lookup failed"
    if session is None and sm is not None:
        return None, "No active session for this wallet"

    container_id = (session or {}).get("container_id") or ""
    if session and container_id and container_id != "shared-desktop":
        key = (session.get("files_key") or "").strip()
        if not key:
            # Session predates file transfer support; container has no key env.
            return None, "Session was started before file transfer was enabled; restart the session"
        return {
            "host": f"axgt-session-{session['id']}",
            "port": _files_port(),
            "key": key,
        }, None

    # Shared single-instance mode: agent runs in this container.
    key = _local_agent_key()
    if not key:
        return None, "File agent key unavailable"
    return {"host": "127.0.0.1", "port": _files_port(), "key": key}, None


def build_agent_url(route_suffix: str, raw_query: str) -> Optional[str]:
    entry = ROUTES.get(route_suffix)
    if not entry:
        return None
    qs = urllib.parse.parse_qs(raw_query or "", keep_blank_values=False)
    forwarded = {k: qs[k][0] for k in FORWARD_QUERY_PARAMS if k in qs}
    query = urllib.parse.urlencode(forwarded)
    return entry[1] + ("?" + query if query else "")


def gevent_connection_class():
    """HTTPConnection whose socket cooperates with the gevent event loop.

    gate_server runs gevent pywsgi *without* monkey patching, so a default
    blocking http.client socket inside a request greenlet would stall every
    other request for the duration of a multi-GB transfer. Raises ImportError
    when gevent is unavailable (callers fall back to plain HTTPConnection).
    """
    from gevent import socket as gevent_socket

    class _GeventHTTPConnection(http.client.HTTPConnection):
        def connect(self):
            self.sock = gevent_socket.create_connection(
                (self.host, self.port), timeout=self.timeout
            )

    return _GeventHTTPConnection


def proxy_to_agent(
    target: dict,
    method: str,
    agent_path: str,
    request_headers,
    content_length: int = 0,
    body_read: Optional[Callable[[int], bytes]] = None,
    timeout: float = 120.0,
    connection_class=None,
) -> Tuple[int, List[Tuple[str, str]], Iterator[bytes]]:
    """Stream a request to the file agent; returns (status, headers, body iterator).

    The returned iterator must be fully consumed (or dropped) by the caller;
    it closes the upstream connection when exhausted or garbage-collected.
    Raises OSError/http.client errors on connect failure.
    """
    conn_cls = connection_class or http.client.HTTPConnection
    conn = conn_cls(target["host"], target["port"], timeout=timeout)
    try:
        conn.putrequest(method, agent_path, skip_accept_encoding=True)
        conn.putheader("X-AxonOS-Files-Key", target["key"])
        for name in FORWARD_REQUEST_HEADERS:
            value = request_headers.get(name) if request_headers else None
            if value:
                conn.putheader(name, value)
        if method == "PUT":
            conn.putheader("Content-Length", str(max(0, content_length)))
        conn.endheaders()

        if method == "PUT" and content_length > 0 and body_read is not None:
            remaining = content_length
            while remaining > 0:
                chunk = body_read(min(STREAM_CHUNK, remaining))
                if not chunk:
                    raise ConnectionError("client upload stream ended early")
                conn.send(chunk)
                remaining -= len(chunk)

        resp = conn.getresponse()
    except Exception:
        conn.close()
        raise

    headers = []
    for name in FORWARD_RESPONSE_HEADERS:
        value = resp.getheader(name)
        if value is not None:
            headers.append((name, value))

    def body_iter() -> Iterator[bytes]:
        try:
            while True:
                chunk = resp.read(STREAM_CHUNK)
                if not chunk:
                    break
                yield chunk
        finally:
            conn.close()

    return resp.status, headers, body_iter()
