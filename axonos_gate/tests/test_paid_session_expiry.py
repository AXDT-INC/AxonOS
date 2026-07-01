import os
import sys
import time
import unittest
from unittest.mock import patch, MagicMock

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
            (True,),  # pg_try_advisory_xact_lock
            ("0xwallet", 197),  # _expire_stale_session
            ("active", now - 100, "0xwallet"), # reconcile SELECT for 197
        ]
        cur.fetchall.side_effect = [
            [],  # _pause_stale_zero_credit_sessions
            [],  # _expire_stale_paused_sessions
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
            None,  # _expire_stale_session
            ("ended", time.time() - 100, "0xwallet"), # reconcile SELECT for 197
        ]
        cur.fetchall.side_effect = [
            [],  # _pause_stale_zero_credit_sessions
            [],  # _expire_stale_paused_sessions
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
                None,  # _expire_stale_session
                ("ended", time.time() - 100, "0xwallet"), # reconcile SELECT
            ]
            cur.fetchall.side_effect = [
                [],  # _pause_stale_zero_credit_sessions
                [],  # _expire_stale_paused_sessions
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
            None,  # _expire_stale_session
            None,  # reconcile SELECT -> not found in DB
        ]
        cur.fetchall.side_effect = [
            [],  # _pause_stale_zero_credit_sessions
            [],  # _expire_stale_paused_sessions
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
            None,  # _expire_stale_session
            ("active", now + 1000, "0xwallet"), # reconcile SELECT -> active and NOT expired
        ]
        cur.fetchall.side_effect = [
            [],  # _pause_stale_zero_credit_sessions
            [],  # _expire_stale_paused_sessions
        ]
        
        session_manager.perform_session_cleanup()
        mock_stop1.assert_not_called()
        mock_stop2.assert_not_called()

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
