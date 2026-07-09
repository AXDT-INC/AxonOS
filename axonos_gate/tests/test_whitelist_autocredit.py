"""
Tests for whitelist auto-credit mock deposits.

Only sentinel mock hashes (0xffffffff + 56 hex) are handled by the bypass; real
tx hashes — including from whitelisted wallets — fall through to on-chain
verification. Mock credits are replay-protected, capped by remaining balance,
and DB outages surface as retryable (pending) rather than terminal errors.
"""

import os
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

WHITELISTED = "0x1111111111111111111111111111111111111111"
WHITELISTED_2 = "0x2222222222222222222222222222222222222222"
NOT_WHITELISTED = "0x3333333333333333333333333333333333333333"
SENTINEL_TX = "0xffffffff" + "ab" * 28
REAL_TX = "0x" + "9c" * 32


def make_ledger(*, init=True, already=False, remaining=0.0, credit=(True, 60.0, None)):
    ledger = MagicMock()
    ledger.init_once.return_value = init
    ledger.tx_hash_already_credited_strict.return_value = already
    ledger.get_remaining_minutes.return_value = remaining
    ledger.credit_deposit.return_value = credit
    return ledger


class TestWhitelistAutoCredit(unittest.TestCase):
    def setUp(self):
        self.env_patcher = patch.dict(os.environ, {
            "AXONOS_WHITELISTED_WALLETS": f"{WHITELISTED},{WHITELISTED_2}",
            "AXONOS_WHITELIST_AUTO_CREDIT_MINUTES": "60",
        })
        self.env_patcher.start()

    def tearDown(self):
        self.env_patcher.stop()

    def test_is_wallet_whitelisted(self):
        from axgt_verifier import is_wallet_whitelisted
        self.assertTrue(is_wallet_whitelisted(WHITELISTED))
        self.assertTrue(is_wallet_whitelisted(WHITELISTED + "  "))
        self.assertTrue(is_wallet_whitelisted(WHITELISTED_2.upper()))
        self.assertFalse(is_wallet_whitelisted(NOT_WHITELISTED))
        self.assertFalse(is_wallet_whitelisted(""))
        self.assertFalse(is_wallet_whitelisted(None))

    def test_mock_hash_detection(self):
        from axgt_verifier import is_mock_whitelist_tx_hash
        self.assertTrue(is_mock_whitelist_tx_hash(SENTINEL_TX))
        self.assertTrue(is_mock_whitelist_tx_hash(SENTINEL_TX.upper()))
        # Real-format hashes and junk are never treated as mock deposits.
        self.assertFalse(is_mock_whitelist_tx_hash(REAL_TX))
        self.assertFalse(is_mock_whitelist_tx_hash("0xabc"))
        self.assertFalse(is_mock_whitelist_tx_hash("junk"))
        self.assertFalse(is_mock_whitelist_tx_hash("0xffffffff" + "z" * 56))
        self.assertFalse(is_mock_whitelist_tx_hash(""))
        self.assertFalse(is_mock_whitelist_tx_hash(None))

    def test_real_hash_falls_through_even_for_whitelisted_wallet(self):
        # Regression: a genuine on-chain deposit from a whitelisted wallet must go
        # through real verification, not be consumed as a flat mock credit.
        from axgt_verifier import check_and_apply_whitelist_deposit_credit
        ledger = make_ledger()
        with patch("axgt_verifier._get_deposit_ledger", return_value=ledger):
            self.assertIsNone(check_and_apply_whitelist_deposit_credit(WHITELISTED, REAL_TX))
            self.assertIsNone(check_and_apply_whitelist_deposit_credit(WHITELISTED, "0xabc"))
            self.assertIsNone(check_and_apply_whitelist_deposit_credit(WHITELISTED, "junk"))
        ledger.credit_deposit.assert_not_called()

    def test_sentinel_from_non_whitelisted_wallet_denied(self):
        from axgt_verifier import check_and_apply_whitelist_deposit_credit
        ledger = make_ledger()
        with patch("axgt_verifier._get_deposit_ledger", return_value=ledger):
            res = check_and_apply_whitelist_deposit_credit(NOT_WHITELISTED, SENTINEL_TX)
        self.assertIsNotNone(res)
        self.assertFalse(res["verified"])
        self.assertTrue(res["not_whitelisted"])
        ledger.credit_deposit.assert_not_called()

    def test_success(self):
        from axgt_verifier import check_and_apply_whitelist_deposit_credit
        ledger = make_ledger(credit=(True, 60.0, None))
        with patch("axgt_verifier._get_deposit_ledger", return_value=ledger):
            res = check_and_apply_whitelist_deposit_credit(WHITELISTED, SENTINEL_TX.upper())
        self.assertTrue(res["verified"])
        self.assertTrue(res["mock"])
        self.assertEqual(res["access_type"], "deposit_credit")
        self.assertEqual(res["credited_minutes"], 60.0)
        self.assertEqual(res["remaining_minutes"], 60.0)
        self.assertEqual(res["tx_hash"], SENTINEL_TX)
        ledger.credit_deposit.assert_called_once_with(
            wallet_address=WHITELISTED,
            axgt_amount=Decimal("0"),
            credited_minutes=60.0,
            tx_hash=SENTINEL_TX,
            block_number=0,
        )

    def test_replay_protection(self):
        from axgt_verifier import check_and_apply_whitelist_deposit_credit
        ledger = make_ledger(already=True)
        with patch("axgt_verifier._get_deposit_ledger", return_value=ledger):
            res = check_and_apply_whitelist_deposit_credit(WHITELISTED, SENTINEL_TX)
        self.assertFalse(res["verified"])
        self.assertEqual(res["error"], "already credited")
        ledger.credit_deposit.assert_not_called()

    def test_ledger_unavailable_is_retryable(self):
        # DB outages must surface as pending (client keeps polling), not as a
        # terminal error or a misleading "already credited".
        from axgt_verifier import check_and_apply_whitelist_deposit_credit
        for ledger in (make_ledger(init=False), make_ledger(already=None)):
            with patch("axgt_verifier._get_deposit_ledger", return_value=ledger):
                res = check_and_apply_whitelist_deposit_credit(WHITELISTED, SENTINEL_TX)
            self.assertFalse(res["verified"])
            self.assertTrue(res["pending"])
            ledger.credit_deposit.assert_not_called()

    def test_balance_cap_blocks_unbounded_minting(self):
        from axgt_verifier import check_and_apply_whitelist_deposit_credit
        ledger = make_ledger(remaining=60.0)
        with patch("axgt_verifier._get_deposit_ledger", return_value=ledger):
            res = check_and_apply_whitelist_deposit_credit(WHITELISTED, SENTINEL_TX)
        self.assertFalse(res["verified"])
        self.assertTrue(res["whitelist_capped"])
        self.assertEqual(res["remaining_minutes"], 60.0)
        ledger.credit_deposit.assert_not_called()

    def test_custom_credit_minutes(self):
        from axgt_verifier import check_and_apply_whitelist_deposit_credit
        ledger = make_ledger(credit=(True, 90.0, None))
        with patch.dict(os.environ, {"AXONOS_WHITELIST_AUTO_CREDIT_MINUTES": "90"}), \
             patch("axgt_verifier._get_deposit_ledger", return_value=ledger):
            res = check_and_apply_whitelist_deposit_credit(WHITELISTED, SENTINEL_TX)
        self.assertTrue(res["verified"])
        self.assertEqual(res["credited_minutes"], 90.0)

    def test_invalid_minutes_env_defaults_to_60(self):
        from axgt_verifier import check_and_apply_whitelist_deposit_credit
        ledger = make_ledger()
        with patch.dict(os.environ, {"AXONOS_WHITELIST_AUTO_CREDIT_MINUTES": "abc"}), \
             patch("axgt_verifier._get_deposit_ledger", return_value=ledger):
            res = check_and_apply_whitelist_deposit_credit(WHITELISTED, SENTINEL_TX)
        self.assertTrue(res["verified"])
        self.assertEqual(res["credited_minutes"], 60.0)

    def test_custom_max_balance_cap(self):
        from axgt_verifier import check_and_apply_whitelist_deposit_credit
        # With a raised cap, a wallet holding 60 minutes can still top up.
        ledger = make_ledger(remaining=60.0, credit=(True, 120.0, None))
        with patch.dict(os.environ, {"AXONOS_WHITELIST_MAX_BALANCE_MINUTES": "120"}), \
             patch("axgt_verifier._get_deposit_ledger", return_value=ledger):
            res = check_and_apply_whitelist_deposit_credit(WHITELISTED, SENTINEL_TX)
        self.assertTrue(res["verified"])


