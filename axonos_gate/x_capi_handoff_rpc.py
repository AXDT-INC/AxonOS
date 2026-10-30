"""Bounded local handoff RPC. Service keys and SQL remain worker-only.

The public gate sends exact service bytes or trusted-cookie-derived app scope.
SO_PEERCRED authenticates the already trusted gate OS identity, not a wallet.
The caller's dispatcher owns database commit/rollback and the existing exclusive
privacy fence. This module never holds a runtime mutex across database or X IO.
"""
from __future__ import annotations

import concurrent.futures
import errno
import os
import select
import socket
import stat
import struct
import threading
import time
from typing import Callable, Optional

try:
    from .x_capi_handoff import (
        BODY_MAX, RPC_MAX, DEFAULT_SOCKET, TIMEOUT_SECONDS, HandoffError,
        ServiceKeyring, FreshnessLease, canonical_json, strict_json_loads,
        validate_schema, _matches, _HASH,
    )
except ImportError:  # Existing standalone worker image entrypoint.
    from x_capi_handoff import (
        BODY_MAX, RPC_MAX, DEFAULT_SOCKET, TIMEOUT_SECONDS, HandoffError,
        ServiceKeyring, FreshnessLease, canonical_json, strict_json_loads,
        validate_schema, _matches, _HASH,
    )


def _error(exc: HandoffError, request_id=None):
    return {"status": exc.status, "body": exc.body(request_id), "headers": {}}


def validate_frame(frame):
    if not isinstance(frame, dict):
        raise HandoffError("INVALID_REQUEST")
    if frame.get("kind") == "readiness":
        if frame != {"kind": "readiness"}:
            raise HandoffError("INVALID_REQUEST")
    elif frame.get("kind") == "source":
        if set(frame) != {"kind", "raw_body", "kid", "mac"} or any(not isinstance(frame[key], str) for key in ("raw_body", "kid", "mac")):
            raise HandoffError("INVALID_REQUEST")
        if len(frame["raw_body"]) > BODY_MAX:
            raise HandoffError("BODY_TOO_LARGE")
        if len(frame["kid"]) > 32 or len(frame["mac"]) > 43:
            raise HandoffError("SERVICE_AUTH_FAILED")
    elif frame.get("kind") == "app":
        required = {"kind", "op", "payload", "owner_hash"}
        if not required <= set(frame) or set(frame) - required - {"new_owner"}:
            raise HandoffError("INVALID_REQUEST")
        if frame["op"] not in ("prepare", "begin", "redeem", "recover", "withdraw") or not isinstance(frame["payload"], dict):
            raise HandoffError("INVALID_REQUEST")
        if not _matches(_HASH, frame["owner_hash"]):
            raise HandoffError("OWNER_REQUIRED")
        if "new_owner" in frame and (type(frame["new_owner"]) is not bool or (frame["new_owner"] and frame["op"] != "prepare")):
            raise HandoffError("INVALID_REQUEST")
        schema = "bridge-form" if frame["op"] == "redeem" else "app-" + frame["op"]
        validate_schema(schema, frame["payload"])
    else:
        raise HandoffError("INVALID_REQUEST")
    if len(canonical_json(frame)) > RPC_MAX:
        raise HandoffError("BODY_TOO_LARGE")


