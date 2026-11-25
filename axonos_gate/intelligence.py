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