class TestVerifyDepositIntegration(unittest.TestCase):
    """The bypass lives inside the shared rail verifiers, so both gate servers and
    the auto-detect router inherit it without endpoint-level special cases."""

    def setUp(self):
        self.env_patcher = patch.dict(os.environ, {
            "AXONOS_WHITELISTED_WALLETS": WHITELISTED,
            "AXONOS_WHITELIST_AUTO_CREDIT_MINUTES": "60",
        })
        self.env_patcher.start()

    def tearDown(self):
        self.env_patcher.stop()

    def _patch_ledger(self, ledger):
        # axgt_verifier can be loaded both as a top-level module (gate servers add
        # axonos_gate/ to sys.path) and as axonos_gate.axgt_verifier — patch both.
        return (
            patch("axgt_verifier._get_deposit_ledger", return_value=ledger),
            patch("axonos_gate.axgt_verifier._get_deposit_ledger", return_value=ledger),
        )

    def test_verify_deposit_short_circuits_on_sentinel(self):
        from deposit_verifier import verify_deposit
        ledger = make_ledger(credit=(True, 60.0, None))
        p1, p2 = self._patch_ledger(ledger)
        with p1, p2:
            res = verify_deposit(authenticated_wallet=WHITELISTED, tx_hash=SENTINEL_TX)
        self.assertTrue(res["verified"])
        self.assertTrue(res["mock"])
        ledger.credit_deposit.assert_called_once()

    def test_verify_usdc_deposit_short_circuits_on_sentinel(self):
        from x402_verifier import verify_usdc_deposit
        ledger = make_ledger(credit=(True, 60.0, None))
        p1, p2 = self._patch_ledger(ledger)
        with p1, p2:
            res = verify_usdc_deposit(authenticated_wallet=WHITELISTED, tx_hash=SENTINEL_TX)
        self.assertTrue(res["verified"])
        self.assertTrue(res["mock"])
        ledger.credit_deposit.assert_called_once()

    def test_verify_deposit_sentinel_from_non_whitelisted_is_terminal(self):
        # Must not fall through to on-chain lookup of a hash that does not exist.
        from deposit_verifier import verify_deposit
        ledger = make_ledger()
        p1, p2 = self._patch_ledger(ledger)
        with p1, p2:
            res = verify_deposit(authenticated_wallet=NOT_WHITELISTED, tx_hash=SENTINEL_TX)
        self.assertFalse(res["verified"])
        self.assertTrue(res["not_whitelisted"])
        self.assertFalse(res.get("pending", False))


if __name__ == "__main__":
    unittest.main()