class WorkerHandoffService:
    """Authenticated operation adapter; callbacks MUST commit before returning.

    generation_guard(req) performs the durable generation/high-water check and
    returns the committed control version for lease operations. Its default
    rejects positives: absent provisioned recovery evidence is never readiness.
    Root integrates these callbacks with the existing worker privacy boundary.
    """
    def __init__(self, *, keyring: ServiceKeyring, lease: FreshnessLease,
                 source_handler: Callable, app_handler: Callable,
                 positive_allowed: Callable[[], bool] = lambda: False,
                 clock_assured: Callable[[], bool] = lambda: False,
                 generation_guard: Optional[Callable] = None):
        self.keyring, self.lease = keyring, lease
        self.source_handler, self.app_handler = source_handler, app_handler
        self.positive_allowed, self.clock_assured = positive_allowed, clock_assured
        self.generation_guard = generation_guard

    def _positive(self):
        if not self.positive_allowed():
            self.lease.invalidate()
            raise HandoffError("ATTRIBUTION_DISABLED")
        if not self.clock_assured():
            self.lease.invalidate()
            raise HandoffError("CLOCK_UNTRUSTED")

    def _source(self, frame):
        raw = frame["raw_body"].encode("ascii")
        kid = frame["kid"]
        document = self.keyring.authenticate_request(raw, kid, frame["mac"])
        try:
            validate_schema("source-request", document)
            operation = document["op"]
            if (document["epoch"], document["generation"]) != (self.lease.epoch, self.lease.generation):
                raise HandoffError("GENERATION_RETIRED" if document["generation"] != self.lease.generation else "VERSION_MISMATCH")
            if operation not in ("revoke", "status"):
                self._positive()
            if operation in ("lease_challenge", "lease_attest"):
                if self.generation_guard is None:
                    raise HandoffError("QUARANTINED")
                try:
                    high_water = self.generation_guard(document)
                except Exception:
                    self.lease.invalidate()
                    raise
                result = (self.lease.challenge(document) if operation == "lease_challenge"
                          else self.lease.attest(document, durable_control_version=high_water))
            else:
                result = self.source_handler(document)
            validate_schema("source-" + operation.replace("_", "-") + "-success", result)
            status = 200
        except HandoffError as exc:
            result, status = exc.body(document["request_id"]), exc.status
        except Exception:
            result, status = HandoffError("DEPENDENCY_UNAVAILABLE").body(document["request_id"]), 503
        return {"status": status, "body": result, "headers": self.keyring.sign_response(raw, status, result, kid)}

    def _app(self, frame):
        operation = frame["op"]
        if operation != "withdraw":
            self._positive()
        result = self.app_handler(operation, frame["payload"], frame["owner_hash"], frame.get("new_owner", False))
        name = {"prepare": "prepared", "begin": "app-granted", "redeem": "redeemed", "recover": "redeemed", "withdraw": "withdrawn"}[operation]
        validate_schema(name, result)
        return {"status": 200, "body": result, "headers": {}}

    def reject(self, frame, code="RATE_LIMITED"):
        """Bounded authenticated admission error, with no DB call or authority."""
        try:
            validate_frame(frame)
            error = HandoffError(code)
            if frame["kind"] == "source":
                raw = frame["raw_body"].encode("ascii")
                document = self.keyring.authenticate_request(raw, frame["kid"], frame["mac"])
                result = _error(error, document["request_id"])
                result["headers"] = self.keyring.sign_response(raw, error.status, result["body"], frame["kid"])
                return result
            return _error(error, frame["payload"].get("request_id"))
        except HandoffError as exc:
            return _error(exc)
        except Exception:
            return _error(HandoffError("DEPENDENCY_UNAVAILABLE"))

    def dispatch(self, frame):
        try:
            validate_frame(frame)
            return self._source(frame) if frame["kind"] == "source" else self._app(frame)
        except HandoffError as exc:
            payload = frame.get("payload", {}) if isinstance(frame, dict) else {}
            return _error(exc, payload.get("request_id") if isinstance(payload, dict) else None)
        except Exception:
            return _error(HandoffError("DEPENDENCY_UNAVAILABLE"))


def _peer_uid(connection):
    if not hasattr(socket, "SO_PEERCRED"):
        raise HandoffError("DEPENDENCY_UNAVAILABLE")
    return struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))[1]


def _safe_socket(path, expected_uid):
    parent = os.path.dirname(os.path.abspath(path))
    info, node = os.lstat(parent), os.lstat(path)
    if (os.path.realpath(parent) != parent or not stat.S_ISDIR(info.st_mode)
            or info.st_uid not in (0, expected_uid) or info.st_mode & 0o022
            or not stat.S_ISSOCK(node.st_mode) or node.st_uid != expected_uid or node.st_mode & 0o077):
        raise HandoffError("DEPENDENCY_UNAVAILABLE")


