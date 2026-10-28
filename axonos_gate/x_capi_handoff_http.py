"""Strict, attribution-only HTTP adapters shared by Flask and port 6080.

This module has no database or signing-key access. Only the isolated worker can
accept a source request, commit a handoff, or acknowledge privacy closure.
"""
from dataclasses import dataclass
from email.utils import formatdate
import json
import os
from pathlib import Path
import re
import secrets
import time
from urllib.parse import parse_qsl, unquote, urlsplit

try:
    from . import x_capi_handoff as protocol
    from . import x_capi_handoff_rpc as rpc
except ImportError:
    import x_capi_handoff as protocol
    import x_capi_handoff_rpc as rpc

APP_ORIGIN = "https://app.axonos.io"
LANDING_ORIGIN = "https://axonos.io"
APP_COOKIE = "__Host-axon_x_a"
ENTRY = "/attribution/entry"
CALLBACK = "/attribution/callback"
PREFIX = "/api/x-attribution/handoff/"
SOURCE = PREFIX + "source"
JSON_OPS = {PREFIX + op: op for op in ("prepare", "begin", "recover", "withdraw")}
PATHS = frozenset((ENTRY, CALLBACK, SOURCE, *JSON_OPS))
POST_PATHS = PATHS - {ENTRY}
BRIDGE_SCRIPT = Path(__file__).resolve().parents[1] / "novnc-theme/app/x-handoff-bridge.js"
if not BRIDGE_SCRIPT.is_file():
    BRIDGE_SCRIPT = Path("/usr/share/novnc/app/x-handoff-bridge.js")
BASE_HEADERS = {
    "Cache-Control": "no-store, private", "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
}


@dataclass(frozen=True)
class Reply:
    status: int
    body: bytes
    headers: dict


def is_handoff_target(target):
    """Recognize aliases for rejection only; never normalize an accepted target.

    Listeners must route encoded, absolute-form and leading-slash aliases to
    the strict adapter rather than a framework redirect/static-file fallback.
    """
    if not isinstance(target, str):
        return False
    candidate = target
    # Rejection classification must not stop after a fixed decode depth. Limit
    # aggregate work instead; ambiguous over-budget encodings are rejected, not
    # passed to another handler. This classification never authorizes a route.
    decode_budget = 131072
    while decode_budget > 0:
        decode_budget -= len(candidate)
        try:
            path = (candidate.split("?", 1)[0].split("#", 1)[0]
                    if candidate.startswith("/") else urlsplit(candidate).path)
        except ValueError:
            return False
        path = "/" + path.lstrip("/")
        if (path.startswith(PREFIX) or path == PREFIX.rstrip("/")
                or any(path == item or path.startswith(item + ";") or path.startswith(item + "/")
                       for item in (ENTRY, CALLBACK))):
            return True
        # Collapse arbitrary nesting of an encoded percent in one bounded
        # pass. Other forms (including encoded hex digits) still use unquote.
        candidate = re.sub(r"%(?:25)+(?=[0-9A-Fa-f]{2})", "%", candidate)
        decoded = unquote(candidate, encoding="ascii", errors="replace")
        if decoded == candidate:
            break
        candidate = decoded
    return decode_budget <= 0


def privacy_safe_http_log(value):
    """Remove query text at the logging sink, independently of routing.

    Valid ordinary paths, methods and statuses remain useful. Parser exceptions
    must additionally use fixed diagnostics because malformed input has no
    trustworthy request-target boundary.
    """
    if not isinstance(value, str):
        return value
    # Quotes and angle brackets are attacker-controlled query bytes, never
    # delimiters. A valid HTTP request target ends at whitespace; malformed
    # request lines use the listeners' fixed parser diagnostics instead.
    return re.sub(r"\?\S*", "?[query-redacted]", value)


def raw_request_target(requestline):
    """Return the server-parsed request line's target before path normalization."""
    words = requestline.split() if isinstance(requestline, str) else []
    return words[1] if len(words) in (2, 3) else ""


