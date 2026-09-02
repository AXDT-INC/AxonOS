import json
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from opencode_client import (
    OpenCodeClient,
    OpenCodeError,
    OpenCodeSessionExpired,
    OpenCodeTextReducer,
    _TurnState,
)


class FakeResponse:
    def __init__(self, payload=None, status_code=200, text=""):
        self.payload = payload
        self.status_code = status_code
        self.text = text

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(str(self.status_code), response=self)


class OpenCodeClientTests(unittest.TestCase):
    def setUp(self):
        self.marker_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.marker_dir.cleanup)
        self.marker_path = os.path.join(self.marker_dir.name, "opencode-active")
        self.client = OpenCodeClient(
            "http://127.0.0.1:4096",
            "/home/aXonian",
            marker_path=self.marker_path,
            marker_wait_timeout=0.2,
        )
        def connect_events(state, *_callbacks):
            state.connected.set()
            state.ready.set()
        self.client._listen_for_events = connect_events

    def test_parse_sse_and_filter_sessions(self):
        event = OpenCodeClient.parse_sse_line(
            'data: {"type":"message.part.delta","properties":{"sessionID":"ses_1"}}'
        )
        self.assertEqual(event["type"], "message.part.delta")
        self.assertTrue(OpenCodeClient.event_matches_sessions(event, {"ses_1"}))
        self.assertFalse(OpenCodeClient.event_matches_sessions(event, {"ses_other"}))
        self.assertIsNone(OpenCodeClient.parse_sse_line(": heartbeat"))

    def test_extract_text_ignores_non_text_parts(self):
        message = {
            "parts": [
                {"type": "text", "text": "Hello "},
                {"type": "tool", "state": {"status": "completed"}},
                {"type": "text", "text": "world"},
            ]
        }
        self.assertEqual(OpenCodeClient.extract_text(message), "Hello world")

    def test_text_reducer_handles_delta_before_snapshot_without_duplicates(self):
        reducer = OpenCodeTextReducer()
        delta = {
            "type": "message.part.delta",
            "properties": {
                "messageID": "msg_1", "partID": "prt_1", "field": "text", "delta": "Hel",
            },
        }
        snapshot = {
            "type": "message.part.updated",
            "properties": {"part": {
                "id": "prt_1", "messageID": "msg_1", "type": "text", "text": "Hello",
            }},
        }

        self.assertEqual(reducer.consume(delta), "")
        self.assertEqual(reducer.consume(snapshot), "")
        self.assertEqual(reducer.consume({
            "type": "message.updated",
            "properties": {"info": {"id": "msg_1", "role": "assistant"}},
        }), "Hello")
        self.assertEqual(reducer.consume(snapshot), "")
        delta["properties"]["delta"] = " world"
        self.assertEqual(reducer.consume(delta), " world")

    def test_text_reducer_never_exposes_reasoning(self):
        reducer = OpenCodeTextReducer()
        reducer.consume({
            "type": "message.part.delta",
            "properties": {
                "messageID": "msg_why", "partID": "why", "field": "text", "delta": "private",
            },
        })
        reducer.consume({
            "type": "message.updated",
            "properties": {"info": {"id": "msg_why", "role": "assistant"}},
        })
        self.assertEqual(reducer.consume({
            "type": "message.part.updated",
            "properties": {"part": {
                "id": "why", "messageID": "msg_why", "type": "reasoning", "text": "private chain",
            }},
        }), "")

    def test_text_reducer_does_not_echo_user_or_unknown_messages(self):
        reducer = OpenCodeTextReducer()
        reducer.consume({
            "type": "message.updated",
            "properties": {"info": {"id": "msg_user", "role": "user"}},
        })
        self.assertEqual(reducer.consume({
            "type": "message.part.updated",
            "properties": {"part": {
                "id": "prt_user", "messageID": "msg_user", "type": "text", "text": "secret prompt",
            }},
        }), "")
        self.assertEqual(reducer.consume({
            "type": "message.part.updated",
            "properties": {"part": {
                "id": "prt_unknown", "messageID": "msg_unknown", "type": "text", "text": "unknown",
            }},
        }), "")

    @patch("opencode_client.requests.post")
    def test_prompt_async_reuses_session_and_includes_message_id_and_image(self, post):
        post.side_effect = [
            FakeResponse({"id": "ses_1"}),
            FakeResponse(status_code=204),
            FakeResponse(status_code=204),
        ]

        def reconciled_messages(_session_id):
            payload = post.call_args_list[-1].kwargs["json"]
            message_id = payload["messageID"]
            answer = "first" if payload["parts"][0]["text"] == "look" else "second"
            return [
                {"info": {"id": message_id, "role": "user"}, "parts": []},
                {
                    "info": {"id": f"asst_{answer}", "role": "assistant", "parentID": message_id},
                    "parts": [{"type": "text", "text": answer}],
                },
            ]

        with patch.object(self.client, "_get_session_status", return_value={"type": "idle"}), \
                patch.object(self.client, "_get_messages", side_effect=reconciled_messages):
            first = self.client.send_message("look", "qwen3.8:latest", image_base64="abc")
            second = self.client.send_message("continue", "qwen3.8:latest")

        self.assertEqual((first, second), ("first", "second"))
        create_calls = [call for call in post.call_args_list if call.args[0].endswith("/session")]
        prompt_calls = [call for call in post.call_args_list if call.args[0].endswith("/prompt_async")]
        self.assertEqual(len(create_calls), 1)
        self.assertEqual(len(prompt_calls), 2)
        self.assertTrue(all(call.args[0].endswith("/session/ses_1/prompt_async") for call in prompt_calls))
        first_payload = prompt_calls[0].kwargs["json"]
        self.assertTrue(first_payload["messageID"].startswith("msg_"))
        self.assertEqual(first_payload["model"], {
            "providerID": "ollama", "modelID": "qwen3.8:latest",
        })
        self.assertEqual(first_payload["parts"][1], {
            "type": "file",
            "mime": "image/png",
            "filename": "axonos-screen.png",
            "url": "data:image/png;base64,abc",
        })
        self.assertFalse(os.path.exists(self.marker_path))

    @patch("opencode_client.requests.post")
    def test_stale_session_is_recreated_once(self, post):
        self.client.session_id = "ses_old"
        post.side_effect = [
            FakeResponse(status_code=404),
            FakeResponse({"id": "ses_new"}),
            FakeResponse(status_code=204),
        ]

        def reconciled_messages(_session_id):
            message_id = post.call_args_list[-1].kwargs["json"]["messageID"]
            return [
                {"info": {"id": message_id, "role": "user"}, "parts": []},
                {
                    "info": {"id": "asst_recovered", "role": "assistant", "parentID": message_id},
                    "parts": [{"type": "text", "text": "recovered"}],
                },
            ]

        with patch.object(self.client, "_get_session_status", return_value={"type": "idle"}), \
                patch.object(self.client, "_get_messages", side_effect=reconciled_messages):
            result = self.client.send_message(
                "current only",
                "qwen3.8:latest",
                fresh_session_text="User: earlier\nAssistant: context\n\nCurrent: current only",
            )

        self.assertEqual(result, "recovered")
        self.assertEqual(self.client.session_id, "ses_new")
        prompt_calls = [call for call in post.call_args_list if call.args[0].endswith("/prompt_async")]
        self.assertEqual(len(prompt_calls), 2)
        self.assertIn("/session/ses_old/prompt_async", prompt_calls[0].args[0])
        self.assertIn("/session/ses_new/prompt_async", prompt_calls[1].args[0])
        self.assertEqual(prompt_calls[0].kwargs["json"]["parts"][0]["text"], "current only")
        self.assertIn("Assistant: context", prompt_calls[1].kwargs["json"]["parts"][0]["text"])

    @patch("opencode_client.requests.post")
    def test_stale_retry_cannot_bypass_failed_marker_release(self, post):
        self.client.session_id = "ses_old"

        def damage_marker_before_404(*_args, **_kwargs):
            os.unlink(self.marker_path)
            return FakeResponse(status_code=404)

        post.side_effect = damage_marker_before_404

        with self.assertRaisesRegex(OpenCodeError, "restart the AxonOS session"):
            self.client.send_message("work", "qwen3.8:latest")

        prompt_calls = [
            call for call in post.call_args_list if call.args[0].endswith("/prompt_async")
        ]
        self.assertEqual(len(prompt_calls), 1)

    @patch("opencode_client.requests.post")
    def test_final_reconciliation_joins_all_root_assistant_messages(self, post):
        self.client.session_id = "ses_1"
        post.return_value = FakeResponse(status_code=204)

        def reconciled_messages(_session_id):
            message_id = post.call_args.kwargs["json"]["messageID"]
            return [
                {"info": {"id": message_id, "role": "user"}, "parts": []},
                {
                    "info": {"id": "asst_1", "role": "assistant", "parentID": message_id},
                    "parts": [{"type": "text", "text": "first answer"}],
                },
                {
                    "info": {"id": "child_asst", "role": "assistant", "parentID": "child_user"},
                    "parts": [{"type": "text", "text": "child output"}],
                },
                {
                    "info": {"id": "asst_2", "role": "assistant", "parentID": message_id},
                    "parts": [{"type": "text", "text": "second answer"}],
                },
            ]

        with patch.object(self.client, "_get_session_status", return_value={"type": "idle"}), \
                patch.object(self.client, "_get_messages", side_effect=reconciled_messages):
            result = self.client.send_message("do both", "qwen3.8:latest")

        self.assertEqual(result, "first answer\n\nsecond answer")

    @patch("opencode_client.requests.post")
    def test_execution_marker_exists_before_async_prompt_dispatch(self, post):
        self.client.session_id = "ses_1"
        marker_observed = []

        def dispatch(*_args, **_kwargs):
            marker_observed.append(os.path.exists(self.marker_path))
            return FakeResponse(status_code=204)

        post.side_effect = dispatch

        def messages(_session_id):
            message_id = post.call_args.kwargs["json"]["messageID"]
            return [
                {"info": {"id": message_id, "role": "user"}, "parts": []},
                {
                    "info": {"id": "asst_1", "role": "assistant", "parentID": message_id},
                    "parts": [{"type": "text", "text": "done"}],
                },
            ]

        with patch.object(self.client, "_get_session_status", return_value={"type": "idle"}), \
                patch.object(self.client, "_get_messages", side_effect=messages):
            self.assertEqual(self.client.send_message("work", "qwen3.8:latest"), "done")

        self.assertEqual(marker_observed, [True])
        self.assertFalse(os.path.exists(self.marker_path))

    def test_expected_token_rejects_worker_delayed_past_cancellation(self):
        expected = self.client.cancellation_token()
        cancellation = self.client.begin_cancel()
        self.client.finish_cancel(cancellation)

        with patch("opencode_client.requests.post") as post:
            with self.assertRaisesRegex(OpenCodeError, "cancelled before dispatch"):
                self.client.send_message(
                    "stale request", "qwen3.8:latest", expected_cancel_epoch=expected,
                )
        post.assert_not_called()

    def test_live_marker_serializes_a_second_assistant_process(self):
        lease = self.client._claim_turn_marker("opencode")
        other = OpenCodeClient(
            "http://127.0.0.1:4096",
            "/home/aXonian",
            marker_path=self.marker_path,
            marker_wait_timeout=0.02,
        )

        with self.assertRaisesRegex(OpenCodeError, "active|cleanup"):
            other.wait_until_ready(other.cancellation_token())

        self.assertTrue(self.client._release_turn_marker(lease))
        self.assertEqual(
            other.wait_until_ready(other.cancellation_token()),
            other.cancellation_token(),
        )

    def test_crashed_opencode_owner_leaves_cross_process_poison(self):
        lease = self.client._claim_turn_marker("opencode")
        self.client._poison_turn_marker(lease)
        relaunched = OpenCodeClient(
            "http://127.0.0.1:4096",
            "/home/aXonian",
            marker_path=self.marker_path,
            marker_wait_timeout=0.02,
        )

        with self.assertRaisesRegex(OpenCodeError, "restart the AxonOS session"):
            relaunched.wait_until_ready(relaunched.cancellation_token())
        self.assertTrue(os.path.exists(self.marker_path))

    def test_failed_opencode_release_persists_cross_process_poison(self):
        lease = self.client._claim_turn_marker("opencode")
        os.unlink(self.marker_path)

        self.assertFalse(self.client._release_turn_marker(lease))
        with open(self.marker_path, encoding="utf-8") as marker_file:
            self.assertEqual(json.load(marker_file)["kind"], "opencode")

        relaunched = OpenCodeClient(
            "http://127.0.0.1:4096",
            "/home/aXonian",
            marker_path=self.marker_path,
            marker_wait_timeout=0.02,
        )
        with self.assertRaisesRegex(OpenCodeError, "restart the AxonOS session"):
            relaunched.wait_until_ready(relaunched.cancellation_token())

    def test_crashed_tool_free_owner_marker_is_safe_to_reap(self):
        lease = self.client._claim_turn_marker("direct")
        self.client._poison_turn_marker(lease)
        relaunched = OpenCodeClient(
            "http://127.0.0.1:4096",
            "/home/aXonian",
            marker_path=self.marker_path,
            marker_wait_timeout=0.02,
        )

        self.assertEqual(
            relaunched.wait_until_ready(relaunched.cancellation_token()),
            relaunched.cancellation_token(),
        )
        self.assertFalse(os.path.exists(self.marker_path))

    def test_tool_free_turn_uses_and_releases_shared_marker(self):
        expected = self.client.cancellation_token()
        lease = self.client.begin_local_turn(expected)
        self.assertEqual(self.client._marker_state(), "active")

        self.assertTrue(self.client.finish_local_turn(lease))
        self.assertEqual(self.client._marker_state(), "clear")

    @patch("opencode_client.requests.delete")
    @patch("opencode_client.requests.post")
    def test_cancel_during_session_creation_never_publishes_orphan(self, post, delete):
        entered = threading.Event()
        release = threading.Event()

        def delayed_create(*_args, **_kwargs):
            entered.set()
            release.wait(1)
            return FakeResponse({"id": "ses_orphan"})

        post.side_effect = delayed_create
        delete.return_value = FakeResponse(True)
        expected = self.client.cancellation_token()
        outcome = {}

        def send():
            try:
                self.client.send_message(
                    "stale", "qwen3.8:latest", expected_cancel_epoch=expected,
                )
            except Exception as exc:
                outcome["error"] = exc

        worker = threading.Thread(target=send)
        worker.start()
        self.assertTrue(entered.wait(1))
        cancellation = self.client.begin_cancel(detach_session=True)
        self.client.finish_cancel(cancellation, delete_session=True)
        release.set()
        worker.join(1)

        self.assertFalse(worker.is_alive())
        self.assertIsNone(self.client.session_id)
        self.assertIsInstance(outcome.get("error"), OpenCodeError)
        self.assertTrue(delete.call_args.args[0].endswith("/session/ses_orphan"))

    def test_noop_abort_before_runner_liveness_fails_closed(self):
        state = _TurnState("ses_uncertain", "msg_uncertain", self.client.cancellation_token())
        state.dispatch_attempted = True
        state.dispatch_done.set()
        self.client.session_id = state.session_id
        state.marker_lease = self.client._claim_turn_marker("opencode")
        user_only = [{"info": {"id": state.message_id, "role": "user"}, "parts": []}]

        with patch.object(self.client, "_get_session_status", return_value={"type": "idle"}), \
                patch.object(self.client, "_post_abort", return_value=True), \
                patch.object(self.client, "_get_messages", return_value=user_only), \
                patch.object(self.client, "_delete_session", return_value=True) as delete:
            safe = self.client._settle_cancelled_turn(state, timeout=0.02)

        self.assertFalse(safe)
        self.assertEqual(self.client.session_id, "ses_uncertain")
        self.assertEqual(self.client._marker_state(), "poisoned")
        delete.assert_not_called()

    def test_cancel_status_read_cannot_retroactively_validate_earlier_abort(self):
        state = _TurnState("ses_racy", "msg_racy", self.client.cancellation_token())
        state.dispatch_attempted = True
        state.dispatch_done.set()
        state.marker_lease = self.client._claim_turn_marker("opencode")
        user_only = [{"info": {"id": state.message_id, "role": "user"}, "parts": []}]
        abort_calls = 0

        def abort_with_late_start(_session_id):
            nonlocal abort_calls
            abort_calls += 1
            if abort_calls == 1:
                with state.lock:
                    state.live = True
            return True

        with patch.object(self.client, "_get_session_status", return_value={"type": "idle"}), \
                patch.object(self.client, "_post_abort", side_effect=abort_with_late_start), \
                patch.object(self.client, "_get_messages", return_value=user_only):
            safe = self.client._settle_cancelled_turn(state, timeout=0.5)

        self.assertTrue(safe)
        self.assertGreaterEqual(abort_calls, 2)
        self.assertEqual(self.client._marker_state(), "clear")

    def test_session_disappearance_during_cancel_fails_closed(self):
        state = _TurnState("ses_deleted", "msg_1", self.client.cancellation_token())
        state.dispatch_attempted = True
        state.dispatch_done.set()
        self.client.session_id = state.session_id

        with patch.object(
            self.client, "_get_messages", side_effect=OpenCodeSessionExpired("gone"),
        ), patch.object(self.client, "_delete_session") as delete:
            self.assertFalse(self.client._settle_cancelled_turn(state, timeout=0.1))

        self.assertEqual(self.client.session_id, "ses_deleted")
        delete.assert_not_called()

    def test_terminal_proof_wins_over_failed_cancel_reconciliation(self):
        state = _TurnState("ses_done", "msg_done", self.client.cancellation_token())
        state.dispatch_attempted = True
        state.dispatch_done.set()
        state.terminal_safe = True
        state.marker_lease = self.client._claim_turn_marker("opencode")

        with patch.object(
            self.client, "_get_messages", side_effect=OpenCodeSessionExpired("gone"),
        ) as messages:
            self.assertTrue(self.client._settle_cancelled_turn(state, timeout=0.1))

        messages.assert_not_called()
        self.assertTrue(state.cleanup_safe)
        self.assertEqual(self.client._marker_state(), "clear")

    @patch("opencode_client.requests.post")
    def test_sender_keeps_turn_for_delayed_stop_then_reset(self, post):
        self.client.session_id = "ses_1"
        post.return_value = FakeResponse(status_code=204)
        state_ready = threading.Event()
        captured = {}

        def wait_for_cancel(state, _timeout):
            captured["state"] = state
            state_ready.set()
            self.assertTrue(state.cancel_requested.wait(1))
            raise OpenCodeError("The OpenCode turn was cancelled")

        outcome = {}

        def send():
            try:
                self.client.send_message("work", "qwen3.8:latest")
            except Exception as exc:
                outcome["error"] = exc

        with patch.object(self.client, "_wait_for_completion", side_effect=wait_for_cancel):
            worker = threading.Thread(target=send)
            worker.start()
            self.assertTrue(state_ready.wait(1))
            cancellation = self.client.begin_cancel()
            worker.join(1)

        self.assertFalse(worker.is_alive())
        self.assertIsInstance(outcome.get("error"), OpenCodeError)
        self.assertEqual(self.client._marker_state(), "active")
        self.assertIs(self.client._active_turn, captured["state"])

        reset = self.client.begin_cancel(detach_session=True)
        self.assertIs(reset["turn"], captured["state"])
        with captured["state"].lock:
            captured["state"].terminal_safe = True
        with patch.object(self.client, "_delete_session", return_value=True) as delete:
            self.assertTrue(self.client.finish_cancel(reset, delete_session=True))
            self.assertTrue(self.client.finish_cancel(cancellation))

        delete.assert_called_once_with("ses_1", tolerate_missing=True)
        self.assertEqual(self.client._marker_state(), "clear")

    def test_cancellation_barrier_waits_for_every_cleanup_ticket(self):
        first = self.client.begin_cancel()
        second = self.client.begin_cancel()
        self.assertIs(first["barrier"], second["barrier"])

        self.client.finish_cancel(first)
        self.assertFalse(first["barrier"].is_set())

        finished = threading.Event()
        result = {}
        expected = self.client.cancellation_token()

        def wait_for_barrier():
            result["epoch"] = self.client._await_cancellation_barrier(expected)
            finished.set()

        waiter = threading.Thread(target=wait_for_barrier)
        waiter.start()
        self.assertFalse(finished.wait(0.05))
        self.client.finish_cancel(second)
        self.assertTrue(finished.wait(1))
        waiter.join(1)
        self.assertEqual(result["epoch"], expected)

    def test_duplicate_ticket_does_not_resettle_completed_turn(self):
        state = _TurnState("ses_1", "msg_1", self.client.cancellation_token())
        self.client.session_id = state.session_id
        self.client._active_turn = state
        first = self.client.begin_cancel()
        second = self.client.begin_cancel()

        with patch.object(self.client, "_settle_cancelled_turn", return_value=True) as settle:
            def mark_complete(_state):
                _state.cancel_complete.set()
                return True

            settle.side_effect = mark_complete
            self.client.finish_cancel(first)
            self.client.finish_cancel(second)

        settle.assert_called_once_with(state)

    def test_mismatched_ticket_cannot_mark_another_turn_cancelled(self):
        state = _TurnState("ses_1", "msg_1", self.client.cancellation_token())
        self.client.session_id = state.session_id
        self.client._active_turn = state
        reset_ticket = self.client.begin_cancel(detach_session=True)
        mismatched_ticket = self.client.begin_cancel()

        self.client.finish_cancel(mismatched_ticket)
        self.assertFalse(state.cancel_complete.is_set())

        with patch.object(self.client, "_settle_cancelled_turn", return_value=True) as settle:
            def mark_complete(_state):
                _state.cancel_complete.set()
                return True

            settle.side_effect = mark_complete
            self.client.finish_cancel(reset_ticket)

        settle.assert_called_once_with(state)

    def test_failed_hard_cleanup_blocks_future_dispatch(self):
        state = _TurnState("ses_1", "msg_1", self.client.cancellation_token())
        self.client.session_id = state.session_id
        self.client._active_turn = state
        cancellation = self.client.begin_cancel(detach_session=True)

        with patch.object(self.client, "_settle_cancelled_turn", return_value=False), \
                patch.object(self.client, "_delete_session") as delete:
            self.assertFalse(self.client.finish_cancel(cancellation, delete_session=True))

        delete.assert_not_called()

        with self.assertRaisesRegex(OpenCodeError, "could not be proven safe"):
            self.client.wait_until_ready(self.client.cancellation_token())

    def test_overlapping_reset_cannot_delete_after_failed_stop_cleanup(self):
        state = _TurnState("ses_1", "msg_1", self.client.cancellation_token())
        self.client.session_id = state.session_id
        self.client._active_turn = state
        stop_ticket = self.client.begin_cancel()
        reset_ticket = self.client.begin_cancel(detach_session=True)

        def failed_cleanup(_state):
            _state.cancel_complete.set()
            return False

        with patch.object(
            self.client, "_settle_cancelled_turn", side_effect=failed_cleanup,
        ) as settle, \
                patch.object(self.client, "_delete_session") as delete:
            self.assertFalse(self.client.finish_cancel(stop_ticket))
            self.assertFalse(self.client.finish_cancel(reset_ticket, delete_session=True))

        settle.assert_called_once_with(state)
        delete.assert_not_called()
        self.assertTrue(reset_ticket["barrier"].is_set())

    def test_user_seen_without_live_runner_does_not_complete_turn(self):
        state = _TurnState("ses_1", "msg_user", self.client.cancellation_token())
        state.armed = True
        first_reconciliation = threading.Event()
        allow_assistant = threading.Event()

        def reconciled_messages(_session_id):
            messages = [
                {"info": {"id": "msg_user", "role": "user"}, "parts": []},
            ]
            if allow_assistant.is_set():
                messages.append({
                    "info": {"id": "asst_1", "role": "assistant", "parentID": "msg_user"},
                    "parts": [{"type": "text", "text": "done"}],
                })
            first_reconciliation.set()
            return messages

        outcome = {}

        def wait_for_completion():
            try:
                outcome["result"] = self.client._wait_for_completion(state, 2)
            except Exception as exc:  # pragma: no cover - retained for useful failure output
                outcome["error"] = exc

        with patch.object(self.client, "_get_session_status", return_value={"type": "idle"}), \
                patch.object(self.client, "_get_messages", side_effect=reconciled_messages):
            waiter = threading.Thread(target=wait_for_completion)
            waiter.start()
            self.assertTrue(first_reconciliation.wait(1))
            time.sleep(0.02)
            self.assertTrue(waiter.is_alive(), "user persistence alone completed the turn")
            allow_assistant.set()
            state.updated.set()
            waiter.join(1)

        self.assertFalse(waiter.is_alive())
        self.assertNotIn("error", outcome)
        self.assertEqual(outcome.get("result"), "done")

    def test_status_idle_cannot_complete_when_liveness_arrives_mid_read(self):
        state = _TurnState("ses_1", "msg_race", self.client.cancellation_token())
        state.armed = True
        user = {"info": {"id": state.message_id, "role": "user"}, "parts": []}
        assistant = {
            "info": {"id": "asst_1", "role": "assistant", "parentID": state.message_id},
            "parts": [{"type": "text", "text": "ordered"}],
        }
        message_calls = 0
        status_calls = 0

        def messages(_session_id):
            nonlocal message_calls
            message_calls += 1
            return [user] if message_calls == 1 else [user, assistant]

        def stale_idle(_session_id):
            nonlocal status_calls
            status_calls += 1
            if status_calls == 1:
                with state.lock:
                    state.live = True
            return {"type": "idle"}

        with patch.object(self.client, "_get_session_status", side_effect=stale_idle), \
                patch.object(self.client, "_get_messages", side_effect=messages):
            result = self.client._wait_for_completion(state, 1)

        self.assertEqual(result, "ordered")
        self.assertGreaterEqual(status_calls, 2)

    def test_persisted_assistant_error_cannot_be_reconciled_as_success(self):
        state = _TurnState("ses_1", "msg_error", self.client.cancellation_token())
        state.armed = True
        messages = [
            {"info": {"id": state.message_id, "role": "user"}, "parts": []},
            {
                "info": {
                    "id": "asst_error",
                    "role": "assistant",
                    "parentID": state.message_id,
                    "error": {"name": "ProviderError", "message": "boom"},
                },
                "parts": [{"type": "text", "text": "partial answer"}],
            },
        ]

        with patch.object(self.client, "_get_session_status", return_value={"type": "idle"}), \
                patch.object(self.client, "_get_messages", return_value=messages):
            with self.assertRaisesRegex(OpenCodeError, "ProviderError"):
                self.client._wait_for_completion(state, 1)

    def test_cancelled_stale_turn_is_not_retried(self):
        def cancelled_turn(*_args, **_kwargs):
            self.client.abort()
            raise OpenCodeSessionExpired("gone")

        with patch.object(self.client, "_send_message", side_effect=cancelled_turn) as send:
            with self.assertRaises(OpenCodeError):
                self.client.send_message("hello", "qwen3.8:latest")

        self.assertEqual(send.call_count, 1)

    @patch("opencode_client.requests.delete")
    def test_reset_detaches_and_deletes_idle_session(self, delete):
        self.client.session_id = "ses_old"
        delete.return_value = FakeResponse(True)

        self.client.reset_session()

        self.assertIsNone(self.client.session_id)
        self.assertTrue(delete.call_args.args[0].endswith("/session/ses_old"))

    @patch("opencode_client.requests.post")
    def test_permission_reply_uses_current_endpoint_and_payload(self, post):
        post.return_value = FakeResponse(True)
        permission = {"id": "per_1", "sessionID": "ses_1"}

        self.assertTrue(self.client.reply_permission(permission, "once"))

        self.assertEqual(post.call_args.args[0], "http://127.0.0.1:4096/permission/per_1/reply")
        self.assertEqual(post.call_args.kwargs["json"], {"reply": "once"})
        self.assertEqual(post.call_args.kwargs["params"], {"directory": "/home/aXonian"})

    def test_permission_reply_rejects_unknown_decision(self):
        with self.assertRaises(OpenCodeError):
            self.client.reply_permission({"id": "per_1", "sessionID": "ses_1"}, "yes")

    @patch("opencode_client.requests.post")
    def test_question_reply_shape(self, post):
        post.return_value = FakeResponse(True)
        question = {"id": "que_1", "sessionID": "ses_1"}

        self.client.reply_question(question, [["Option A"], ["custom"]])

        self.assertEqual(post.call_args.args[0], "http://127.0.0.1:4096/question/que_1/reply")
        self.assertEqual(post.call_args.kwargs["json"], {"answers": [["Option A"], ["custom"]]})


if __name__ == "__main__":
    unittest.main()
