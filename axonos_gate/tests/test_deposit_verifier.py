"""
Tests for deposit_verifier: Transfer log parsing and verify_deposit with mocked RPC.
"""

import os
import sys
import unittest
from decimal import Decimal
from unittest.mock import patch

_tests_dir = os.path.dirname(os.path.abspath(__file__))
_repo_root = os.path.dirname(os.path.dirname(_tests_dir))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)


class TestDepositVerifierParsing(unittest.TestCase):
    def setUp(self):
        self.env = {
            "AXGT_REVENUE_WALLET": "0xrevenue00000000000000000000000000000000001",
            "AXGT_CONTRACT_ADDRESS": "0xcontract0000000000000000000000000000000001",
            "AXGT_RPC_URL": "https://rpc.example.com",
        }
        self.patcher = patch.dict(os.environ, self.env)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def test_parse_transfer_logs_empty(self):
        from deposit_verifier import _parse_transfer_logs
        total = _parse_transfer_logs(
            [],
            "0xcontract0000000000000000000000000000000001",
            "0xrevenue00000000000000000000000000000000001",
            "0xsender000000000000000000000000000000000001",
            18,
        )
        self.assertEqual(total, Decimal("0"))

    def test_parse_transfer_logs_single_transfer(self):
        from axonos_gate.deposit_verifier import _parse_transfer_logs, TRANSFER_TOPIC
        amount_hex = "0x" + hex(100 * 10**18)[2:].zfill(64)
        # Topic: 32 bytes; last 20 bytes = address (40 hex chars). Use 40-char addresses.
        from_40 = "sender0000000000000000000000000000000000"
        to_40 = "revenue000000000000000000000000000000001"  # 40 chars
        logs = [
            {
                "address": "0xcontract0000000000000000000000000000000001",
                "topics": [TRANSFER_TOPIC, "0x" + "0" * 24 + from_40, "0x" + "0" * 24 + to_40],
                "data": amount_hex,
            }
        ]
        total = _parse_transfer_logs(
            logs,
            "0xcontract0000000000000000000000000000000001",
            "0xrevenue000000000000000000000000000000001",
            "0x" + from_40,
            18,
        )
        self.assertEqual(total, Decimal("100"))

    def test_parse_transfer_logs_wrong_sender_ignored(self):
        from deposit_verifier import _parse_transfer_logs, TRANSFER_TOPIC
        amount_hex = "0x" + hex(50 * 10**18)[2:].zfill(64)
        logs = [
            {
                "address": "0xcontract0000000000000000000000000000000001",
                "topics": [
                    TRANSFER_TOPIC,
                    "0x000000000000000000000000other000000000000000000000000000000000001",
                    "0x000000000000000000000000revenue00000000000000000000000000000000001",
                ],
                "data": amount_hex,
            }
        ]
        total = _parse_transfer_logs(
            logs,
            "0xcontract0000000000000000000000000000000001",
            "0xrevenue00000000000000000000000000000000001",
            "0xsender000000000000000000000000000000000001",
            18,
        )
        self.assertEqual(total, Decimal("0"))

    def test_parse_transfer_logs_multiple_same_sender_summed(self):
        from axonos_gate.deposit_verifier import _parse_transfer_logs, TRANSFER_TOPIC
        from_addr_40 = "sender0000000000000000000000000000000000"
        to_addr_40 = "revenue000000000000000000000000000000001"  # 40 chars
        a1 = "0x" + hex(30 * 10**18)[2:].zfill(64)
        a2 = "0x" + hex(70 * 10**18)[2:].zfill(64)
        logs = [
            {
                "address": "0xcontract0000000000000000000000000000000001",
                "topics": [TRANSFER_TOPIC, "0x" + "0" * 24 + from_addr_40, "0x" + "0" * 24 + to_addr_40],
                "data": a1,
            },
            {
                "address": "0xcontract0000000000000000000000000000000001",
                "topics": [TRANSFER_TOPIC, "0x" + "0" * 24 + from_addr_40, "0x" + "0" * 24 + to_addr_40],
                "data": a2,
            },
        ]
        total = _parse_transfer_logs(
            logs,
            "0xcontract0000000000000000000000000000000001",
            "0xrevenue000000000000000000000000000000001",
            "0x" + from_addr_40,
            18,
        )
        self.assertEqual(total, Decimal("100"))

    @patch("axonos_gate.deposit_ledger.tx_hash_already_credited")
    def test_verify_deposit_duplicate_tx_rejected(self, mock_already):
        from axonos_gate.deposit_verifier import verify_deposit
        mock_already.return_value = True
        result = verify_deposit(
            authenticated_wallet="0x1234567890123456789012345678901234567890",
            tx_hash="0xabcdef",
        )
        self.assertFalse(result["verified"])
        self.assertIn("already credited", result.get("error", "").lower())

    @patch("axonos_gate.deposit_verifier._rpc")
    @patch("axonos_gate.deposit_ledger.tx_hash_already_credited")
    def test_verify_deposit_tx_not_found(self, mock_already, mock_rpc):
        from axonos_gate.deposit_verifier import verify_deposit
        mock_already.return_value = False
        mock_rpc.return_value = None
        result = verify_deposit(
            authenticated_wallet="0x1234567890123456789012345678901234567890",
            tx_hash="0xabcdef",
        )
        self.assertFalse(result["verified"])
        self.assertTrue(result.get("pending"))
        self.assertEqual(result.get("confirmations"), 0)
        self.assertIn("not found", result.get("error", "").lower())

    @patch("axonos_gate.deposit_verifier._rpc")
    @patch("axonos_gate.deposit_ledger.tx_hash_already_credited")
    def test_verify_deposit_insufficient_confirmations_pending(
        self, mock_already, mock_rpc
    ):
        from axonos_gate.deposit_verifier import verify_deposit

        mock_already.return_value = False
        wallet = "0x1234567890123456789012345678901234567890"
        tx_hash = "0x" + "ab" * 32

        def rpc_side_effect(url, method, params):
            if method == "eth_getTransactionByHash":
                return {"from": wallet, "to": "0x" + "1" * 40, "value": "0x0"}
            if method == "eth_getTransactionReceipt":
                return {"status": "0x1", "blockNumber": "0x64"}
            if method == "eth_blockNumber":
                return "0x65"  # 2 confirmations (0x65 - 0x64 + 1)
            return None

        mock_rpc.side_effect = rpc_side_effect
        with patch.dict(os.environ, {"AXGT_DEPOSIT_MIN_CONFIRMATIONS": "6"}, clear=False):
            result = verify_deposit(authenticated_wallet=wallet, tx_hash=tx_hash)

        self.assertFalse(result["verified"])
        self.assertTrue(result.get("pending"))
        self.assertEqual(result.get("confirmations"), 2)
        self.assertEqual(result.get("required"), 6)
        self.assertIn("Insufficient confirmations", result.get("error", ""))


if __name__ == "__main__":
    unittest.main()