def _header(headers, name):
    """Reject duplicate security headers, including proxy-combined values."""
    getter = getattr(headers, "get_all", None) or getattr(headers, "getlist", None)
    values = getter(name) if getter else None
    if values is None:
        value = headers.get(name)
        values = [] if value is None else [value]
    if len(values) > 1:
        raise protocol.HandoffError("INVALID_REQUEST")
    value = values[0] if values else None
    if value is not None and (not isinstance(value, str) or any(ord(c) < 32 or ord(c) > 126 for c in value)):
        raise protocol.HandoffError("INVALID_REQUEST")
    return value


def body_length(headers, method, path):
    """Called before reading either listener's body. No chunked/ambiguous input."""
    if _header(headers, "Transfer-Encoding") is not None or _header(headers, "Content-Encoding") is not None:
        raise protocol.HandoffError("INVALID_REQUEST")
    raw = _header(headers, "Content-Length")
    if method == "GET":
        if raw not in (None, "0"):
            raise protocol.HandoffError("INVALID_REQUEST")
        return 0
    if raw is None or re.fullmatch(r"0|[1-9][0-9]{0,7}", raw) is None:
        raise protocol.HandoffError("INVALID_REQUEST")
    length = int(raw)
    limit = 256 if path == CALLBACK else 8192
    if length > limit:
        raise protocol.HandoffError("BODY_TOO_LARGE")
    if length == 0:
        raise protocol.HandoffError("INVALID_REQUEST")
    return length


def _media_type(headers, expected):
    raw = _header(headers, "Content-Type") or ""
    if raw.lower() not in (expected, expected + "; charset=utf-8"):
        raise protocol.HandoffError("UNSUPPORTED_MEDIA_TYPE")


def _provenance(headers, *, form=False):
    origin = LANDING_ORIGIN if form else APP_ORIGIN
    if _header(headers, "Origin") != origin:
        raise protocol.HandoffError("ORIGIN_REJECTED")
    if _header(headers, "Sec-Fetch-Site") != ("same-site" if form else "same-origin"):
        raise protocol.HandoffError("ORIGIN_REJECTED")
    if _header(headers, "Sec-Fetch-Mode") not in (("navigate",) if form else ("cors", "same-origin")):
        raise protocol.HandoffError("ORIGIN_REJECTED")
    if _header(headers, "Sec-Fetch-Dest") != ("document" if form else "empty"):
        raise protocol.HandoffError("ORIGIN_REJECTED")
    if form:
        if _header(headers, "Sec-Fetch-User") not in (None, "?1"):
            raise protocol.HandoffError("ORIGIN_REJECTED")
    elif _header(headers, "X-AxonOS-Handoff") != "2":
        raise protocol.HandoffError("ORIGIN_REJECTED")


def trusted_browser_owner_hash(headers, method="POST"):
    """Capture actual A cookie only; never interpret caller JSON as authority.

    This is deliberately pure, bounded and contains no auth/worker I/O. A missing
    value suppresses attribution while the caller continues its business request.
    """
    try:
        origin = _header(headers, "Origin")
        if origin is not None and origin != APP_ORIGIN:
            return None
        if origin is None and method != "GET":
            return None
        if (_header(headers, "Sec-Fetch-Site") != "same-origin"
                or _header(headers, "Sec-Fetch-Mode") not in ("cors", "same-origin")
                or _header(headers, "Sec-Fetch-Dest") != "empty"
                or _header(headers, "Sec-GPC") not in (None, "0")):
            return None
        token = protocol.parse_owner_cookie(_header(headers, "Cookie"))
        return protocol.token_hash(token, "app-owner") if token else None
    except Exception:
        return None


def _positive_enabled():
    # Public adapters have an independent kill switch. Privacy/status transport
    # remains available even when optional positive attribution is disabled.
    return (os.environ.get("X_HANDOFF_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")
            and os.environ.get("X_CAPI_MODE", "off").strip().lower() in ("dry_run", "live"))


