"""Request-local HTTP client for the standalone AxonAI service."""
import http.client
import json
import uuid

try:
    # The production gate serves greenlets without globally patching sockets.
    from gevent import socket
except ImportError:
    import socket

SOCKET_PATH = "/root/axonai/inference.sock"
MAX_BODY = 64 * 1024
MAX_REPLY_BYTES = 1024 * 1024
NDJSON = "application/x-ndjson"
FINISH_REASONS = ("stop", "length", "cancelled")
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
        # The buffered API has a fixed legacy timeout. Long answers use the
        # streaming protocol and its worker-selected production limit.
        data["max_new_tokens"] = limit
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
        connection.request("POST", "/generate", body=body, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        if response.status != 200:
            return generation_error(response.status, prompt)
        body = response.read(MAX_BODY + 1)
        if len(body) > MAX_BODY:
            raise ValueError
        reply = json.loads(body)
        if (not isinstance(reply, dict) or not isinstance(reply.get("text"), str)
                or type(reply.get("input_tokens")) is not int or not 1 <= reply["input_tokens"] <= 2048
                or type(reply.get("output_tokens")) is not int or not 1 <= reply["output_tokens"] <= limit):
            raise ValueError
        result = {key: reply[key] for key in ("text", "input_tokens", "output_tokens")}
        # Older worker installations don't report why a buffered answer ended.
        if "finish_reason" in reply:
            if reply["finish_reason"] not in FINISH_REASONS:
                raise ValueError
            result["finish_reason"] = reply["finish_reason"]
        return result, 200
    except TimeoutError:
        return {"error": "AxonAI took too long to respond. Try again."}, 504
    except (OSError, http.client.RemoteDisconnected):
        return {"error": "AxonAI is unavailable. Try again shortly."}, 503
    except (ValueError, RecursionError, http.client.HTTPException):
        return {"error": "AxonAI returned an invalid reply."}, 502
    finally:
        connection.close()


def generation_error(status, prompt=""):
    """Only gate-owned messages cross the public boundary."""
    if status == 400:
        if "<|im_start|>" in prompt or "<|im_end|>" in prompt:
            return {"error": "Remove reserved chat delimiters and try again."}, 400
        return {"error": "The conversation exceeds the 2,048-token input limit. Shorten it or start a new chat."}, 400
    messages = {
        409: "This message is already being generated.",
        413: "The conversation exceeds the inference request size. Shorten it or start a new chat.",
        429: "AxonAI’s queue is full. Try again shortly.",
        503: "AxonAI is unavailable. Try again shortly.",
        504: "AxonAI took too long to respond. Try again.",
    }
    if status in messages:
        return {"error": messages[status]}, status
    return {"error": "AxonAI could not generate a reply."}, 502


def worker_request_id(wallet, request_id):
    """Namespace cancellation and duplicate detection by the verified wallet."""
    try:
        if not isinstance(request_id, str) or str(uuid.UUID(request_id)) != request_id:
            return None
    except ValueError:
        return None
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "axonos/intelligence/" + wallet + "/" + request_id))


def cancel(work_id):
    conn = _InferenceConnection("localhost")
    try:
        conn.connect()
        conn.sock.settimeout(5)
        conn.request("POST", "/generations/" + work_id + "/stop", body=b"{}",
                     headers={"Content-Type": "application/json"})
        with conn.getresponse() as reply:
            if len(reply.read(4097)) > 4096:
                return generation_error(502)
            if reply.status == 200:
                return {}, 200
            return generation_error(reply.status)
    except (OSError, http.client.HTTPException):
        return {"error": "AxonAI could not confirm the stop request. Try again."}, 503
    finally:
        conn.close()


def stream(prompt, work_id):
    """Read complete bounded frames. Closing this iterator disconnects its worker."""
    conn = _InferenceConnection("localhost")
    status = 502
    try:
        raw = json.dumps({"prompt": prompt, "request_id": work_id}, ensure_ascii=False).encode()
        conn.request("POST", "/generate/stream", body=raw, headers={"Content-Type": "application/json"})
        with conn.getresponse() as response:
            if response.status != 200:
                status = response.status
            elif response.headers.get_content_type() == NDJSON:
                started = False
                total = 0
                while True:
                    # Each line is flushed by the private worker. A multi-byte
                    # Unicode character or one frame may span several reads.
                    line = response.readline(6 * MAX_REPLY_BYTES + 1024)
                    if not line.endswith(b"\n"):
                        break
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        break
                    kind = event.get("type")
                    if kind == "error":
                        code = event.get("status")
                        status = code if type(code) is int else 502
                        break
                    if not started:
                        if kind != "start":
                            break
                        started = True
                        yield {"type": "start"}
                    elif kind == "ping":
                        yield {"type": kind}
                    elif kind == "delta" and isinstance(event.get("text"), str):
                        total += len(event["text"].encode())
                        if total > MAX_REPLY_BYTES:
                            break
                        yield {"type": kind, "text": event["text"]}
                    elif kind == "done":
                        answer = event.get("text")
                        reason = event.get("finish_reason")
                        if (not isinstance(answer, str) or len(answer.encode()) > MAX_REPLY_BYTES
                                or reason not in FINISH_REASONS):
                            break
                        yield {"type": kind, "text": answer, "finish_reason": reason}
                        return
                    else:
                        break
    except TimeoutError:
        status = 504
    except OSError:
        status = 503
    except (http.client.HTTPException, ValueError, RecursionError):
        status = 502
    finally:
        conn.close()
    payload, code = generation_error(status, prompt)
    yield {"type": "error", "status": code, **payload}


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
