"""
Tests for deposit_ledger: balance, ledger writes, replay protection, admin helpers.
Uses mocked Postgres (psycopg2) to avoid a real DB.
"""

import os
import unittest
from unittest.mock import MagicMock, patch

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestDepositLedger(unittest.TestCase):
    def setUp(self):
        self.env_patcher = patch.dict(os.environ, {"AXGT_CHALLENGE_DB_URL": "postgresql://test/test"})
        self.env_patcher.start()
        import deposit_ledger as dl
        if hasattr(dl, "_pg_init_done"):
            dl._pg_init_done = False

    def tearDown(self):
        self.env_patcher.stop()

    @patch("deposit_ledger._get_connection")
    def test_get_remaining_minutes_no_record(self, mock_conn):
        import deposit_ledger as dl
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = None
        conn = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.return_value = conn

        with patch.object(dl, "init_once", return_value=True):
            with patch.object(dl, "_get_connection", return_value=conn):
                remaining = dl.get_remaining_minutes("0x1234567890123456789012345678901234567890")
        self.assertEqual(remaining, 0.0)

    @patch("deposit_ledger._get_connection")
    def test_get_remaining_minutes_with_balance(self, mock_conn):
        import deposit_ledger as dl
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.return_value = (75.5,)
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.return_value = conn

        with patch.object(dl, "init_once", return_value=True):
            with patch.object(dl, "_get_connection", return_value=conn):
                remaining = dl.get_remaining_minutes("0x1234567890123456789012345678901234567890")
        self.assertEqual(remaining, 75.5)

    def test_tx_hash_already_credited_empty_hash(self):
        import deposit_ledger as dl
        with patch.object(dl, "init_once", return_value=True):
            self.assertTrue(dl.tx_hash_already_credited(""))
            self.assertTrue(dl.tx_hash_already_credited(None))

    @patch("deposit_ledger._get_connection")
    def test_tx_hash_already_credited_not_seen(self, mock_conn):
        import deposit_ledger as dl
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.return_value = None
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.return_value = conn

        with patch.object(dl, "init_once", return_value=True):
            with patch.object(dl, "_get_connection", return_value=conn):
                self.assertFalse(dl.tx_hash_already_credited("0xabc"))

    @patch("deposit_ledger._get_connection")
    def test_tx_hash_already_credited_seen(self, mock_conn):
        import deposit_ledger as dl
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.return_value = (1,)
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.return_value = conn

        with patch.object(dl, "init_once", return_value=True):
            with patch.object(dl, "_get_connection", return_value=conn):
                self.assertTrue(dl.tx_hash_already_credited("0xabc"))

    def test_get_deposit_status_invalid_wallet(self):
        import deposit_ledger as dl
        with patch.object(dl, "init_once", return_value=True):
            status = dl.get_deposit_status("")
        self.assertFalse(status["has_deposit"])
        self.assertEqual(status["remaining_minutes"], 0.0)

    def test_ledger_event_types(self):
        import deposit_ledger as dl
        self.assertIn("deposit_credit", dl._ALLOWED_EVENT_TYPES)
        self.assertIn("usage_deduction", dl._ALLOWED_EVENT_TYPES)
        self.assertIn("session_expiry", dl._ALLOWED_EVENT_TYPES)
        self.assertIn("verification_reject", dl._ALLOWED_EVENT_TYPES)


if __name__ == "__main__":
    unittest.main()
