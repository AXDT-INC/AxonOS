"""Request-local HTTP client for the standalone AxonAI service."""
import http.client
import json

try:
    # The production gate serves greenlets without globally patching sockets.
    from gevent import socket
except ImportError:
    import socket

SOCKET_PATH = "/root/axonai/inference.sock"
MAX_BODY = 64 * 1024
# The service admits one running generation and two waiting generations.
REPLY_TIMEOUT = 150


class _InferenceConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(2)
        self.sock.connect(SOCKET_PATH)
        self.sock.settimeout(REPLY_TIMEOUT)


def generate(raw):
    if len(raw) > MAX_BODY:
        return {"error": "The conversation exceeds 64 KiB. Shorten it or start a new chat."}, 413
    try:
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict) or data.keys() - {"prompt", "max_new_tokens"}:
            raise ValueError
        prompt = data.get("prompt")
        limit = data.get("max_new_tokens", 256)
        if not isinstance(prompt, str) or not prompt.strip() or type(limit) is not int or not 1 <= limit <= 256:
            raise ValueError
        prompt.encode("utf-8")
    except (ValueError, RecursionError):
        return {"error": "Send a nonempty prompt and an optional max_new_tokens integer from 1 to 256."}, 400

    connection = _InferenceConnection("localhost")
    try:
        connection.request("POST", "/generate", body=raw, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        if response.status == 429:
            return {"error": "AxonAI’s queue is full. Try again shortly."}, 429
        if response.status == 503:
            return {"error": "AxonAI is unavailable. Try again shortly."}, 503
        if response.status == 400:
            if "<|im_start|>" in prompt or "<|im_end|>" in prompt:
                return {"error": "Remove reserved chat delimiters and try again."}, 400
            return {"error": "The conversation exceeds the 2,048-token input limit. Shorten it or start a new chat."}, 400
        if response.status != 200:
            return {"error": "AxonAI could not generate a reply."}, 502
        body = response.read(MAX_BODY + 1)
        if len(body) > MAX_BODY:
            raise ValueError
        reply = json.loads(body)
        if (not isinstance(reply, dict) or not isinstance(reply.get("text"), str)
                or type(reply.get("input_tokens")) is not int or not 1 <= reply["input_tokens"] <= 2048
                or type(reply.get("output_tokens")) is not int or not 1 <= reply["output_tokens"] <= limit):
            raise ValueError
        return {key: reply[key] for key in ("text", "input_tokens", "output_tokens")}, 200
    except TimeoutError:
        return {"error": "AxonAI took too long to respond. Try again."}, 504
    except (OSError, http.client.RemoteDisconnected):
        return {"error": "AxonAI is unavailable. Try again shortly."}, 503
    except (ValueError, RecursionError, http.client.HTTPException):
        return {"error": "AxonAI returned an invalid reply."}, 502
    finally:
        connection.close()


def conversation_rpc(wallet, operation, **data):
    """Delegate volume access to the authenticated launcher off the WSGI hub."""
    import os
    try:
        from . import session_launcher
    except ImportError:
        import session_launcher
    base = os.environ.get("AXGT_SESSION_LAUNCHER_URL", "").rstrip("/")
    if not base or not os.environ.get("AXGT_SESSION_LAUNCHER_TOKEN", "").strip():
        return {"error": "Wallet storage is unavailable."}, 503
    args = ("POST", base + "/intelligence/storage", {"wallet": wallet, "operation": operation, "data": data}, 30)
    try:
        from gevent import get_hub
    except ImportError:
        status, payload, error = session_launcher._http_json(*args)
    else:
        status, payload, error = get_hub().threadpool.apply(session_launcher._http_json, args)
    if error or status in (401, 403) or not isinstance(payload, dict) or ("error" not in payload and status >= 400):
        return {"error": "Wallet storage is unavailable. Try again shortly."}, 503
    return payload, status


def _share_cipher():
    import base64
    import hashlib
    import os
    from cryptography.fernet import Fernet

    secret = os.environ.get("AXGT_SESSION_LAUNCHER_TOKEN", "").strip()
    if not secret or secret == "change-me-launcher-token":
        raise ValueError("A private launcher token is required for sharing.")
    key = hashlib.sha256(b"axonos/intelligence/public-share/v1\0" + secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def new_share_token(wallet, cid):
    # Both identifiers travel in the encrypted capability. The wallet home
    # additionally records the exact active token, which enforces revocation.
    return _share_cipher().encrypt((wallet + cid).encode("ascii")).decode("ascii")


def resolve_share(token):
    import re
    from cryptography.fernet import InvalidToken
    if not re.fullmatch(r"[A-Za-z0-9_-]{100,510}={0,2}", token):
        return None
    try:
        identity = _share_cipher().decrypt(token.encode()).decode("ascii")
    except (InvalidToken, ValueError):
        return None
    if re.fullmatch(r"0x[0-9a-f]{72}", identity):
        return identity[:42], identity[42:]
    return None
