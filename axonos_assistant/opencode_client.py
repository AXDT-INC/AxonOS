#!/usr/bin/env python3
"""Thread-safe client for the local OpenCode server.

The GTK application sends work from background threads. OpenCode runs prompts
asynchronously and reports progress over SSE, so this client keeps one durable
session while making dispatch, cancellation, approvals, and final-message
reconciliation explicit.
"""

import fcntl
import json
import os
import threading
import time
import uuid
from typing import Callable, Dict, Optional

import requests


class OpenCodeError(RuntimeError):
    """Raised when the local OpenCode service cannot complete an operation."""


class OpenCodeSessionExpired(OpenCodeError):
    """Raised when the server restarted and forgot the stored session."""


class OpenCodeTextReducer:
    """Reduce reordered part snapshots/deltas into unseen assistant text."""

    def __init__(self):
        self.parts = {}
        self.message_roles = {}

    def consume(self, event):
        event_type = event.get("type")
        properties = event.get("properties") or {}
        if event_type == "message.updated":
            info = properties.get("info") or {}
            message_id = info.get("id")
            if not message_id:
                return ""
            self.message_roles[message_id] = info.get("role")
            return "".join(
                self._drain(state)
                for state in self.parts.values()
                if state.get("message_id") == message_id
            )
        if event_type == "message.part.delta" and properties.get("field") == "text":
            part_id = properties.get("partID")
            if not part_id:
                return ""
            state = self.parts.setdefault(
                part_id,
                {"message_id": None, "kind": None, "text": "", "emitted": ""},
            )
            state["message_id"] = properties.get("messageID") or state["message_id"]
            state["text"] += properties.get("delta", "")
            return self._drain(state)
        if event_type == "message.part.updated":
            part = properties.get("part") or {}
            if part.get("type") not in ("text", "reasoning") or not part.get("id"):
                return ""
            state = self.parts.setdefault(
                part["id"],
                {"message_id": None, "kind": None, "text": "", "emitted": ""},
            )
            state["message_id"] = part.get("messageID") or state["message_id"]
            state["kind"] = part["type"]
            snapshot = part.get("text", "")
            if snapshot.startswith(state["text"]):
                state["text"] = snapshot
            elif not state["text"].startswith(snapshot) and len(snapshot) > len(state["text"]):
                state["text"] = snapshot
            return self._drain(state)
        return ""

    def _drain(self, state):
        if (
            self.message_roles.get(state["message_id"]) != "assistant"
            or state["kind"] != "text"
            or not state["text"].startswith(state["emitted"])
        ):
            return ""
        unseen = state["text"][len(state["emitted"]):]
        state["emitted"] = state["text"]
        return unseen


class _TurnState:
    """Mutable state shared by one sender, its SSE listener, and Stop."""

    def __init__(self, session_id, message_id, cancel_epoch):
        self.session_id = session_id
        self.message_id = message_id
        self.cancel_epoch = cancel_epoch
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.connected = threading.Event()
        self.updated = threading.Event()
        self.dispatch_done = threading.Event()
        self.cancel_requested = threading.Event()
        self.cancel_complete = threading.Event()
        self.finished = threading.Event()
        self.listener = None
        self.response = None
        self.dispatch_attempted = False
        self.armed = False
        self.live = False
        self.user_seen = False
        self.assistant_ids = []
        self.error = None
        self.stream_error = None
        self.cleanup_safe = None
        self.terminal_safe = False
        self.marker_lease = None


