import os
import sys
import time
import unittest
from unittest.mock import call, patch, MagicMock

_tests_dir = os.path.dirname(os.path.abspath(__file__))
_pkg_dir = os.path.dirname(_tests_dir)
_repo_root = os.path.dirname(_pkg_dir)
if _pkg_dir not in sys.path:
    sys.path.insert(0, _pkg_dir)
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

class TestPaidSessionExpiry(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "AXGT_CHALLENGE_DB_URL": "postgresql://test/test",
            "AXGT_SESSION_GRACE_SECONDS": "60",
            "AXGT_HEARTBEAT_TIMEOUT_SECONDS": "120",
        })
        self.env.start()

        # Patch deposit_ledger methods in both flat and package names
        self.patches = [
            patch("deposit_ledger.init_once", return_value=True),
            patch("axonos_gate.deposit_ledger.init_once", return_value=True),
            patch("deposit_ledger.get_remaining_minutes", return_value=10.0),
            patch("axonos_gate.deposit_ledger.get_remaining_minutes", return_value=10.0),
            patch("deposit_ledger.record_session_expiry"),
            patch("axonos_gate.deposit_ledger.record_session_expiry"),
            patch(
                "deposit_ledger._deduct_usage_on_cursor",
                return_value=(True, 9.0, None),
            ),
            patch(
                "axonos_gate.deposit_ledger._deduct_usage_on_cursor",
                return_value=(True, 9.0, None),
            ),
        ]
        for p in self.patches:
            p.start()
        
    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.env.stop()

    @patch("session_manager._get_connection")
    @patch("axonos_gate.session_manager._get_connection")
    @patch("session_launcher.list_running_sessions", return_value=[197])
    @patch("axonos_gate.session_launcher.list_running_sessions", return_value=[197])
    @patch("session_launcher.stop_session")
    @patch("axonos_gate.session_launcher.stop_session")
    def test_expired_active_session_marked_ended_and_stopped(self, mock_stop2, mock_stop1, mock_list2, mock_list1, mock_conn2, mock_conn1):
        from axonos_gate import session_manager
        session_manager._pg_init_done = True
        
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__.return_value = cur
        conn.cursor.return_value = cur
        mock_conn1.return_value = conn
        mock_conn2.return_value = conn
        
        now = time.time()
        cur.fetchone.side_effect = [
            (True,),  # pg_try_advisory_xact_lock
            ("active", now - 100, "0xwallet"), # reconcile SELECT for 197
        ]
        cur.fetchall.side_effect = [
            [("0xwallet", 197, now - 120, now - 300, "small", "0")],
            [],  # _expire_stale_paused_sessions
            [],  # paused runtime reconciliation
        ]
        
        session_manager.perform_session_cleanup()
        self.assertTrue(mock_stop1.called or mock_stop2.called)
        
    @patch("session_manager._get_connection")
    @patch("axonos_gate.session_manager._get_connection")
    @patch("session_launcher.list_running_sessions", return_value=[197])
    @patch("axonos_gate.session_launcher.list_running_sessions", return_value=[197])
    @patch("session_launcher.stop_session")
    @patch("axonos_gate.session_launcher.stop_session")
    def test_ended_session_still_running_container_stopped(self, mock_stop2, mock_stop1, mock_list2, mock_list1, mock_conn2, mock_conn1):
        from axonos_gate import session_manager
        session_manager._pg_init_done = True
        
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__.return_value = cur
        conn.cursor.return_value = cur
        mock_conn1.return_value = conn
        mock_conn2.return_value = conn
        
        cur.fetchone.side_effect = [
            (True,),  # pg_try_advisory_xact_lock
            ("ended", time.time() - 100, "0xwallet"), # reconcile SELECT for 197
        ]
        cur.fetchall.side_effect = [
            [],  # _pause_stale_zero_credit_sessions
            [],  # _expire_stale_paused_sessions
            [],  # paused runtime reconciliation
        ]
        
        session_manager.perform_session_cleanup()
        self.assertTrue(mock_stop1.called or mock_stop2.called)
        if mock_stop1.called:
            mock_stop1.assert_called_with(session_id=197, container_id=None)
        else:
            mock_stop2.assert_called_with(session_id=197, container_id=None)

    @patch("session_manager._get_connection")
    @patch("axonos_gate.session_manager._get_connection")
    @patch("session_launcher.list_running_sessions", return_value=[197])
    @patch("axonos_gate.session_launcher.list_running_sessions", return_value=[197])
    @patch("session_launcher.stop_session")
    @patch("axonos_gate.session_launcher.stop_session")
    def test_reconciliation_retry_on_subsequent_cleanup_cycles(self, mock_stop2, mock_stop1, mock_list2, mock_list1, mock_conn2, mock_conn1):
        from axonos_gate import session_manager
        session_manager._pg_init_done = True
        
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__.return_value = cur
        conn.cursor.return_value = cur
        mock_conn1.return_value = conn
        mock_conn2.return_value = conn
        
        for _ in range(3):
            cur.fetchone.side_effect = [
                (True,),  # pg_try_advisory_xact_lock
                ("ended", time.time() - 100, "0xwallet"), # reconcile SELECT
            ]
            cur.fetchall.side_effect = [
                [],  # _pause_stale_zero_credit_sessions
                [],  # _expire_stale_paused_sessions
                [],  # paused runtime reconciliation
            ]
            session_manager.perform_session_cleanup()
            
        self.assertEqual(mock_stop1.call_count + mock_stop2.call_count, 3)

    @patch("session_manager._get_connection")
    @patch("axonos_gate.session_manager._get_connection")
    @patch("session_launcher.list_running_sessions", return_value=[197])
    @patch("axonos_gate.session_launcher.list_running_sessions", return_value=[197])
    @patch("session_launcher.stop_session")
    @patch("axonos_gate.session_launcher.stop_session")
    def test_non_existent_session_in_db_but_running_container_stopped(self, mock_stop2, mock_stop1, mock_list2, mock_list1, mock_conn2, mock_conn1):
        from axonos_gate import session_manager
        session_manager._pg_init_done = True
        
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__.return_value = cur
        conn.cursor.return_value = cur
        mock_conn1.return_value = conn
        mock_conn2.return_value = conn
        
        cur.fetchone.side_effect = [
            (True,),  # pg_try_advisory_xact_lock
            None,  # reconcile SELECT -> not found in DB
        ]
        cur.fetchall.side_effect = [
            [],  # _pause_stale_zero_credit_sessions
            [],  # _expire_stale_paused_sessions
            [],  # paused runtime reconciliation
        ]
        
        session_manager.perform_session_cleanup()
        self.assertTrue(mock_stop1.called or mock_stop2.called)
        if mock_stop1.called:
            mock_stop1.assert_called_with(session_id=197, container_id=None)
        else:
            mock_stop2.assert_called_with(session_id=197, container_id=None)

    @patch("session_manager._get_connection")
    @patch("axonos_gate.session_manager._get_connection")
    @patch("session_launcher.list_running_sessions", return_value=[197])
    @patch("axonos_gate.session_launcher.list_running_sessions", return_value=[197])
    @patch("session_launcher.stop_session")
    @patch("axonos_gate.session_launcher.stop_session")
    def test_non_expired_desktop_session_is_not_reclaimed(self, mock_stop2, mock_stop1, mock_list2, mock_list1, mock_conn2, mock_conn1):
        from axonos_gate import session_manager
        session_manager._pg_init_done = True
        
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__.return_value = cur
        conn.cursor.return_value = cur
        mock_conn1.return_value = conn
        mock_conn2.return_value = conn
        
        now = time.time()
        cur.fetchone.side_effect = [
            (True,),  # pg_try_advisory_xact_lock
            ("active", now + 1000, "0xwallet"), # reconcile SELECT -> active and NOT expired
        ]
        cur.fetchall.side_effect = [
            [],  # _pause_stale_zero_credit_sessions
            [],  # _expire_stale_paused_sessions
            [],  # paused runtime reconciliation
        ]
        
        session_manager.perform_session_cleanup()
        mock_stop1.assert_not_called()
        mock_stop2.assert_not_called()

    def test_cleanup_freezes_legacy_paused_runtime(self):
        from axonos_gate import session_manager

        session_manager._pg_init_done = True
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.fetchone.return_value = (True,)
        cur.fetchall.return_value = [
            ("0xwallet", 197, "axgt-session-197", "legacy")
        ]
        conn.cursor.return_value = cur

        with patch.object(session_manager, "_get_connection", return_value=conn), \
             patch.object(session_manager, "_expire_stale_session", return_value=(None, [])), \
             patch.object(session_manager, "_expire_stale_paused_sessions", return_value=[]), \
             patch.object(session_manager, "_reconcile_containers", return_value=([], [])), \
             patch.object(session_manager, "_new_transition_token", return_value="recovery-token"), \
             patch.object(session_manager, "_on_session_credit_paused", return_value=True) as mock_pause, \
             patch.object(session_manager, "_end_after_runtime_pause_failure") as mock_end:
            session_manager.perform_session_cleanup()

        mock_pause.assert_called_once_with(
            "0xwallet", 197, "axgt-session-197", "legacy", "recovery-token"
        )
        mock_end.assert_not_called()

    def test_cleanup_cas_claims_stale_resuming_transition_before_refreeze(self):
        from axonos_gate import session_manager

        session_manager._pg_init_done = True
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.fetchone.return_value = (True,)
        cur.fetchall.return_value = [
            ("0xwallet", 197, "axgt-session-197", "credit_exhausted")
        ]
        conn.cursor.return_value = cur

        with patch.object(session_manager, "_get_connection", return_value=conn), \
             patch.object(session_manager, "_expire_stale_session", return_value=(None, [])), \
             patch.object(session_manager, "_expire_stale_paused_sessions", return_value=[]), \
             patch.object(session_manager, "_reconcile_containers", return_value=([], [])), \
             patch.object(session_manager, "_lifecycle_transition_timeout_seconds", return_value=120), \
             patch.object(session_manager, "_new_transition_token", return_value="recovery-token"), \
             patch.object(session_manager.time, "time", return_value=2000.0), \
             patch.object(session_manager, "_on_session_credit_paused", return_value=True) as mock_pause, \
             patch.object(session_manager, "_restore_paused_transition") as mock_restore:
            session_manager.perform_session_cleanup()

        mock_pause.assert_called_once_with(
            "0xwallet", 197, "axgt-session-197", "credit_exhausted", "recovery-token"
        )
        mock_restore.assert_not_called()
        lifecycle_calls = [
            call for call in cur.execute.call_args_list
            if call.args and "COALESCE(transition_started_at" in str(call.args[0])
        ]
        self.assertEqual(len(lifecycle_calls), 1)
        lifecycle_sql, lifecycle_params = lifecycle_calls[0].args
        self.assertIn("SET status = 'pausing'", lifecycle_sql)
        self.assertEqual(
            lifecycle_params,
            (2000.0, 2000.0, "recovery-token", 1880.0),
        )

    def test_stale_expiry_settles_every_gpu_weighted_final_interval_atomically(self):
        from axonos_gate import session_manager

        cur = MagicMock()
        cur.fetchall.return_value = [
            ("0xbbb", 22, 940.0, 800.0, "medium", "0,1"),
            ("0xaaa", 11, 970.0, 850.0, "small", "2"),
        ]
        ledger = MagicMock()
        ledger.init_once.return_value = True
        ledger._deduct_usage_on_cursor.side_effect = [
            (True, 9.5, None),
            (True, 7.5, None),
        ]

        with patch.object(session_manager, "_heartbeat_timeout_seconds", return_value=120), \
             patch.object(session_manager, "session_grace_seconds", return_value=60), \
             patch.object(session_manager, "_import_deposit_ledger", return_value=ledger), \
             patch.dict(os.environ, {
                 "AXGT_GPU_PROFILES_ENABLED": "true",
                 "AXGT_GPU_WEIGHTED_BILLING": "true",
             }, clear=False):
            ended, paused = session_manager._expire_stale_session(cur, 1000.0)

        self.assertEqual(ended, [("0xaaa", 11), ("0xbbb", 22)])
        self.assertEqual(paused, [])
        ledger._deduct_usage_on_cursor.assert_has_calls([
            call(cur, "0xaaa", 0.5, session_id="11"),
            call(cur, "0xbbb", 2.0, session_id="22"),
        ])
        sql, params = cur.execute.call_args.args
        self.assertIn("FOR UPDATE", sql)
        self.assertIn("SET status = 'ended', last_billed_at = %s", sql)
        self.assertEqual(params, (880.0, 1000.0, 60, 1000.0, 1000.0))

    def test_stale_expiry_without_rows_does_not_touch_ledger(self):
        from axonos_gate import session_manager

        cur = MagicMock()
        cur.fetchall.return_value = []
        ledger = MagicMock()
        with patch.object(session_manager, "_import_deposit_ledger", return_value=ledger):
            ended, paused = session_manager._expire_stale_session(cur, 1000.0)

        self.assertEqual((ended, paused), ([], []))
        ledger.init_once.assert_not_called()
        ledger._deduct_usage_on_cursor.assert_not_called()

    def test_all_stale_rows_receive_post_commit_container_cleanup(self):
        from axonos_gate import session_manager

        with patch.object(session_manager, "_on_session_ended") as ended_hook:
            session_manager._apply_stale_session_maintenance(
                [("0xaaa", 11), ("0xbbb", 22)],
                [],
            )

        ended_hook.assert_has_calls([call("0xaaa", 11), call("0xbbb", 22)])

    @patch("gate_server.try_claim_session")
    @patch("axonos_gate.gate_server.try_claim_session")
    @patch("gate_server._issue_gate_auth_token")
    @patch("axonos_gate.gate_server._issue_gate_auth_token")
    def test_auth_token_ttl_issued_after_claim_succeeds(self, mock_issue2, mock_issue1, mock_claim2, mock_claim1):
        import flask
        import gate_server
        
        gate_server.app.testing = True
        client = gate_server.app.test_client()
        
        mock_claim1.return_value = {
            "granted": True,
            "remaining_seconds": 7200,
        }
        mock_claim2.return_value = {
            "granted": True,
            "remaining_seconds": 7200,
        }
        mock_issue1.return_value = ("fake_token", 7260)
        mock_issue2.return_value = ("fake_token", 7260)
        
        with patch.object(gate_server, "validate_ssh_public_key", return_value="ssh-ed25519 AAAA"), \
             patch.object(gate_server, "validate_wallet_address", return_value=True), \
             patch.object(gate_server, "get_wallet_access_status", return_value={"verified": True, "remaining_minutes": 120.0}):
            
            resp = client.post(
                "/api/x402/session",
                json={"wallet_address": "0x123", "ssh_pubkey": "ssh-ed25519 AAAA"}
            )
            
        self.assertEqual(resp.status_code, 200)
        mock_issue1.assert_called_once_with("0x123", custom_ttl=7200 + 60)
        self.assertEqual(resp.get_json().get("auth_token"), "fake_token")