def _gpc(headers):
    value = _header(headers, "Sec-GPC")
    if value not in (None, "0", "1"):
        raise protocol.HandoffError("INVALID_REQUEST")
    return value == "1"


def _json_reply(status, body, extra=None):
    raw = protocol.canonical_json(body)
    if len(raw) > 8192:
        raise protocol.HandoffError("DEPENDENCY_UNAVAILABLE")
    headers = dict(BASE_HEADERS, **{"Content-Type": "application/json"})
    headers.update(extra or {})
    return Reply(status, raw, headers)


def _cookie(token, deadline, now_ms):
    remaining = (deadline - now_ms) // 1000
    if remaining <= 0:
        raise protocol.HandoffError("EXPIRED")
    return (f"{APP_COOKIE}={token}; Max-Age={remaining}; Expires="
            f"{formatdate(deadline // 1000, usegmt=True)}; Secure; HttpOnly; SameSite=Strict; Path=/")


def _bridge(kind, data=None, status=200, *, server_gpc=False):
    nonce = secrets.token_urlsafe(24)
    action = LANDING_ORIGIN + "/attribution/authorize" if kind == "entry" else "'none'"
    headers = dict(BASE_HEADERS, **{
        "Content-Type": "text/html; charset=utf-8", "Referrer-Policy": "strict-origin",
        "Content-Security-Policy": "default-src 'none'; script-src 'nonce-" + nonce +
            "'; connect-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; form-action " + action,
    })
    intro = "Conversion sharing" if kind == "entry" else "Continue to AxonOS"
    controls = ("<p>If you agree, AxonOS may use the click you consented to share on axonos.io "
                "to measure wallet verification, completed deposits and session starts. "
                "This choice is separate from landing-page consent and does not sign you in.</p>"
                "<button id=grant type=button>Agree and continue</button>"
                "<button id=deny type=button>Continue without sharing</button>"
                if kind == "entry" else "")
    document = ("<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width'>"
                "<title>AxonOS</title></head><body><h1>" + intro + "</h1>" + controls +
                "<p id=status>Sharing is optional. You can continue normally at any time.</p>"
                "<a href=\"https://app.axonos.io/\" id=continue>Continue to AxonOS</a>")
    if kind in ("entry", "callback", "suppressed"):
        # Only worker-validated ciphertext/UUIDs are embedded; raw H is never
        # accepted from an entry query/body or an arbitrary forwarding message.
        bootstrap = json.dumps({"kind": kind, "result": data, "server_gpc": bool(server_gpc)}, ensure_ascii=True, separators=(",", ":"))
        bootstrap = bootstrap.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
        document += ("<script nonce=\"" + nonce + "\">\nconst AXONOS_HANDOFF_PAGE=" + bootstrap + ";\n" +
                     BRIDGE_SCRIPT.read_text(encoding="utf-8") + "\n</script>")
    document += "</body></html>"
    raw = document.encode("utf-8")
    if len(raw) > 65536:
        raise protocol.HandoffError("DEPENDENCY_UNAVAILABLE")
    return Reply(status, raw, headers)


def failure(error, *, html=False):
    if html:
        return _bridge("error", status=error.status)
    return _json_reply(error.status, error.body())


def request_line_too_long():
    """Transport parser rejection, before any contracted route is accepted."""
    return Reply(414, b"Request line too long\n", dict(BASE_HEADERS, **{
        "Content-Type": "text/plain; charset=utf-8",
    }))


def _worker(frame, call):
    result = call(frame)
    if not isinstance(result, dict) or set(result) != {"status", "body", "headers"}:
        raise protocol.HandoffError("DEPENDENCY_UNAVAILABLE")
    status, body, headers = result["status"], result["body"], result["headers"]
    if type(status) is not int or status < 200 or status > 599 or not isinstance(body, dict) or not isinstance(headers, dict):
        raise protocol.HandoffError("DEPENDENCY_UNAVAILABLE")
    if status != 200:
        protocol.validate_schema("error", body)
        if status != protocol.ERROR_STATUS[body["error"]]:
            raise protocol.HandoffError("DEPENDENCY_UNAVAILABLE")
    return status, body, headers