class OpenCodeClient:
    def __init__(
        self,
        base_url="http://127.0.0.1:4096",
        directory="/home/aXonian",
        marker_path="/run/axonos-assistant/opencode-active",
        marker_wait_timeout=60,
    ):
        self.base_url = base_url.rstrip("/")
        self.directory = directory
        self.marker_path = marker_path
        self.marker_wait_timeout = marker_wait_timeout
        self.session_id = None
        self._lock = threading.RLock()
        self._session_create_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._dispatch_lock = threading.Lock()
        self._cleanup_lock = threading.Lock()
        self._cancel_epoch = 0
        self._pending_cancellations = 0
        self._cancel_barrier = threading.Event()
        self._cancel_barrier.set()
        self._cleanup_error = None
        self._active_turn = None

    @property
    def query(self):
        return {"directory": self.directory}

    def health(self):
        response = requests.get(f"{self.base_url}/global/health", timeout=(3, 5))
        response.raise_for_status()
        payload = response.json()
        return bool(payload.get("healthy"))

    def begin_local_turn(self, expected_cancel_epoch=None):
        """Serialize a tool-free Ollama turn against every AxonAI process."""
        self._send_lock.acquire()
        lease = None
        try:
            cancel_epoch = self._await_cancellation_barrier(expected_cancel_epoch)
            lease = self._claim_turn_marker("direct")
            with self._lock:
                self._check_epoch(cancel_epoch)
            return lease
        except Exception:
            if lease is not None:
                self._release_turn_marker(lease)
            self._send_lock.release()
            raise

    def finish_local_turn(self, lease):
        """Release a tool-free turn marker and its shared send lock."""
        try:
            return self._release_turn_marker(lease)
        finally:
            self._send_lock.release()

    def cancellation_token(self):
        """Return a token that invalidates synchronously when Stop/Reset is clicked."""
        with self._lock:
            return self._cancel_epoch

    def wait_until_ready(self, expected_cancel_epoch=None):
        """Wait until all earlier Stop/Reset cleanup is fully drained."""
        return self._await_cancellation_barrier(expected_cancel_epoch)

    def ensure_session(self, title="AxonAI", cancel_epoch=None):
        """Get or create the durable session without holding the UI-facing lock over I/O."""
        with self._session_create_lock:
            with self._lock:
                self._check_epoch(cancel_epoch)
                if self.session_id:
                    return self.session_id
            response = requests.post(
                f"{self.base_url}/session",
                params=self.query,
                json={"title": title},
                timeout=(5, 15),
            )
            self._raise_for_status(response, "create a session")
            payload = response.json()
            session_id = payload.get("id") if isinstance(payload, dict) else None
            if not session_id:
                raise OpenCodeError("OpenCode created a session without an id")
            with self._lock:
                cancelled = cancel_epoch is not None and cancel_epoch != self._cancel_epoch
                if not cancelled:
                    self.session_id = session_id
            if cancelled:
                self._delete_session(session_id, tolerate_missing=True)
                raise OpenCodeError("The OpenCode turn was cancelled")
            return session_id

    def reset_session(self):
        cancellation = self.begin_cancel(detach_session=True)
        return self.finish_cancel(cancellation, delete_session=True)

    def abort(self):
        cancellation = self.begin_cancel()
        return self.finish_cancel(cancellation)

    def begin_cancel(self, detach_session=False):
        """Invalidate work immediately and return a ticket for background cleanup."""
        with self._lock:
            self._cancel_epoch += 1
            if self._pending_cancellations == 0:
                self._cancel_barrier = threading.Event()
            self._pending_cancellations += 1
            turn = self._active_turn
            session_id = self.session_id
            if detach_session:
                self.session_id = None
            if turn is not None:
                turn.cancel_requested.set()
                turn.updated.set()
            return {
                "session_id": session_id,
                "turn": turn,
                "barrier": self._cancel_barrier,
                "detach_session": detach_session,
            }

    def finish_cancel(self, cancellation, delete_session=False):
        """Abort and reconcile work prepared by :meth:`begin_cancel`."""
        session_id = cancellation.get("session_id")
        turn = cancellation.get("turn")
        delete_session = delete_session or cancellation.get("detach_session", False)
        success = True
        cleanup_safe = True
        try:
            with self._cleanup_lock:
                if turn is not None and turn.session_id == session_id:
                    if not turn.cancel_complete.is_set():
                        cleanup_safe = self._settle_cancelled_turn(turn)
                        with turn.lock:
                            turn.cleanup_safe = cleanup_safe
                    else:
                        with turn.lock:
                            cleanup_safe = turn.cleanup_safe is True
                    success = cleanup_safe
                # With no matching active turn, the epoch check prevents any
                # queued sender from dispatching. An idle durable session needs
                # no abort; Reset may still delete it below.
                with self._lock:
                    if not cleanup_safe:
                        self._cleanup_error = (
                            "OpenCode cancellation could not be proven safe; restart the "
                            "AxonOS session before sending another turn"
                        )
                    safe_to_delete = cleanup_safe and not self._cleanup_error
                    success = success and not self._cleanup_error
                if delete_session and session_id and safe_to_delete:
                    try:
                        self._delete_session(session_id, tolerate_missing=True)
                    except (requests.RequestException, ValueError, OpenCodeError):
                        success = False
            return success
        finally:
            with self._lock:
                self._pending_cancellations = max(0, self._pending_cancellations - 1)
                if self._pending_cancellations == 0:
                    self._cancel_barrier.set()

    def reply_permission(self, permission, response_value):
        if response_value not in {"once", "always", "reject"}:
            raise OpenCodeError("Invalid OpenCode permission response")
        session_id = permission.get("sessionID") or self.session_id
        permission_id = permission.get("id")
        if not session_id or not permission_id:
            raise OpenCodeError("Malformed OpenCode permission request")
        response = requests.post(
            f"{self.base_url}/permission/{permission_id}/reply",
            params=self.query,
            json={"reply": response_value},
            timeout=(3, 15),
        )
        self._raise_for_status(response, "reply to a permission request")
        return bool(response.json())

    def reply_question(self, question_request, answers):
        request_id = question_request.get("id")
        if not request_id:
            raise OpenCodeError("Malformed OpenCode question request")
        response = requests.post(
            f"{self.base_url}/question/{request_id}/reply",
            params=self.query,
            json={"answers": answers},
            timeout=(3, 15),
        )
        self._raise_for_status(response, "reply to an agent question")
        return bool(response.json())

    def reject_question(self, question_request):
        request_id = question_request.get("id")
        if not request_id:
            raise OpenCodeError("Malformed OpenCode question request")
        response = requests.post(
            f"{self.base_url}/question/{request_id}/reject",
            params=self.query,
            timeout=(3, 15),
        )
        self._raise_for_status(response, "reject an agent question")
        return bool(response.json())

    def send_message(
        self,
        text,
        model_id,
        system_prompt=None,
        image_base64=None,
        fresh_session_text=None,
        on_event: Optional[Callable[[Dict], None]] = None,
        on_permission: Optional[Callable[[Dict], str]] = None,
        on_question: Optional[Callable[[Dict], Optional[list]]] = None,
        timeout=900,
        expected_cancel_epoch=None,
    ):
        """Serialize turns and run one asynchronous OpenCode prompt to completion."""
        with self._send_lock:
            cancel_epoch = self._await_cancellation_barrier(expected_cancel_epoch)
            kwargs = {
                "text": text,
                "model_id": model_id,
                "system_prompt": system_prompt,
                "image_base64": image_base64,
                "fresh_session_text": fresh_session_text,
                "on_event": on_event,
                "on_permission": on_permission,
                "on_question": on_question,
                "timeout": timeout,
                "cancel_epoch": cancel_epoch,
            }
            try:
                return self._send_message(**kwargs)
            except OpenCodeSessionExpired:
                with self._lock:
                    self._check_epoch(cancel_epoch)
                    self.session_id = None
                # The first attempt has fully unwound its marker lease. Re-enter
                # the complete safety gate so a failed release, or a Stop that
                # raced the 404, cannot be bypassed by the one allowed retry.
                self._await_cancellation_barrier(cancel_epoch)
                return self._send_message(**kwargs)

    def _await_cancellation_barrier(self, expected_cancel_epoch):
        deadline = time.monotonic() + self.marker_wait_timeout
        waiting_for_marker = False
        while True:
            with self._lock:
                barrier = self._cancel_barrier
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if waiting_for_marker:
                    raise OpenCodeError(
                        "Another AxonAI turn is still active; close the other "
                        "window or restart the AxonOS session if it is no longer running"
                    )
                raise OpenCodeError("Timed out waiting for previous OpenCode cleanup")
            if not barrier.wait(remaining):
                raise OpenCodeError("Timed out waiting for previous OpenCode cleanup")
            with self._lock:
                if barrier is not self._cancel_barrier or self._pending_cancellations:
                    continue
                if self._cleanup_error:
                    raise OpenCodeError(self._cleanup_error)
                cancel_epoch = self._cancel_epoch
                if expected_cancel_epoch is not None and expected_cancel_epoch != cancel_epoch:
                    raise OpenCodeError("The OpenCode turn was cancelled before dispatch")
            marker_state = self._marker_state()
            if marker_state == "clear":
                return cancel_epoch
            if marker_state == "poisoned":
                with self._lock:
                    self._cleanup_error = (
                        "A previous OpenCode turn was interrupted without proven cleanup; "
                        "restart the AxonOS session before sending another turn"
                    )
                raise OpenCodeError(self._cleanup_error)
            waiting_for_marker = True
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))

    def _send_message(
        self, text, model_id, system_prompt, image_base64, fresh_session_text, on_event,
        on_permission, on_question, timeout, cancel_epoch,
    ):
        with self._lock:
            self._check_epoch(cancel_epoch)
            continuing_session = bool(self.session_id)
        session_id = self.ensure_session(cancel_epoch=cancel_epoch)
        prompt_text = text if continuing_session or not fresh_session_text else fresh_session_text
        state = _TurnState(session_id, f"msg_{uuid.uuid4().hex}", cancel_epoch)
        with self._lock:
            self._check_epoch(cancel_epoch)
            self._active_turn = state
        state.listener = threading.Thread(
            target=self._listen_for_events,
            args=(state, on_event, on_permission, on_question),
            daemon=True,
        )
        state.listener.start()
        try:
            if not state.ready.wait(10) or not state.connected.is_set():
                detail = state.stream_error or "no server.connected event"
                raise OpenCodeError(f"Could not subscribe to the OpenCode event stream: {detail}")
            parts = [{"type": "text", "text": prompt_text}]
            if image_base64:
                parts.append({
                    "type": "file", "mime": "image/png",
                    "filename": "axonos-screen.png",
                    "url": f"data:image/png;base64,{image_base64}",
                })
            payload = {
                "messageID": state.message_id,
                "model": {"providerID": "ollama", "modelID": model_id},
                "agent": "build", "parts": parts,
            }
            if system_prompt:
                payload["system"] = system_prompt
            try:
                with self._dispatch_lock:
                    with self._lock:
                        self._check_epoch(cancel_epoch)
                    marker_lease = self._claim_turn_marker("opencode")
                    with state.lock:
                        state.marker_lease = marker_lease
                    state.dispatch_attempted = True
                    response = requests.post(
                        f"{self.base_url}/session/{session_id}/prompt_async",
                        params=self.query, json=payload, timeout=(5, 30),
                    )
                    if response.status_code == 404:
                        with state.lock:
                            state.terminal_safe = True
                        raise OpenCodeSessionExpired("OpenCode session expired before dispatch")
                    self._raise_for_status(response, "dispatch the agent prompt")
                    if response.status_code != 204:
                        raise OpenCodeError(
                            f"OpenCode prompt dispatch returned HTTP {response.status_code}, expected 204"
                        )
                    with state.lock:
                        state.armed = True
                        state.updated.set()
            except requests.RequestException as exc:
                self._cancel_after_failure(state)
                raise OpenCodeError(f"OpenCode prompt dispatch was ambiguous: {exc}") from exc
            finally:
                state.dispatch_done.set()
            return self._wait_for_completion(state, timeout)
        except OpenCodeSessionExpired:
            raise
        except OpenCodeError:
            if state.dispatch_attempted and not state.cancel_requested.is_set():
                self._cancel_after_failure(state)
            raise
        finally:
            # Once cancellation starts, its reconciliation thread exclusively
            # owns the marker lease. It may legitimately outlive this sender
            # (dispatch alone can take 30s), so never poison or close its lease
            # here based on an arbitrary wait timeout.
            with self._lock:
                if self._active_turn is state:
                    if state.cancel_requested.is_set():
                        # A quick Reset must still find the cancelling turn and
                        # join its cleanup instead of deleting its live session.
                        if state.cancel_complete.is_set():
                            self._active_turn = None
                    else:
                        # This lock makes the ownership decision atomic against
                        # begin_cancel(), which acquires the same lock.
                        with state.lock:
                            marker_safe = state.terminal_safe or state.cleanup_safe is True
                            marker_lease = state.marker_lease
                        if marker_safe:
                            self._release_turn_marker(marker_lease)
                        else:
                            self._poison_turn_marker(marker_lease)
                        self._active_turn = None
            self._close_turn(state)
            state.finished.set()

    def _wait_for_completion(self, state, timeout):
        deadline = time.monotonic() + timeout
        last_messages_check = 0.0
        latest_messages = []
        while True:
            if state.cancel_requested.is_set():
                remaining = max(0.0, min(20.0, deadline - time.monotonic()))
                state.cancel_complete.wait(remaining)
                raise OpenCodeError("The OpenCode turn was cancelled")
            with self._lock:
                self._check_epoch(state.cancel_epoch)
            with state.lock:
                stream_error = state.stream_error
                armed = state.armed
            if stream_error:
                self._cancel_after_failure(state)
                raise OpenCodeError(f"OpenCode event stream ended before completion: {stream_error}")
            now = time.monotonic()
            if now >= deadline:
                self._cancel_after_failure(state)
                raise OpenCodeError("OpenCode agent timed out")
            try:
                with state.lock:
                    should_reconcile = (
                        not state.user_seen
                        or not state.live
                        or now - last_messages_check >= 1.0
                    )
                if should_reconcile:
                    latest_messages = self._get_messages(state.session_id)
                    last_messages_check = now
                    self._observe_messages(state, latest_messages)
                with state.lock:
                    # Only a status read initiated after liveness was already
                    # observed can prove terminal idle for an async prompt.
                    live_before_status = state.live
                status = self._get_session_status(state.session_id)
                status_type = status.get("type") if isinstance(status, dict) else None
                if status_type in {"busy", "retry"}:
                    with state.lock:
                        state.live = True
                elif armed and live_before_status and status_type in {None, "idle"}:
                    # Idle is now ordered after liveness. Re-fetch immutable final
                    # messages for complete text, never for the liveness proof.
                    latest_messages = self._get_messages(state.session_id)
                    last_messages_check = now
                    self._observe_messages(state, latest_messages)
                    with state.lock:
                        user_seen = state.user_seen
                        error = state.error
                    if error:
                        raise OpenCodeError(f"OpenCode agent failed: {self._format_error(error)}")
                    assistants = self._assistant_messages(state, latest_messages)
                    if user_seen and assistants:
                        with state.lock:
                            state.terminal_safe = True
                        return "\n\n".join(
                            text for text in (self.extract_text(item) for item in assistants) if text
                        )
            except OpenCodeSessionExpired:
                self._cancel_after_failure(state)
                raise OpenCodeError("OpenCode lost the active session during the agent turn")
            except requests.RequestException:
                pass
            state.updated.wait(min(0.25, max(0.0, deadline - time.monotonic())))
            state.updated.clear()

    def _listen_for_events(self, state, on_event, on_permission, on_question):
        descendants = {state.session_id}
        response = None
        try:
            with requests.get(
                f"{self.base_url}/event", params=self.query,
                headers={"Accept": "text/event-stream"}, stream=True,
                timeout=(5, 900),
            ) as response:
                self._raise_for_status(response, "subscribe to agent events")
                with state.lock:
                    state.response = response
                for line in response.iter_lines(chunk_size=1, decode_unicode=True):
                    if state.stop.is_set():
                        return
                    event = self.parse_sse_line(line)
                    if not event:
                        continue
                    event_type = event.get("type")
                    if event_type == "server.connected":
                        state.connected.set()
                        state.ready.set()
                        continue
                    if event_type == "server.instance.disposed":
                        state.updated.set()
                        if state.cancel_requested.is_set():
                            return
                        raise OpenCodeError("OpenCode server instance was disposed")
                    properties = event.get("properties") or {}
                    if event_type == "session.created":
                        info = properties.get("info") or {}
                        if info.get("parentID") in descendants and info.get("id"):
                            descendants.add(info["id"])
                    if not self.event_matches_sessions(event, descendants):
                        continue
                    if self.event_session_id(event) == state.session_id:
                        self._observe_root_event(state, event)
                    if on_event:
                        try:
                            on_event(event)
                        except Exception:
                            pass
                    if event_type == "permission.asked":
                        decision = "reject"
                        if on_permission:
                            try:
                                decision = on_permission(properties) or "reject"
                            except Exception:
                                decision = "reject"
                        self.reply_permission(properties, decision)
                    elif event_type == "question.asked":
                        try:
                            answers = on_question(properties) if on_question else None
                        except Exception:
                            answers = None
                        if answers is None:
                            self.reject_question(properties)
                        else:
                            self.reply_question(properties, answers)
            if not state.stop.is_set():
                raise OpenCodeError("OpenCode event stream closed")
        except (requests.RequestException, ValueError, OpenCodeError) as exc:
            state.ready.set()
            if not state.stop.is_set():
                with state.lock:
                    state.stream_error = str(exc)
                    state.updated.set()
                if on_event:
                    try:
                        on_event({
                            "type": "client.event.error",
                            "properties": {"sessionID": state.session_id, "error": str(exc)},
                        })
                    except Exception:
                        pass
        finally:
            state.ready.set()
            with state.lock:
                if state.response is response:
                    state.response = None

    def _observe_root_event(self, state, event):
        event_type = event.get("type")
        properties = event.get("properties") or {}
        with state.lock:
            if event_type == "message.updated":
                info = properties.get("info") or {}
                if info.get("id") == state.message_id and info.get("role") == "user":
                    state.user_seen = True
                elif info.get("role") == "assistant" and info.get("parentID") == state.message_id:
                    state.live = True
                    if info.get("error") is not None:
                        state.error = info["error"]
                    if info.get("id") and info["id"] not in state.assistant_ids:
                        state.assistant_ids.append(info["id"])
            elif event_type == "session.status":
                status = properties.get("status") or {}
                if status.get("type") in {"busy", "retry"}:
                    state.live = True
            elif event_type == "session.error":
                state.live = True
                state.error = properties.get("error") or "unknown error"
            elif event_type in {"permission.asked", "question.asked"}:
                # A blocker can only be raised by a runner that is already live.
                state.live = True
            elif event_type in {"message.part.updated", "message.part.delta"}:
                part = properties.get("part") or {}
                message_id = properties.get("messageID") or part.get("messageID")
                if message_id in state.assistant_ids:
                    state.live = True
            state.updated.set()

    def _observe_messages(self, state, messages):
        with state.lock:
            for message in messages:
                info = message.get("info") if isinstance(message, dict) else None
                info = info if isinstance(info, dict) else {}
                if info.get("id") == state.message_id and info.get("role") == "user":
                    state.user_seen = True
                elif info.get("role") == "assistant" and info.get("parentID") == state.message_id:
                    state.live = True
                    if info.get("error") is not None:
                        state.error = info["error"]
                    if info.get("id") and info["id"] not in state.assistant_ids:
                        state.assistant_ids.append(info["id"])

    @staticmethod
    def _assistant_messages(state, messages):
        return [
            message for message in messages
            if isinstance(message, dict)
            and isinstance(message.get("info"), dict)
            and message["info"].get("role") == "assistant"
            and message["info"].get("parentID") == state.message_id
        ]

    def _get_session_status(self, session_id):
        response = requests.get(
            f"{self.base_url}/session/status", params=self.query, timeout=(3, 10),
        )
        if response.status_code == 404:
            raise OpenCodeSessionExpired("OpenCode session status is unavailable")
        self._raise_for_status(response, "read agent status")
        payload = response.json()
        if not isinstance(payload, dict):
            raise OpenCodeError("OpenCode returned malformed session status")
        return payload.get(session_id, {"type": "idle"})

    def _get_messages(self, session_id):
        response = requests.get(
            f"{self.base_url}/session/{session_id}/message",
            params={**self.query, "limit": 50}, timeout=(3, 10),
        )
        if response.status_code == 404:
            raise OpenCodeSessionExpired("OpenCode session messages are unavailable")
        self._raise_for_status(response, "read agent messages")
        payload = response.json()
        if not isinstance(payload, list):
            raise OpenCodeError("OpenCode returned malformed session messages")
        return payload

    def _settle_cancelled_turn(self, state, timeout=8):
        """Wait through dispatch, abort, then fail closed on an ambiguous runner."""
        state.cancel_requested.set()
        state.updated.set()
        safe = False
        try:
            with self._dispatch_lock:
                pass
            state.dispatch_done.wait(1)
            with state.lock:
                if state.terminal_safe:
                    safe = True
                    return safe
            if not state.dispatch_attempted:
                safe = True
                return safe
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                messages = []
                try:
                    messages = self._get_messages(state.session_id)
                    self._observe_messages(state, messages)
                except OpenCodeSessionExpired:
                    # Session deletion is not a runner cancellation in 1.18.26;
                    # disappearance therefore requires the same instance fence.
                    break
                except (requests.RequestException, ValueError, OpenCodeError):
                    pass
                with state.lock:
                    naturally_finished = state.user_seen and bool(
                        self._assistant_messages(state, messages) or state.error
                    )
                    live_before_abort = state.live
                abort_after_live = False
                try:
                    abort_after_live = bool(self._post_abort(state.session_id)) and live_before_abort
                except OpenCodeSessionExpired:
                    break
                except (requests.RequestException, ValueError, OpenCodeError):
                    pass
                try:
                    # This read is deliberately after both observed liveness and
                    # the abort request; an earlier idle snapshot is not proof.
                    status = self._get_session_status(state.session_id)
                    status_type = status.get("type") if isinstance(status, dict) else None
                    if status_type in {"busy", "retry"}:
                        with state.lock:
                            state.live = True
                except OpenCodeSessionExpired:
                    break
                except (requests.RequestException, ValueError, OpenCodeError):
                    status_type = "unknown"
                if status_type in {None, "idle"} and (
                    naturally_finished or abort_after_live
                ):
                    safe = True
                    break
                time.sleep(0.2)
            if not safe:
                # A prompt may be between async dispatch and runner
                # registration, while shell tools can be detached process
                # groups. Neither session deletion nor restarting OpenCode can
                # prove those processes gone. Keep the session and fail closed;
                # only restarting the whole AxonOS session is a hard boundary.
                safe = False
            return safe
        finally:
            with state.lock:
                # Ordered normal completion can win after Stop has started. Its
                # terminal proof is stronger than a later failed status read.
                safe = safe or state.terminal_safe
                state.cleanup_safe = safe
                marker_lease = state.marker_lease
            if safe:
                self._release_turn_marker(marker_lease)
            else:
                self._poison_turn_marker(marker_lease)
            state.stop.set()
            self._close_response(state)
            state.cancel_complete.set()
            state.updated.set()
            with self._lock:
                if self._active_turn is state:
                    self._active_turn = None

    def _cancel_after_failure(self, state):
        if state.cancel_complete.is_set():
            return
        state.cancel_requested.set()
        with self._cleanup_lock:
            if not state.cancel_complete.is_set():
                safe = self._settle_cancelled_turn(state)
                if not safe:
                    with self._lock:
                        self._cleanup_error = (
                            "OpenCode failure cleanup could not be proven safe; restart the "
                            "AxonOS session before sending another turn"
                        )

    def _post_abort(self, session_id):
        response = requests.post(
            f"{self.base_url}/session/{session_id}/abort",
            params=self.query, timeout=(3, 10),
        )
        if response.status_code == 404:
            raise OpenCodeSessionExpired("OpenCode session disappeared during cancellation")
        self._raise_for_status(response, "abort the session")
        return bool(response.json())

    def _delete_session(self, session_id, tolerate_missing=False):
        response = requests.delete(
            f"{self.base_url}/session/{session_id}",
            params=self.query, timeout=(3, 10),
        )
        if tolerate_missing and response.status_code == 404:
            return True
        self._raise_for_status(response, "delete the old session")
        return bool(response.json())

    def _close_turn(self, state):
        state.stop.set()
        self._close_response(state)
        if state.listener is not None and state.listener is not threading.current_thread():
            state.listener.join(timeout=3)

    def _claim_turn_marker(self, kind):
        """Atomically claim the per-container AxonAI execution marker."""
        lock_fd = self._open_marker_lock()
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(lock_fd)
            raise OpenCodeError("Another AxonAI turn is already active") from exc
        except OSError as exc:
            os.close(lock_fd)
            self._latch_marker_failure("Could not lock the AxonAI safety marker")
            raise OpenCodeError(self._cleanup_error) from exc

        if self._marker_lock_is_poisoned(lock_fd):
            os.close(lock_fd)
            self._latch_marker_failure(
                "A previous OpenCode turn was interrupted without proven cleanup"
            )
            raise OpenCodeError(self._cleanup_error)

        marker_fd = None
        try:
            try:
                marker_fd = self._open_existing_marker()
            except FileNotFoundError:
                pass
            if marker_fd is not None:
                payload = self._read_marker(marker_fd)
                if not (
                    payload
                    and payload.get("kind") == "direct"
                    and self._unlink_marker_for_fd(marker_fd)
                ):
                    self._latch_marker_failure(
                        "A previous OpenCode turn was interrupted without proven cleanup"
                    )
                    raise OpenCodeError(self._cleanup_error)
                os.close(marker_fd)
                marker_fd = None

            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            marker_fd = os.open(self.marker_path, flags, 0o600)
            token = uuid.uuid4().hex
            payload = json.dumps({"token": token, "kind": kind}).encode("utf-8")
            if os.write(marker_fd, payload) != len(payload):
                raise OSError("short safety-marker write")
            os.fsync(marker_fd)
            os.close(marker_fd)
            marker_fd = None
            return {"fd": lock_fd, "token": token, "kind": kind, "closed": False}
        except OpenCodeError:
            os.close(lock_fd)
            raise
        except (OSError, ValueError) as exc:
            os.close(lock_fd)
            self._latch_marker_failure("Could not persist the AxonAI safety marker")
            raise OpenCodeError(self._cleanup_error) from exc
        finally:
            if marker_fd is not None:
                try:
                    os.close(marker_fd)
                except OSError:
                    pass

    def _marker_state(self):
        """Return clear, active, or poisoned without trusting marker contents."""
        try:
            lock_fd = self._open_marker_lock()
        except OSError:
            return "poisoned"
        locked = False
        marker_fd = None
        try:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except BlockingIOError:
                return "active"
            if self._marker_lock_is_poisoned(lock_fd):
                return "poisoned"
            try:
                marker_fd = self._open_existing_marker()
            except FileNotFoundError:
                return "clear"
            except OSError:
                return "poisoned"
            payload = self._read_marker(marker_fd)
            if payload and payload.get("kind") == "direct":
                # A dead direct Ollama client cannot leave tool processes. Its
                # stale marker is therefore safe to reap after its lock vanished.
                if self._unlink_marker_for_fd(marker_fd):
                    return "clear"
            return "poisoned"
        finally:
            if marker_fd is not None:
                try:
                    os.close(marker_fd)
                except OSError:
                    pass
            if locked:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            try:
                os.close(lock_fd)
            except OSError:
                pass

    def _open_marker_lock(self):
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        return os.open(f"{self.marker_path}.lock", flags, 0o600)

    @staticmethod
    def _marker_lock_is_poisoned(fd):
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            return bool(os.read(fd, 128))
        except OSError:
            return True

    @staticmethod
    def _poison_marker_lock(fd):
        payload = b"axonos-opencode-poison-v1\n"
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            if os.write(fd, payload) != len(payload):
                return False
            os.fsync(fd)
            return True
        except OSError:
            return False

    def _persist_opencode_poison_marker(self):
        """Keep a failed OpenCode release fail-closed across GTK restarts."""
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        marker_fd = None
        try:
            marker_fd = os.open(self.marker_path, flags, 0o600)
            os.fchmod(marker_fd, 0o600)
            payload = json.dumps({
                "token": f"poison_{uuid.uuid4().hex}",
                "kind": "opencode",
            }).encode("utf-8")
            if os.write(marker_fd, payload) != len(payload):
                return False
            os.fsync(marker_fd)
            file_stat = os.fstat(marker_fd)
            path_stat = os.stat(self.marker_path, follow_symlinks=False)
            return (file_stat.st_dev, file_stat.st_ino) == (
                path_stat.st_dev, path_stat.st_ino,
            )
        except OSError:
            return False
        finally:
            if marker_fd is not None:
                try:
                    os.close(marker_fd)
                except OSError:
                    pass

    def _open_existing_marker(self):
        flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        return os.open(self.marker_path, flags)

    @staticmethod
    def _read_marker(fd):
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            payload = json.loads(os.read(fd, 4096).decode("utf-8"))
            if (
                isinstance(payload, dict)
                and isinstance(payload.get("token"), str)
                and payload.get("kind") in {"opencode", "direct"}
            ):
                return payload
        except (OSError, UnicodeDecodeError, ValueError):
            pass
        return None

    def _unlink_marker_for_fd(self, fd):
        try:
            file_stat = os.fstat(fd)
            path_stat = os.stat(self.marker_path, follow_symlinks=False)
            if (file_stat.st_dev, file_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino):
                return False
            os.unlink(self.marker_path)
            return True
        except (FileNotFoundError, OSError):
            return False

    def _release_turn_marker(self, lease):
        if not lease or lease.get("closed"):
            return True
        lock_fd = lease["fd"]
        marker_fd = None
        released = False
        try:
            marker_fd = self._open_existing_marker()
            payload = self._read_marker(marker_fd)
            released = (
                payload is not None
                and payload.get("token") == lease.get("token")
                and self._unlink_marker_for_fd(marker_fd)
            )
            return released
        except OSError:
            return False
        finally:
            if marker_fd is not None:
                try:
                    os.close(marker_fd)
                except OSError:
                    pass
            if not released and lease.get("kind") == "opencode":
                # A missing or replaced marker must not turn a failed release
                # into a clear state after this process exits. Persist both the
                # marker and a poison bit in its already-locked companion file.
                self._poison_marker_lock(lock_fd)
                self._persist_opencode_poison_marker()
            try:
                os.close(lock_fd)
            except OSError:
                pass
            lease["closed"] = True
            if not released:
                self._latch_marker_failure("Could not clear the AxonAI safety marker")

    def _poison_turn_marker(self, lease):
        """Drop the live lock but retain an unsafe OpenCode marker."""
        if not lease or lease.get("closed"):
            return
        if lease.get("kind") == "opencode":
            self._poison_marker_lock(lease["fd"])
            self._persist_opencode_poison_marker()
        try:
            os.close(lease["fd"])
        except OSError:
            pass
        lease["closed"] = True

    def _latch_marker_failure(self, detail):
        with self._lock:
            self._cleanup_error = f"{detail}; restart the AxonOS session before continuing"

    @staticmethod
    def _close_response(state):
        with state.lock:
            response = state.response
            state.response = None
        if response is not None:
            try:
                response.close()
            except Exception:
                pass

    def _check_epoch(self, expected):
        if expected is not None and expected != self._cancel_epoch:
            raise OpenCodeError("The OpenCode turn was cancelled")

    @staticmethod
    def parse_sse_line(line):
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="replace")
        if not line or not line.startswith("data:"):
            return None
        data = line[5:].strip()
        if not data:
            return None
        return json.loads(data)

    @staticmethod
    def event_session_id(event):
        properties = event.get("properties") or {}
        event_session = properties.get("sessionID")
        if not event_session and isinstance(properties.get("part"), dict):
            event_session = properties["part"].get("sessionID")
        if not event_session and isinstance(properties.get("info"), dict):
            event_session = properties["info"].get("sessionID")
        return event_session

    @staticmethod
    def event_matches_sessions(event, session_ids):
        if event.get("type") == "server.connected":
            return False
        return OpenCodeClient.event_session_id(event) in session_ids

    @staticmethod
    def extract_text(message):
        parts = message.get("parts") if isinstance(message, dict) else None
        if not isinstance(parts, list):
            return ""
        return "".join(
            part.get("text", "") for part in parts
            if isinstance(part, dict) and part.get("type") == "text"
        ).strip()

    @staticmethod
    def _format_error(error):
        if isinstance(error, str):
            return error
        try:
            return json.dumps(error, ensure_ascii=False)[:500]
        except (TypeError, ValueError):
            return str(error)[:500]

    @staticmethod
    def _raise_for_status(response, action):
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            detail = response.text.strip()
            if len(detail) > 500:
                detail = detail[:500] + "…"
            raise OpenCodeError(
                f"Could not {action}: HTTP {response.status_code} {detail}"
            ) from exc
