import json
import time
import unittest
from unittest.mock import MagicMock, Mock, patch

from axonos_gate.webrtc import service


WALLET = "0x1234567890123456789012345678901234567890"
OTHER_WALLET = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
SHARED_AGENT_KEY = "shared-agent-secret"


class ResolveAgentScopeTests(unittest.TestCase):
    def test_resolves_only_the_validator_trusted_identity(self):
        validator = Mock(return_value={"id": 73, "wallet_address": WALLET})

        scope = service.resolve_agent_scope(
            "73",
            f"  {WALLET.upper()}  ",
            " session-secret ",
            validator,
        )

        self.assertEqual(
            scope,
            service.AgentScope(compute_session_id=73, wallet_address=WALLET),
        )
        validator.assert_called_once_with(73, WALLET, "session-secret")

    def test_missing_or_invalid_untrusted_identity_never_calls_validator(self):
        invalid_inputs = (
            (None, WALLET, "session-secret"),
            ("", WALLET, "session-secret"),
            ("not-numeric", WALLET, "session-secret"),
            (0, WALLET, "session-secret"),
            (73, "", "session-secret"),
            (73, WALLET, ""),
        )
        for compute_id, wallet, key in invalid_inputs:
            with self.subTest(compute_id=compute_id, wallet=wallet, key=key):
                validator = Mock()
                self.assertIsNone(
                    service.resolve_agent_scope(compute_id, wallet, key, validator)
                )
                validator.assert_not_called()

        self.assertIsNone(
            service.resolve_agent_scope(73, WALLET, "session-secret", None)
        )

    def test_rejects_validator_identity_mismatch_or_failure(self):
        mismatches = (
            {"id": 74, "wallet_address": WALLET},
            {"id": 73, "wallet_address": OTHER_WALLET},
            True,
            None,
        )
        for trusted in mismatches:
            with self.subTest(trusted=trusted):
                validator = Mock(return_value=trusted)
                self.assertIsNone(
                    service.resolve_agent_scope(
                        73, WALLET, "session-secret", validator
                    )
                )

        validator = Mock(side_effect=RuntimeError("database unavailable"))
        with self.assertLogs(service.logger, level="ERROR"):
            scope = service.resolve_agent_scope(
                73, WALLET, "session-secret", validator
            )
        self.assertIsNone(scope)


