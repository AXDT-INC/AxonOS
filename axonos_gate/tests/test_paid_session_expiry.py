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
            (True,),  # pg_try_advisory_lock scheduler/teardown lock
            ("active", now - 100, "0xwallet"), # reconcile SELECT for 197
        ]
        cur.fetchall.side_effect = [
            [("0xwallet", 197)],  # _expire_stale_session
            [],  # _expire_credit_grace_sessions
            [],  # post-cleanup stale recheck
            [],  # post-cleanup credit-grace recheck
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
            (True,),  # pg_try_advisory_lock scheduler/teardown lock
            ("ended", time.time() - 100, "0xwallet"), # reconcile SELECT for 197
        ]
        cur.fetchall.side_effect = [
            [],  # _expire_stale_session
            [],  # _expire_credit_grace_sessions
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
                (True,),  # pg_try_advisory_lock scheduler/teardown lock
                ("ended", time.time() - 100, "0xwallet"), # reconcile SELECT
            ]
            cur.fetchall.side_effect = [
                [],  # _expire_stale_session
                [],  # _expire_credit_grace_sessions
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
            (True,),  # pg_try_advisory_lock scheduler/teardown lock
            None,  # reconcile SELECT -> not found in DB
        ]
        cur.fetchall.side_effect = [
            [],  # _expire_stale_session
            [],  # _expire_credit_grace_sessions
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
            (True,),  # pg_try_advisory_lock scheduler/teardown lock
            ("active", now + 1000, "0xwallet"), # reconcile SELECT -> active and NOT expired
        ]
        cur.fetchall.side_effect = [
            [],  # _expire_stale_session
            [],  # _expire_credit_grace_sessions
        ]
        
        session_manager.perform_session_cleanup()
        mock_stop1.assert_not_called()
        mock_stop2.assert_not_called()

    def test_stale_runtime_heartbeat_ends_instead_of_entering_credit_grace(self):
        from axonos_gate import session_manager

        cur = MagicMock()
        cur.fetchall.return_value = [
            ("0xwallet", 197),
            ("0xsecond", 198),
        ]
        with patch.object(
            session_manager, "_heartbeat_timeout_seconds", return_value=120
        ), patch.object(session_manager, "session_grace_seconds", return_value=60):
            ended, grace_transitions = session_manager._expire_stale_session(
                cur, 1000.0
            )

        self.assertEqual(ended, [("0xwallet", 197), ("0xsecond", 198)])
        self.assertEqual(grace_transitions, [])
        sql, params = cur.execute.call_args.args
        self.assertIn("SET status = 'ended'", sql)
        self.assertNotIn("credit_grace", sql)
        self.assertEqual(params, (880.0, 1000.0, 60, 1000.0))

        with patch.object(session_manager, "_on_session_credit_grace") as grace_hook, \
             patch.object(session_manager, "_on_session_ended") as ended_hook:
            session_manager._apply_stale_session_maintenance(
                ended, grace_transitions
            )
        grace_hook.assert_not_called()
        self.assertEqual(
            ended_hook.call_args_list,
            [
                call("0xwallet", 197),
                call("0xsecond", 198),
            ],
        )

    def test_credit_grace_expiry_uses_its_fixed_start_timestamp(self):
        from axonos_gate import session_manager

        cur = MagicMock()
        cur.fetchall.return_value = [("0xwallet", 197)]
        with patch.object(
            session_manager,
            "_session_credit_grace_max_seconds",
            return_value=7200,
        ):
            expired = session_manager._expire_credit_grace_sessions(cur, 10000.0)

        self.assertEqual(expired, [("0xwallet", 197)])
        sql, params = cur.execute.call_args.args
        self.assertIn("WHERE status = 'credit_grace'", sql)
        self.assertIn(
            "COALESCE(credit_grace_started_at, last_heartbeat)",
            " ".join(sql.split()),
        )
        self.assertNotIn("hard_expires_at", sql)
        self.assertEqual(params, (2800.0,))

    def test_reconciliation_does_not_apply_ssh_hard_cap_during_credit_grace(self):
        from axonos_gate import session_manager

        cur = MagicMock()
        cur.fetchone.return_value = ("credit_grace", 100.0, "0xwallet")
        launcher = MagicMock()
        launcher.list_running_sessions.return_value = [197]

        with patch.object(
            session_manager, "_import_session_launcher", return_value=launcher
        ), patch.object(session_manager, "session_grace_seconds", return_value=60):
            to_stop, to_expire = session_manager._reconcile_containers(cur, 1000.0)

        self.assertEqual(to_stop, [])
        self.assertEqual(to_expire, [])
        self.assertEqual(cur.execute.call_count, 1)

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
