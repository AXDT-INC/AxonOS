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
        self.assertIn("test_credit", dl._ALLOWED_EVENT_TYPES)
        self.assertIn("usage_deduction", dl._ALLOWED_EVENT_TYPES)
        self.assertIn("session_expiry", dl._ALLOWED_EVENT_TYPES)
        self.assertIn("verification_reject", dl._ALLOWED_EVENT_TYPES)

    def test_verified_deposit_schema_has_safe_provenance_migration(self):
        import deposit_ledger as dl

        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        dl._ensure_tables(conn)

        sql = "\n".join(str(call.args[0]) for call in cur.execute.call_args_list)
        self.assertIn("ADD COLUMN IF NOT EXISTS credit_source", sql)
        self.assertIn("ADD COLUMN IF NOT EXISTS payment_rail", sql)
        self.assertIn("DEFAULT 'onchain'", sql)
        self.assertIn("credit_source = 'legacy_test_credit'", sql)
        self.assertIn("^0xffffffff[0-9A-Fa-f]{56}$", sql)
        self.assertIn("block_number = 0", sql)
        self.assertIn("axgt_amount = 0", sql)
        conn.commit.assert_called_once()

    @staticmethod
    def _test_credit_connection(fetch_rows):
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.side_effect = fetch_rows
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        return conn, cur

    def test_test_credit_partial_grant_holds_wallet_lock_and_stops_at_exact_cap(self):
        import deposit_ledger as dl

        wallet = "0x1234567890123456789012345678901234567890"
        conn, cur = self._test_credit_connection([(59.0,), None])
        with patch.object(dl, "init_once", return_value=True), patch.object(
            dl, "_get_connection", return_value=conn
        ), patch.object(dl.time, "time", return_value=1000.0):
            result = dl.credit_test_grant(wallet, 60.0, 60.0, "request-0001", "eth")

        self.assertTrue(result["ok"])
        self.assertEqual(result["credited_minutes"], 1.0)
        self.assertEqual(result["remaining_minutes"], 60.0)
        sql = "\n".join(str(call.args[0]) for call in cur.execute.call_args_list)
        self.assertIn("pg_advisory_xact_lock", sql)
        self.assertIn("FOR UPDATE", sql)
        self.assertIn("credit_source", sql)
        self.assertIn("payment_rail", sql)
        verified_calls = [
            call
            for call in cur.execute.call_args_list
            if "INSERT INTO axgt_verified_deposits" in str(call.args[0])
        ]
        self.assertEqual(len(verified_calls), 1)
        self.assertIn("test_credit", verified_calls[0].args[1])
        self.assertIn("eth", verified_calls[0].args[1])
        ledger_calls = [
            call
            for call in cur.execute.call_args_list
            if "INSERT INTO axgt_ledger" in str(call.args[0])
        ]
        self.assertEqual(ledger_calls[0].args[1][1], "test_credit")
        conn.commit.assert_called_once()

    def test_whitelisted_additive_grant_adds_full_amount_above_prior_balance(self):
        import deposit_ledger as dl

        wallet = "0x1234567890123456789012345678901234567890"
        conn, cur = self._test_credit_connection([(59.0,), None])
        with patch.object(dl, "init_once", return_value=True), patch.object(
            dl, "_get_connection", return_value=conn
        ), patch.object(dl.time, "time", return_value=1000.0):
            result = dl.credit_test_grant(
                wallet, 60.0, 60.0, "request-0001", "eth", additive=True
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["credited_minutes"], 60.0)
        self.assertEqual(result["remaining_minutes"], 119.0)
        updates = [
            call for call in cur.execute.call_args_list
            if "UPDATE axgt_deposits" in str(call.args[0])
        ]
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].args[1][0], 60.0)
        self.assertEqual(updates[0].args[1][1], 119.0)

    def test_test_credit_replay_is_idempotent(self):
        import deposit_ledger as dl

        wallet = "0x1234567890123456789012345678901234567890"
        conn, cur = self._test_credit_connection(
            [(10.0,), (wallet, 5.0, "test_credit", "eth")]
        )
        with patch.object(dl, "init_once", return_value=True), patch.object(
            dl, "_get_connection", return_value=conn
        ):
            result = dl.credit_test_grant(wallet, 60.0, 60.0, "request-0001", "eth")

        self.assertTrue(result["ok"])
        self.assertTrue(result["replayed"])
        self.assertEqual(result["credited_minutes"], 5.0)
        sql = "\n".join(str(call.args[0]) for call in cur.execute.call_args_list)
        self.assertNotIn("UPDATE axgt_deposits", sql)
        conn.commit.assert_called_once()

    def test_test_credit_request_reuse_cannot_change_wallet_or_rail(self):
        import deposit_ledger as dl

        wallet = "0x1234567890123456789012345678901234567890"
        other = "0x2234567890123456789012345678901234567890"
        conn, _ = self._test_credit_connection(
            [(10.0,), (other, 5.0, "test_credit", "usdc")]
        )
        with patch.object(dl, "init_once", return_value=True), patch.object(
            dl, "_get_connection", return_value=conn
        ):
            result = dl.credit_test_grant(wallet, 60.0, 60.0, "request-0001", "eth")

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "request_mismatch")
        conn.commit.assert_called_once()

    def test_test_credit_at_cap_is_audited_successful_no_op(self):
        import deposit_ledger as dl

        wallet = "0x1234567890123456789012345678901234567890"
        conn, cur = self._test_credit_connection([(60.0,), None])
        with patch.object(dl, "init_once", return_value=True), patch.object(
            dl, "_get_connection", return_value=conn
        ):
            result = dl.credit_test_grant(wallet, 60.0, 60.0, "request-0001", "axgt")

        self.assertTrue(result["ok"])
        self.assertTrue(result["capped"])
        self.assertTrue(result["no_op"])
        self.assertEqual(result["credited_minutes"], 0.0)
        verified_calls = [
            call
            for call in cur.execute.call_args_list
            if "INSERT INTO axgt_verified_deposits" in str(call.args[0])
        ]
        self.assertEqual(len(verified_calls), 1)
        ledger_calls = [
            call
            for call in cur.execute.call_args_list
            if "INSERT INTO axgt_ledger" in str(call.args[0])
        ]
        self.assertEqual(ledger_calls[0].args[1][1], "test_credit")
        self.assertEqual(ledger_calls[0].args[1][2], 0.0)
        conn.commit.assert_called_once()

    def test_test_credit_ledger_rejects_invalid_wallet_nonfinite_and_oversized_config(self):
        import deposit_ledger as dl

        invalid_wallet = dl.credit_test_grant("bad", 60, 60, "request-0001", "eth")
        self.assertEqual(invalid_wallet["error_code"], "invalid_wallet")
        for grant, cap in ((float("nan"), 60), (float("inf"), 60), (1441, 60), (60, 10081)):
            with self.subTest(grant=grant, cap=cap):
                result = dl.credit_test_grant(
                    "0x1234567890123456789012345678901234567890",
                    grant,
                    cap,
                    "request-0001",
                    "eth",
                )
                self.assertEqual(result["error_code"], "invalid_credit_config")


if __name__ == "__main__":
    unittest.main()