class HandoffRpcClient:
    def __init__(self, path=None, expected_uid=None):
        self.path = path or os.getenv("X_HANDOFF_SOCKET", DEFAULT_SOCKET)
        self.expected_uid = int(os.getenv("X_CAPI_WORKER_UID", "10001")) if expected_uid is None else int(expected_uid)
        if self.expected_uid < 0 or not os.path.isabs(self.path):
            raise ValueError("invalid_handoff_socket")
        # Nonstandard sockets are explicit isolated test construction only.
        if path is None and self.path != DEFAULT_SOCKET:
            raise ValueError("nonstandard_handoff_socket")

    def request(self, frame):
        validate_frame(frame)
        raw = canonical_json(frame)
        deadline = time.monotonic() + TIMEOUT_SECONDS
        try:
            _safe_socket(self.path, self.expected_uid)
            client = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            with client:
                def remaining():
                    value = deadline - time.monotonic()
                    if value <= 0:
                        raise TimeoutError("handoff_timeout")
                    client.settimeout(value)
                remaining()
                client.connect(self.path)
                if _peer_uid(client) != self.expected_uid:
                    raise HandoffError("DEPENDENCY_UNAVAILABLE")
                remaining()
                if client.send(raw) != len(raw):
                    raise OSError("short_send")
                remaining()
                data, _anc, flags, _addr = client.recvmsg(RPC_MAX + 1)
                if flags & getattr(socket, "MSG_TRUNC", 0) or len(data) > RPC_MAX:
                    raise HandoffError("BODY_TOO_LARGE")
            response = strict_json_loads(data, canonical=True, max_bytes=RPC_MAX)
            if (not isinstance(response, dict) or set(response) != {"status", "body", "headers"}
                    or type(response["status"]) is not int or response["status"] not in {200, 400, 401, 403, 409, 410, 413, 415, 429, 503}
                    or not isinstance(response["body"], dict) or not isinstance(response["headers"], dict)
                    or set(response["headers"]) - {"X-AxonOS-Handoff-Key-Id", "X-AxonOS-Handoff-MAC"}
                    or any(not isinstance(x, str) or len(x) > 43 or any(ord(c) < 32 or ord(c) > 126 for c in x) for x in response["headers"].values())):
                raise HandoffError("DEPENDENCY_UNAVAILABLE")
            if frame["kind"] == "readiness" and response["status"] == 200:
                body = response["body"]
                if (set(body) != {"ready", "clock_trusted", "lease_fresh", "positive_allowed"}
                        or any(type(value) is not bool for value in body.values())
                        or body["ready"] != all(body[key] for key in ("clock_trusted", "lease_fresh", "positive_allowed"))
                        or response["headers"]):
                    raise HandoffError("DEPENDENCY_UNAVAILABLE")
            else:
                validate_schema("source-success" if frame["kind"] == "source" else "browser-success", response["body"]) if response["status"] == 200 else validate_schema("error", response["body"])
            if len(canonical_json(response["body"])) > BODY_MAX:
                raise HandoffError("BODY_TOO_LARGE")
            return response
        except HandoffError:
            raise
        except Exception as exc:
            raise HandoffError("DEPENDENCY_UNAVAILABLE") from exc


def request(frame):
    return HandoffRpcClient().request(frame)