def _bounded_body(method, path, headers, body, read_body):
    length = body_length(headers, method, path)
    if read_body is not None:
        try:
            body = read_body(length) if length else b""
        except Exception:
            raise protocol.HandoffError("INVALID_REQUEST") from None
    if not isinstance(body, bytes) or len(body) != length:
        raise protocol.HandoffError("INVALID_REQUEST")
    return body


def handle(method, target, headers, body=b"", *, https=False, call=None, now_ms=None, read_body=None):
    """Serve one fixed handoff operation; exceptions never escape to business code."""
    path = target.partition("?")[0] if isinstance(target, str) else ""
    html = path in (ENTRY, CALLBACK)
    try:
        if target != path or path not in PATHS:
            raise protocol.HandoffError("INVALID_REQUEST")
        if _header(headers, "Host") != "app.axonos.io" or not https:
            raise protocol.HandoffError("ORIGIN_REJECTED")
        expected_method = "GET" if path == ENTRY else "POST"
        if method != expected_method:
            raise protocol.HandoffError("INVALID_REQUEST")
        if path == ENTRY:
            body = _bounded_body(method, path, headers, body, read_body)
            if (_header(headers, "Sec-Fetch-Mode") != "navigate"
                    or _header(headers, "Sec-Fetch-Dest") != "document"
                    or _header(headers, "Sec-Fetch-Site") not in ("none", "same-origin", "same-site", "cross-site")):
                raise protocol.HandoffError("ORIGIN_REJECTED")
            if _gpc(headers):
                # Navigation GET never mutates consent or replaces an owner.
                # A nonce-only suppression bridge confirms the origin-local A
                # with the existing same-origin POST withdrawal path, even if
                # Strict omitted it on the initial navigation. Header GPC is
                # authoritative when navigator does not expose the signal.
                return _bridge("suppressed", server_gpc=True)
            return _bridge("entry" if _positive_enabled() else "error")
        call = call or rpc.request
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        if path == SOURCE:
            body = _bounded_body(method, path, headers, body, read_body)
            _media_type(headers, "application/json")
            if any(_header(headers, key) is not None for key in
                   ("Origin", "Sec-Fetch-Site", "Sec-Fetch-Mode", "Sec-Fetch-Dest", "Cookie", "Authorization")):
                raise protocol.HandoffError("ORIGIN_REJECTED")
            # The gate transports exact bytes. Only the worker owns MAC keys.
            raw_body = body.decode("ascii")
            document = protocol.strict_json_loads(body, canonical=True)
            protocol.validate_schema("source-request", document)
            if document["op"] not in ("revoke", "status") and not _positive_enabled():
                raise protocol.HandoffError("ATTRIBUTION_DISABLED")
            status, result, reply_headers = _worker({"kind": "source", "raw_body": raw_body,
                "kid": _header(headers, "X-AxonOS-Handoff-Key-Id") or "",
                "mac": _header(headers, "X-AxonOS-Handoff-MAC") or ""}, call)
            if status == 200:
                protocol.validate_schema("source-success", result)
            allowed = {"X-AxonOS-Handoff-Key-Id", "X-AxonOS-Handoff-MAC"}
            if set(reply_headers) not in (set(), allowed) or (status == 200 and set(reply_headers) != allowed):
                raise protocol.HandoffError("DEPENDENCY_UNAVAILABLE")
            if reply_headers:
                kid = reply_headers["X-AxonOS-Handoff-Key-Id"]
                mac = reply_headers["X-AxonOS-Handoff-MAC"]
                if not isinstance(kid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", kid):
                    raise protocol.HandoffError("DEPENDENCY_UNAVAILABLE")
                try:
                    protocol.b64url_decode(mac, length=32, max_chars=43)
                except Exception:
                    raise protocol.HandoffError("DEPENDENCY_UNAVAILABLE") from None
            return _json_reply(status, result, reply_headers)
        _provenance(headers, form=path == CALLBACK)
        owner = protocol.parse_owner_cookie(_header(headers, "Cookie"))
        gpc = _gpc(headers)
        op = "redeem" if path == CALLBACK else JSON_OPS[path]
        closed = None
        if gpc and owner:
            # Suppression authority is the strict cookie plus exact provenance,
            # independent of the optional positive body's syntax or schema.
            # Do not acknowledge a failed closure, mint an owner, or submit a
            # positive operation when the worker is unavailable.
            import uuid
            status, closed, _ = _worker({"kind": "app", "op": "withdraw", "payload": {"v": 2,
                "request_id": str(uuid.uuid4()), "reason": "gpc"},
                "owner_hash": protocol.token_hash(owner, "app-owner"), "new_owner": False}, call)
            if status != 200:
                raise protocol.HandoffError(closed["error"])
            protocol.validate_schema("withdrawn", closed)
        body = _bounded_body(method, path, headers, body, read_body)
        if path == CALLBACK:
            _media_type(headers, "application/x-www-form-urlencoded")
            raw = body.decode("ascii")
            if re.search(r"%(?![0-9a-fA-F]{2})", raw):
                raise protocol.HandoffError("INVALID_REQUEST")
            pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True, encoding="ascii", errors="strict", max_num_fields=2)
            if len(pairs) != 2 or {key for key, _ in pairs} != {"v", "handoff"}:
                raise protocol.HandoffError("INVALID_REQUEST")
            payload = dict(pairs)
            protocol.validate_schema("bridge-form", payload)
            op = "redeem"
        else:
            _media_type(headers, "application/json")
            payload = protocol.strict_json_loads(body)
            op = JSON_OPS[path]
            protocol.validate_schema("app-" + op, payload)
        if gpc and op == "withdraw" and closed is not None:
            # The monotonic owner closure already committed before body read.
            # A valid body only correlates the acknowledgment; do not issue a
            # second RPC or weaken GPC to the caller's optional reason. Owner
            # withdrawal is idempotent independently of its request UUID.
            return _json_reply(200, dict(closed, request_id=payload["request_id"]))
        if gpc and op != "withdraw":
            raise protocol.HandoffError("GPC_ACTIVE")
        if op != "withdraw" and not _positive_enabled():
            raise protocol.HandoffError("ATTRIBUTION_DISABLED")
        if not owner and op != "prepare":
            raise protocol.HandoffError("OWNER_REQUIRED")
        new_owner = owner is None
        owner = owner or protocol.new_token()
        status, result, _ = _worker({"kind": "app", "op": op, "payload": payload,
            "owner_hash": protocol.token_hash(owner, "app-owner"), "new_owner": new_owner}, call)
        if status != 200:
            return _bridge("error", status=status) if html else _json_reply(status, result)
        schema = {"prepare": "prepared", "begin": "app-granted", "redeem": "redeemed",
                  "recover": "redeemed", "withdraw": "withdrawn"}[op]
        protocol.validate_schema(schema, result)
        cookie = None
        if op == "prepare" and new_owner or op == "redeem":
            cookie = _cookie(owner, result["expires_at_ms"], now_ms)
        if html:
            reply = _bridge("callback", result)
            if cookie:
                reply.headers["Set-Cookie"] = cookie
            return reply
        return _json_reply(status, result, {"Set-Cookie": cookie} if cookie else None)
    except protocol.HandoffError as error:
        return failure(error, html=html)
    except (ValueError, UnicodeError):
        return failure(protocol.HandoffError("INVALID_REQUEST"), html=html)
    except Exception:
        # Never log the exception/request body/cookie/context or propagate it
        # into wallet authentication, settlement or session operations.
        return failure(protocol.HandoffError("DEPENDENCY_UNAVAILABLE"), html=html)