class WebrtcServiceScopeTests(unittest.TestCase):
    def setUp(self):
        self.config = MagicMock(spec=service.config)
        self.store = MagicMock(spec=service.store)
        self.metrics = MagicMock(spec=service.metrics)

        self.config.webrtc_enabled.return_value = True
        self.config.agent_internal_key.return_value = SHARED_AGENT_KEY
        self.config.validate_sdp.return_value = True
        self.config.ice_candidate_list_from_body.return_value = []
        self.config.ice_servers_for_client.return_value = []
        self.config.session_timeout_seconds.return_value = 600
        self.config.max_reconnect_attempts.return_value = 5

        self.store.ensure_table.return_value = True
        self.store.create_session.return_value = "signal-created"
        self.store.set_offer.return_value = True
        self.store.append_client_ice.return_value = True
        self.store.set_answer.return_value = True
        self.store.append_server_ice.return_value = True
        self.store.mark_failed.return_value = True

        patchers = (
            patch.object(service, "config", self.config),
            patch.object(service, "store", self.store),
            patch.object(service, "metrics", self.metrics),
        )
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

        self.scope = service.AgentScope(
            compute_session_id=73,
            wallet_address=WALLET,
        )
        self.store.reset_mock()
        self.metrics.reset_mock()

    def assert_no_store_calls(self):
        self.assertEqual(self.store.mock_calls, [])

    def test_shared_key_without_session_scope_rejects_all_agent_operations(self):
        calls = (
            lambda: service.handle_agent_next(SHARED_AGENT_KEY, None),
            lambda: service.handle_agent_row(SHARED_AGENT_KEY, None, "signal-b"),
            lambda: service.handle_agent_answer(
                SHARED_AGENT_KEY,
                None,
                {"session_id": "signal-b", "sdp": "v=0", "type": "answer"},
            ),
            lambda: service.handle_agent_fail(
                SHARED_AGENT_KEY,
                None,
                {"session_id": "signal-b", "error": "failed"},
            ),
        )
        for call in calls:
            with self.subTest(call=call):
                self.store.reset_mock()
                status, payload = call()
                self.assertEqual(status, 403)
                self.assertEqual(payload.get("error"), "Forbidden")
                self.assert_no_store_calls()

    def test_missing_shared_key_rejects_valid_scope_without_store_access(self):
        calls = (
            lambda: service.handle_agent_next("", self.scope),
            lambda: service.handle_agent_row("", self.scope, "signal-a"),
            lambda: service.handle_agent_answer(
                "",
                self.scope,
                {"session_id": "signal-a", "sdp": "v=0", "type": "answer"},
            ),
            lambda: service.handle_agent_fail(
                "",
                self.scope,
                {"session_id": "signal-a", "error": "failed"},
            ),
        )
        for call in calls:
            with self.subTest(call=call):
                self.store.reset_mock()
                status, payload = call()
                self.assertEqual(status, 403)
                self.assertEqual(payload.get("error"), "Forbidden")
                self.assert_no_store_calls()

    def test_agent_next_claim_is_scoped_to_compute_and_wallet(self):
        job = {
            "session_id": "signal-a",
            "compute_session_id": 73,
            "wallet_address": WALLET,
            "offer_sdp": "v=0",
            "offer_type": "offer",
        }
        self.store.fetch_next_pending_offer_for_agent.return_value = job

        status, payload = service.handle_agent_next(
            SHARED_AGENT_KEY, self.scope
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload, job)
        self.store.fetch_next_pending_offer_for_agent.assert_called_once_with(
            73, WALLET
        )

    def test_agent_row_read_is_scoped_to_compute_and_wallet(self):
        candidate = {"candidate": "candidate:1", "sdpMid": "0"}
        self.store.get_row_for_agent.return_value = {
            "state": "agent_processing",
            "client_ice": json.dumps([candidate]),
            "expires_at": 2000.0,
        }

        status, payload = service.handle_agent_row(
            SHARED_AGENT_KEY, self.scope, "signal-a"
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["client_ice"], [candidate])
        self.store.get_row_for_agent.assert_called_once_with(
            "signal-a", 73, WALLET
        )
        self.store.get_row.assert_not_called()

    def test_agent_answer_writes_answer_and_ice_inside_scope(self):
        candidates = [{"candidate": "candidate:server", "sdpMid": "0"}]
        self.config.ice_candidate_list_from_body.return_value = candidates
        body = {
            "session_id": "signal-a",
            "sdp": "v=0\r\nanswer",
            "type": "answer",
            "server_ice": candidates,
        }

        status, payload = service.handle_agent_answer(
            SHARED_AGENT_KEY, self.scope, body
        )

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.store.set_answer.assert_called_once_with(
            "signal-a", 73, WALLET, "v=0\r\nanswer", "answer"
        )
        self.store.append_server_ice.assert_called_once_with(
            "signal-a", 73, WALLET, candidates
        )

    def test_agent_fail_marks_only_the_authenticated_scope(self):
        status, payload = service.handle_agent_fail(
            SHARED_AGENT_KEY,
            self.scope,
            {"session_id": "signal-a", "error": "capture_failed"},
        )

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.store.mark_failed.assert_called_once_with(
            "signal-a", 73, WALLET, "capture_failed"
        )

    def test_invalid_answer_cannot_mark_a_row_outside_authenticated_scope(self):
        self.config.validate_sdp.return_value = False
        # A scoped store rejects this signaling ID if it belongs to another
        # compute session; the service must never retry with an unscoped write.
        self.store.mark_failed.return_value = False

        status, payload = service.handle_agent_answer(
            SHARED_AGENT_KEY,
            self.scope,
            {"session_id": "signal-b", "sdp": "invalid", "type": "answer"},
        )

        self.assertEqual(status, 400)
        self.assertEqual(payload.get("error"), "Invalid answer")
        self.store.mark_failed.assert_called_once_with(
            "signal-b", 73, WALLET, "invalid_answer_sdp"
        )
        self.store.set_answer.assert_not_called()
        self.store.get_row.assert_not_called()

    def test_browser_compute_mismatch_rejects_create_offer_and_ice_before_store(self):
        calls = (
            lambda: service.handle_create_session(WALLET, True, 73, 74),
            lambda: service.handle_post_offer(
                "signal-old",
                WALLET,
                True,
                73,
                {
                    "compute_session_id": 74,
                    "sdp": "v=0\r\noffer",
                    "type": "offer",
                },
            ),
            lambda: service.handle_post_client_ice(
                "signal-old",
                WALLET,
                True,
                73,
                {
                    "compute_session_id": 74,
                    "candidate": "candidate:1",
                },
            ),
            lambda: service.handle_get_status(
                "signal-old",
                WALLET,
                True,
                73,
                74,
            ),
        )
        for call in calls:
            with self.subTest(call=call):
                self.store.reset_mock()
                status, payload = call()
                self.assertEqual(status, 409)
                self.assertIn("session changed", payload.get("error", "").lower())
                self.assert_no_store_calls()

    def test_same_wallet_old_compute_binding_rejects_offer_and_ice_mutations(self):
        stale_row = {
            "id": "signal-old",
            "wallet_address": WALLET,
            "compute_session_id": 73,
            "expires_at": 9999.0,
        }
        self.store.get_row.return_value = stale_row

        status, payload = service.handle_post_offer(
            "signal-old",
            WALLET,
            True,
            74,
            {
                "compute_session_id": 74,
                "sdp": "v=0\r\noffer",
                "type": "offer",
            },
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload.get("error"), "Invalid session")
        self.store.get_row.assert_called_once_with("signal-old")
        self.store.set_offer.assert_not_called()

        self.store.reset_mock()
        self.store.get_row.return_value = stale_row
        status, payload = service.handle_post_client_ice(
            "signal-old",
            WALLET,
            True,
            74,
            {
                "compute_session_id": 74,
                "candidate": "candidate:1",
            },
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload.get("error"), "Invalid session")
        self.store.get_row.assert_called_once_with("signal-old")
        self.store.append_client_ice.assert_not_called()

    def test_status_rejects_ended_compute_before_reading_stale_row(self):
        status, payload = service.handle_get_status(
            "signal-old",
            WALLET,
            True,
            None,
            73,
        )

        self.assertEqual(status, 410)
        self.assertEqual(payload.get("state"), "closed")
        self.assert_no_store_calls()

    def test_status_requires_current_compute_binding(self):
        self.store.get_row.return_value = {
            "id": "signal-a",
            "wallet_address": WALLET,
            "compute_session_id": 73,
            "expires_at": time.time() + 60,
            "state": "offer_received",
        }

        status, payload = service.handle_get_status(
            "signal-a",
            WALLET,
            True,
            73,
            73,
        )

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.store.get_row.assert_called_once_with("signal-a")


if __name__ == "__main__":
    unittest.main()