class HandoffRpcServer:
    """Finite admission, independent reserved closure slots, one frame per peer."""
    def __init__(self, path: str, dispatch: Callable, *, allowed_uid: int, positive_slots: int = 8, closure_slots: int = 2, reject_handler: Optional[Callable] = None, readiness: Optional[Callable] = None):
        if not 1 <= positive_slots <= 8 or not 1 <= closure_slots <= 2:
            raise ValueError("invalid_rpc_admission")
        self.path, self.dispatch, self.allowed_uid = path, dispatch, allowed_uid
        self.readiness = readiness
        self.health_uid = os.geteuid()
        self.reject = reject_handler or getattr(getattr(dispatch, "__self__", None), "reject", None)
        self._positive = threading.BoundedSemaphore(positive_slots)
        self._closure = threading.BoundedSemaphore(closure_slots)
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=positive_slots + closure_slots, thread_name_prefix="x-handoff")
        self._stop = threading.Event()
        self.sock = None
        self._thread = None
        self._socket_identity = None
        self._futures_lock = threading.Lock()
        self._futures = set()

    def open(self):
        parent = os.path.dirname(os.path.abspath(self.path))
        info = os.lstat(parent)
        if (os.path.realpath(parent) != parent or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.geteuid() or info.st_mode & 0o077
                or not hasattr(socket, "SO_PEERCRED") or not hasattr(socket, "SOCK_SEQPACKET")):
            raise RuntimeError("unsafe_handoff_socket_directory")
        # Reclaim only a proven stale, unchanged, private socket after crash.
        # A second worker cannot unlink a live listener or a non-socket path.
        if os.path.lexists(self.path):
            old = os.lstat(self.path)
            if not stat.S_ISSOCK(old.st_mode) or old.st_uid != os.geteuid() or old.st_mode & 0o077:
                raise RuntimeError("unsafe_existing_handoff_socket")
            with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as probe:
                probe.settimeout(0.05)
                try:
                    probe.connect(self.path)
                except OSError as exc:
                    if exc.errno != errno.ECONNREFUSED:
                        raise RuntimeError("existing_handoff_socket") from None
                else:
                    raise RuntimeError("existing_handoff_socket")
            current = os.lstat(self.path)
            if (current.st_dev, current.st_ino) != (old.st_dev, old.st_ino):
                raise RuntimeError("changed_handoff_socket")
            os.unlink(self.path)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            listener.bind(self.path)
            os.chmod(self.path, 0o600)
            node = os.lstat(self.path)
            self._socket_identity = node.st_dev, node.st_ino
            listener.listen(16)
            listener.setblocking(False)
            self.sock = listener
        except Exception:
            listener.close()
            self.close()
            raise

    def start(self):
        self.open()
        self._thread = threading.Thread(target=self._run, name="x-handoff-listen", daemon=True)
        self._thread.start()
        return self

    @staticmethod
    def _is_close(frame):
        if frame.get("kind") == "app":
            return frame.get("op") == "withdraw"
        try:
            doc = strict_json_loads(frame.get("raw_body", "").encode("ascii"), canonical=True)
            return doc.get("op") == "revoke"
        except Exception:
            return False

    def _run(self):
        while not self._stop.is_set():
            try:
                listener = self.sock
                if listener is None:
                    return
                if not select.select([listener], [], [], 0.05)[0]:
                    continue
                connection, _address = listener.accept()
                started = time.monotonic()
                try:
                    peer = _peer_uid(connection)
                    if peer not in (self.allowed_uid, self.health_uid):
                        connection.close()
                        continue
                    connection.settimeout(0.05)
                    raw, _anc, flags, _addr = connection.recvmsg(RPC_MAX + 1)
                    if flags & getattr(socket, "MSG_TRUNC", 0) or len(raw) > RPC_MAX:
                        raise HandoffError("BODY_TOO_LARGE")
                    frame = strict_json_loads(raw, canonical=True, max_bytes=RPC_MAX)
                    validate_frame(frame)
                    # Only the worker's own UID may ask for aggregate health.
                    # This does not grant it access to gate/source operations.
                    if frame["kind"] == "readiness":
                        if peer != self.health_uid or self.readiness is None:
                            connection.close()
                            continue
                    elif peer != self.allowed_uid:
                        connection.close()
                        continue
                    admission = self._closure if self._is_close(frame) else self._positive
                    if not admission.acquire(blocking=False):
                        response = self.reject(frame, "RATE_LIMITED") if self.reject else _error(HandoffError("RATE_LIMITED"))
                        self._send(connection, response, started)
                        connection.close()
                        continue
                    try:
                        future = self._pool.submit(self._handle, connection, frame, admission, started)
                        with self._futures_lock:
                            self._futures.add(future)
                        future.add_done_callback(self._finished)
                    except Exception:
                        admission.release()
                        connection.close()
                except Exception:
                    connection.close()
            except (OSError, ValueError):
                if self._stop.is_set():
                    return

    @staticmethod
    def _send(connection, response, started):
        remaining = TIMEOUT_SECONDS - (time.monotonic() - started)
        if remaining <= 0:
            return
        raw = canonical_json(response)
        if len(raw) > RPC_MAX:
            raw = canonical_json(_error(HandoffError("DEPENDENCY_UNAVAILABLE")))
        connection.settimeout(min(0.05, remaining))
        if connection.send(raw) != len(raw):
            raise OSError("short_handoff_response")

    def _handle(self, connection, frame, admission, started):
        try:
            response = ({"status": 200, "body": self.readiness(), "headers": {}}
                        if frame["kind"] == "readiness" else self.dispatch(frame))
            self._send(connection, response, started)
        except Exception:
            pass
        finally:
            connection.close()
            admission.release()

    def _finished(self, future):
        with self._futures_lock:
            self._futures.discard(future)

    def close(self, *, timeout=3.0):
        """Close admission and drain accepted handlers before boot replacement.

        A false result forbids replacement. No queued work is cancelled: each
        accepted request retains its connection/slot until its own finally block.
        The caller may retry the bounded drain, with old authority still retired.
        """
        deadline = time.monotonic() + max(0.0, min(float(timeout), 5.0))
        self._stop.set()
        if self.sock is not None:
            self.sock.close()
            self.sock = None
        if self._thread is not None:
            self._thread.join(timeout=max(0.0, deadline - time.monotonic()))
        # No replacement may race the accept thread's final submit.
        if self._thread is not None and self._thread.is_alive():
            return False
        self._pool.shutdown(wait=False, cancel_futures=False)
        try:
            node = os.lstat(self.path)
            if self._socket_identity == (node.st_dev, node.st_ino) and stat.S_ISSOCK(node.st_mode) and node.st_uid == os.geteuid():
                os.unlink(self.path)
        except FileNotFoundError:
            pass
        with self._futures_lock:
            pending = tuple(self._futures)
        if pending:
            _done, pending = concurrent.futures.wait(pending, timeout=max(0.0, deadline - time.monotonic()))
        return not pending
