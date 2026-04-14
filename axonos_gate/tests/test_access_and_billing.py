"""
Access control and billing tests: wallet with no deposit denied, with credit allowed,
heartbeat billing. Uses mocked deposit_ledger and session DB.
"""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

_repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)


class TestAccessControl(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(
            os.environ,
            {
                "AXGT_CHALLENGE_DB_URL": "postgresql://test/test",
                "AXGT_MIN_DEPOSIT": "100",
                "AXGT_CREDIT_PER_100_AXGT_MINUTES": "60",
            },
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_get_wallet_access_status_no_deposit(self):
        from axonos_gate import axgt_verifier
        with patch("axonos_gate.deposit_ledger.init_once", return_value=True), \
             patch("axonos_gate.deposit_ledger.get_deposit_status") as mock_get:
            mock_get.return_value = {
                "remaining_minutes": 0.0,
                "consumed_minutes": 0.0,
                "credited_minutes_total": 0.0,
                "deposited_amount_axgt": 0,
                "has_deposit": False,
            }
            status = axgt_verifier.get_wallet_access_status("0x1234567890123456789012345678901234567890")
        self.assertFalse(status["verified"])
        self.assertIsNone(status["access_type"])
        self.assertEqual(status["remaining_minutes"], 0.0)

    def test_get_wallet_access_status_with_credit(self):
        from axonos_gate import axgt_verifier
        with patch("axonos_gate.deposit_ledger.init_once", return_value=True), \
             patch("axonos_gate.deposit_ledger.get_deposit_status") as mock_get:
            mock_get.return_value = {
                "remaining_minutes": 45.0,
                "consumed_minutes": 15.0,
                "credited_minutes_total": 60.0,
                "deposited_amount_axgt": 100,
                "has_deposit": True,
            }
            status = axgt_verifier.get_wallet_access_status("0x1234567890123456789012345678901234567890")
        self.assertTrue(status["verified"])
        self.assertEqual(status["access_type"], "deposit_credit")
        self.assertEqual(status["remaining_minutes"], 45.0)
        self.assertEqual(status["consumed_minutes"], 15.0)
        self.assertEqual(status["credited_minutes"], 60.0)

    def test_has_access_no_credit(self):
        from axonos_gate import axgt_verifier
        with patch("axonos_gate.axgt_verifier.get_wallet_access_status") as mock_status:
            mock_status.return_value = {"verified": False, "remaining_minutes": 0.0}
            allowed, access_type, remaining = axgt_verifier.has_access("0x1234567890123456789012345678901234567890")
        self.assertFalse(allowed)
        self.assertIsNone(access_type)
        self.assertEqual(remaining, 0.0)

    def test_has_access_with_credit(self):
        from axonos_gate import axgt_verifier
        with patch("axonos_gate.axgt_verifier.get_wallet_access_status") as mock_status:
            mock_status.return_value = {
                "verified": True,
                "access_type": "deposit_credit",
                "remaining_minutes": 30.0,
            }
            allowed, access_type, remaining = axgt_verifier.has_access("0x1234567890123456789012345678901234567890")
        self.assertTrue(allowed)
        self.assertEqual(access_type, "deposit_credit")
        self.assertEqual(remaining, 30.0)

    def test_get_credit_policy_deposit_based(self):
        from axonos_gate import axgt_verifier
        with patch.dict(os.environ, {"AXGT_MIN_DEPOSIT": "100", "AXGT_CREDIT_PER_100_AXGT_MINUTES": "60"}):
            policy = axgt_verifier.get_credit_policy()
        self.assertIn("min_deposit", policy)
        self.assertEqual(policy["credit_per_100_axgt_minutes"], 60)


class TestBillingAndSession(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"AXGT_CHALLENGE_DB_URL": "postgresql://test/test"})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    @patch("axonos_gate.session_manager._get_connection")
    def test_heartbeat_calls_deduct_usage(self, mock_conn):
        from axonos_gate import session_manager
        session_manager._pg_init_done = True
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.side_effect = [
            None,                          # _expire_stale_session RETURNING
            (1, 1000.0, 2000.0, 500.0, "small", "0", "shared-desktop"),   # SELECT session row
            (2000.0,),                     # UPDATE RETURNING expires_at
        ]
        conn.cursor.return_value = cur
        mock_conn.return_value = conn

        with patch("axonos_gate.deposit_ledger.init_once", return_value=True), \
             patch("axonos_gate.deposit_ledger._deduct_usage_on_cursor") as mock_deduct:
            mock_deduct.return_value = (True, 58.5, None)
            result = session_manager.heartbeat("0x1234567890123456789012345678901234567890")
        self.assertTrue(result.get("ok"))
        mock_deduct.assert_called_once()
        call_args = mock_deduct.call_args[0]
        self.assertEqual(call_args[1], "0x1234567890123456789012345678901234567890".lower())
        self.assertGreater(call_args[2], 0)

    def test_gpu_allocation_no_overlap(self):
        from axonos_gate import session_manager
        active_rows = [
            {"gpu_ids": [0], "wallet_address": "0xaaa"},
            {"gpu_ids": [2, 3], "wallet_address": "0xbbb"},
        ]
        with patch.dict(os.environ, {"AXGT_GPU_DEVICE_IDS": "0,1,2,3"}):
            alloc = session_manager._choose_allocation(active_rows, 1)
        self.assertEqual(alloc, [1])

    def test_gpu_allocation_insufficient_capacity(self):
        from axonos_gate import session_manager
        active_rows = [
            {"gpu_ids": [0, 1, 2], "wallet_address": "0xaaa"},
        ]
        with patch.dict(os.environ, {"AXGT_GPU_DEVICE_IDS": "0,1,2,3"}):
            alloc = session_manager._choose_allocation(active_rows, 2)
        self.assertIsNone(alloc)


if __name__ == "__main__":
    unittest.main()
