"""Tests for the ETH-first AXGT holder discount tokenomics module.

Covers:

- Default tier table (boundaries + edge cases at every tier).
- Custom tier overrides via ``AXGT_DISCOUNT_TIERS`` (compact) and
  ``AXGT_DISCOUNT_TIERS_JSON`` (rich) env vars, including malformed input
  fall-back to defaults.
- ``apply_discount`` arithmetic for representative ETH prices.
- ``fetch_axgt_balance`` happy-path with mocked RPC, and safe failure when
  RPC throws / returns malformed data (``ok=False`` so callers default to
  no discount instead of blocking).
- End-to-end: ``verify_deposit`` ETH path applies the discount-adjusted
  minimum + credit rate; AXGT direct deposits return a clear error in the
  default ETH-first tokenomics mode.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

_tests_dir = os.path.dirname(os.path.abspath(__file__))
_repo_root = os.path.dirname(os.path.dirname(_tests_dir))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)


class TestDefaultTiers(unittest.TestCase):
    """Verify the suggested 0/100/1000/10000/100000 AXGT tier boundaries."""

    def setUp(self) -> None:
        # Clear any tier override so we test the built-in defaults.
        self._clear_env = patch.dict(
            os.environ,
            {
                "AXGT_DISCOUNT_TIERS": "",
                "AXGT_DISCOUNT_TIERS_JSON": "",
                "AXGT_DISCOUNT_TIERS_FILE": "",
            },
            clear=False,
        )
        self._clear_env.start()

    def tearDown(self) -> None:
        self._clear_env.stop()

    def test_tier0_zero_balance(self) -> None:
        from axonos_gate.discount import resolve_tier
        tier = resolve_tier(0)
        self.assertEqual(tier.index, 0)
        self.assertEqual(tier.discount_percent, 0)

    def test_tier0_just_below_tier1(self) -> None:
        from axonos_gate.discount import resolve_tier
        tier = resolve_tier(99)
        self.assertEqual(tier.index, 0)
        self.assertEqual(tier.discount_percent, 0)

    def test_tier1_at_threshold(self) -> None:
        from axonos_gate.discount import resolve_tier
        tier = resolve_tier(100)
        self.assertEqual(tier.index, 1)
        self.assertEqual(tier.discount_percent, 5)

    def test_tier1_just_below_tier2(self) -> None:
        from axonos_gate.discount import resolve_tier
        tier = resolve_tier(999)
        self.assertEqual(tier.index, 1)
        self.assertEqual(tier.discount_percent, 5)

    def test_tier2_at_threshold(self) -> None:
        from axonos_gate.discount import resolve_tier
        tier = resolve_tier(1000)
        self.assertEqual(tier.index, 2)
        self.assertEqual(tier.discount_percent, 10)

    def test_tier3_at_threshold(self) -> None:
        from axonos_gate.discount import resolve_tier
        tier = resolve_tier(10000)
        self.assertEqual(tier.index, 3)
        self.assertEqual(tier.discount_percent, 15)

    def test_tier4_at_threshold(self) -> None:
        from axonos_gate.discount import resolve_tier
        tier = resolve_tier(100000)
        self.assertEqual(tier.index, 4)
        self.assertEqual(tier.discount_percent, 25)

    def test_tier4_far_above_threshold(self) -> None:
        from axonos_gate.discount import resolve_tier
        tier = resolve_tier(10**9)
        self.assertEqual(tier.index, 4)
        self.assertEqual(tier.discount_percent, 25)

    def test_negative_balance_treated_as_tier0(self) -> None:
        from axonos_gate.discount import resolve_tier
        tier = resolve_tier(-5)
        self.assertEqual(tier.index, 0)
        self.assertEqual(tier.discount_percent, 0)


class TestTierOverrides(unittest.TestCase):
    def test_compact_env_override(self) -> None:
        with patch.dict(
            os.environ,
            {"AXGT_DISCOUNT_TIERS": "0:0,500:7,5000:14"},
            clear=False,
        ):
            from importlib import reload
            from axonos_gate import discount as disc_mod
            reload(disc_mod)
            tiers = disc_mod.get_tiers()
            self.assertEqual(len(tiers), 3)
            self.assertEqual(tiers[1].min_axgt, 500)
            self.assertEqual(tiers[1].discount_percent, 7)
            self.assertEqual(disc_mod.resolve_tier(7000).index, 2)

    def test_json_env_override(self) -> None:
        payload = json.dumps([
            {"min": 0, "discount": 0, "label": "free"},
            {"min": 250, "discount": 3, "label": "bronze"},
            {"min": 2500, "discount": 12, "label": "silver"},
        ])
        with patch.dict(
            os.environ,
            {"AXGT_DISCOUNT_TIERS_JSON": payload, "AXGT_DISCOUNT_TIERS": ""},
            clear=False,
        ):
            from importlib import reload
            from axonos_gate import discount as disc_mod
            reload(disc_mod)
            tier = disc_mod.resolve_tier(300)
            self.assertEqual(tier.label, "bronze")
            self.assertEqual(tier.discount_percent, 3)

    def test_malformed_env_falls_back_to_defaults(self) -> None:
        with patch.dict(
            os.environ,
            {"AXGT_DISCOUNT_TIERS": "not:a:valid:format"},
            clear=False,
        ):
            from importlib import reload
            from axonos_gate import discount as disc_mod
            reload(disc_mod)
            tiers = disc_mod.get_tiers()
            self.assertEqual([t.min_axgt for t in tiers], [0, 100, 1000, 10000, 100000])


class TestApplyDiscount(unittest.TestCase):
    def test_no_discount_returns_base(self) -> None:
        from axonos_gate.discount import apply_discount
        self.assertEqual(apply_discount(Decimal("0.0005"), 0), Decimal("0.0005"))

    def test_five_percent_on_min_eth(self) -> None:
        from axonos_gate.discount import apply_discount
        # 0.0005 * 0.95 = 0.000475
        self.assertEqual(apply_discount(Decimal("0.0005"), 5), Decimal("0.000475"))

    def test_twenty_five_percent_on_one_eth(self) -> None:
        from axonos_gate.discount import apply_discount
        self.assertEqual(apply_discount(Decimal("1"), 25), Decimal("0.75"))

    def test_zero_base_eth(self) -> None:
        from axonos_gate.discount import apply_discount
        self.assertEqual(apply_discount(Decimal("0"), 25), Decimal("0"))

    def test_full_discount_clamps_to_zero(self) -> None:
        from axonos_gate.discount import apply_discount
        self.assertEqual(apply_discount(Decimal("0.0005"), 100), Decimal("0"))


class TestFetchAxgtBalance(unittest.TestCase):
    def setUp(self) -> None:
        self.env_patch = patch.dict(
            os.environ,
            {
                "AXGT_RPC_URL": "https://rpc.example.com",
                "AXGT_CONTRACT_ADDRESS": "0x6112C3509A8a787df576028450FebB3786A2274d",
            },
            clear=False,
        )
        self.env_patch.start()

    def tearDown(self) -> None:
        self.env_patch.stop()

    def test_invalid_wallet_returns_not_ok(self) -> None:
        from axonos_gate.discount import fetch_axgt_balance
        result = fetch_axgt_balance("not-a-wallet")
        self.assertFalse(result.ok)
        self.assertEqual(result.balance_axgt, Decimal("0"))
        self.assertIn("Invalid", result.error or "")

    def test_missing_rpc_returns_not_ok(self) -> None:
        with patch.dict(os.environ, {"AXGT_RPC_URL": ""}, clear=False):
            from axonos_gate.discount import fetch_axgt_balance
            result = fetch_axgt_balance("0x" + "a" * 40)
            self.assertFalse(result.ok)
            self.assertIn("AXGT_RPC_URL", result.error or "")

    @patch("axonos_gate.discount.requests.post")
    def test_balanceof_happy_path(self, mock_post) -> None:
        from axonos_gate.discount import fetch_axgt_balance
        # Side-effect: balanceOf → 1500 AXGT (1500e18 wei); decimals() → 18.
        bal_wei_hex = "0x" + hex(1500 * 10**18)[2:].zfill(64)
        responses = [
            MagicMock(status_code=200, raise_for_status=lambda: None,
                      json=lambda: {"jsonrpc": "2.0", "id": 1, "result": bal_wei_hex}),
            MagicMock(status_code=200, raise_for_status=lambda: None,
                      json=lambda: {"jsonrpc": "2.0", "id": 1, "result": "0x12"}),  # 18
        ]
        mock_post.side_effect = responses
        result = fetch_axgt_balance("0x" + "f" * 40)
        self.assertTrue(result.ok)
        self.assertEqual(result.balance_axgt, Decimal("1500"))
        self.assertEqual(result.decimals, 18)
        self.assertEqual(result.floor_axgt(), 1500)

    @patch("axonos_gate.discount.requests.post")
    def test_balanceof_rpc_error_safe_fallback(self, mock_post) -> None:
        from axonos_gate.discount import fetch_axgt_balance
        mock_post.return_value = MagicMock(
            status_code=200, raise_for_status=lambda: None,
            json=lambda: {"jsonrpc": "2.0", "id": 1, "error": {"message": "boom"}},
        )
        result = fetch_axgt_balance("0x" + "f" * 40)
        self.assertFalse(result.ok)
        self.assertEqual(result.balance_axgt, Decimal("0"))
        self.assertTrue(result.error)

    @patch("axonos_gate.discount.requests.post")
    def test_balanceof_request_exception_safe_fallback(self, mock_post) -> None:
        from axonos_gate.discount import fetch_axgt_balance
        import requests

        mock_post.side_effect = requests.exceptions.ConnectionError("RPC down")
        result = fetch_axgt_balance("0x" + "f" * 40)
        self.assertFalse(result.ok)
        self.assertIn("RPC", result.error or "")

    @patch("axonos_gate.discount.requests.post")
    def test_balanceof_truncates_fractional_for_tier_lookup(self, mock_post) -> None:
        """A balance of 99.999 AXGT must resolve to Tier 0 (floor)."""
        from axonos_gate.discount import fetch_axgt_balance, resolve_tier
        # 99.999 AXGT in wei = 99999 * 10**15
        bal_wei_hex = "0x" + hex(99999 * 10**15)[2:].zfill(64)
        responses = [
            MagicMock(status_code=200, raise_for_status=lambda: None,
                      json=lambda: {"jsonrpc": "2.0", "id": 1, "result": bal_wei_hex}),
            MagicMock(status_code=200, raise_for_status=lambda: None,
                      json=lambda: {"jsonrpc": "2.0", "id": 1, "result": "0x12"}),
        ]
        mock_post.side_effect = responses
        result = fetch_axgt_balance("0x" + "1" * 40)
        self.assertTrue(result.ok)
        self.assertEqual(result.floor_axgt(), 99)
        tier = resolve_tier(result.floor_axgt())
        self.assertEqual(tier.index, 0)


class TestQuoteForBalance(unittest.TestCase):
    def test_quote_for_min_eth_at_each_tier(self) -> None:
        from axonos_gate.discount import quote_for_balance
        cases = [
            (0, "0.0005", "0.0005", 0),
            (100, "0.0005", "0.000475", 5),
            (1000, "0.0005", "0.00045", 10),
            (10000, "0.0005", "0.000425", 15),
            (100000, "0.0005", "0.000375", 25),
        ]
        for balance, base, expected_final, expected_pct in cases:
            with self.subTest(balance=balance):
                q = quote_for_balance(Decimal(base), balance)
                self.assertEqual(q["discount_percent"], expected_pct)
                self.assertEqual(Decimal(q["final_eth"]), Decimal(expected_final))


class TestVerifyDepositEthFirst(unittest.TestCase):
    """End-to-end: ETH path applies discount; AXGT direct path is disabled by default."""

    def setUp(self) -> None:
        self.env_patch = patch.dict(
            os.environ,
            {
                "AXGT_RPC_URL": "https://rpc.example.com",
                "AXGT_CONTRACT_ADDRESS": "0x6112c3509a8a787df576028450febb3786a2274d",
                "AXGT_REVENUE_WALLET": "0xrevenue00000000000000000000000000000001".lower(),
                "ETH_MIN_DEPOSIT": "0.0005",
                "ETH_CREDIT_PER_ETH_MINUTES": "120000",
                "AXGT_DEPOSIT_MIN_CONFIRMATIONS": "1",
                "AXGT_ENABLE_AXGT_DEPOSITS": "",  # default → disabled
                "AXGT_DISCOUNT_TIERS": "",
                "AXGT_DISCOUNT_TIERS_JSON": "",
            },
            clear=False,
        )
        self.env_patch.start()

    def tearDown(self) -> None:
        self.env_patch.stop()

    @patch("axonos_gate.deposit_verifier._import_discount")
    @patch("axonos_gate.deposit_ledger.tx_hash_already_credited")
    @patch("axonos_gate.deposit_ledger.credit_eth_deposit")
    @patch("axonos_gate.deposit_verifier._rpc")
    def test_eth_with_25_percent_discount_credits_full_minutes(
        self, mock_rpc, mock_credit, mock_already, mock_disc_import
    ) -> None:
        from axonos_gate import deposit_verifier as verifier_mod
        from axonos_gate.deposit_verifier import verify_deposit

        sender = "0x1111111111111111111111111111111111111111"
        revenue = "0xrevenue00000000000000000000000000000001"

        # User pays the discounted minimum: 0.000375 ETH (0.0005 * 0.75).
        eth_amount = Decimal("0.000375")
        value_wei = int(eth_amount * Decimal(10 ** 18))

        def rpc_side_effect(url, method, params):
            if method == "eth_getTransactionByHash":
                return {"from": sender, "to": revenue, "value": hex(value_wei)}
            if method == "eth_getTransactionReceipt":
                return {"status": "0x1", "blockNumber": "0x10", "logs": []}
            if method == "eth_blockNumber":
                return "0x12"
            return None

        mock_rpc.side_effect = rpc_side_effect
        mock_already.return_value = False

        # Simulate Tier 4 (≥100000 AXGT → 25% discount) via the discount module.
        fake_disc = MagicMock()
        bal = MagicMock()
        bal.ok = True
        bal.balance_axgt = Decimal("123456")
        bal.floor_axgt = MagicMock(return_value=123456)
        fake_disc.fetch_axgt_balance = MagicMock(return_value=bal)
        fake_tier = MagicMock(index=4, label="Tier 4", min_axgt=100000, discount_percent=25.0)
        fake_disc.resolve_tier = MagicMock(return_value=fake_tier)
        mock_disc_import.return_value = fake_disc

        # Credit ledger returns ok with computed remaining minutes.
        mock_credit.return_value = (True, 60.0, None)

        result = verify_deposit(authenticated_wallet=sender, tx_hash="0x" + "a" * 64)
        self.assertTrue(result["verified"], result)
        self.assertEqual(result["deposit_currency"], "ETH")
        self.assertEqual(result["tier"]["discount_percent"], 25.0)
        # base credit rate 120000 / (1 - 0.25) = 160000 min/ETH
        # 0.000375 ETH * 160000 = 60 minutes (parity with non-discounted user paying 0.0005)
        credited_call = mock_credit.call_args
        credited_minutes = float(credited_call.args[2])
        self.assertAlmostEqual(credited_minutes, 60.0, places=4)

    @patch("axonos_gate.deposit_verifier._import_discount")
    @patch("axonos_gate.deposit_ledger.tx_hash_already_credited")
    @patch("axonos_gate.deposit_ledger.credit_eth_deposit")
    @patch("axonos_gate.deposit_ledger.record_verification_reject")
    @patch("axonos_gate.deposit_verifier._rpc")
    def test_eth_below_discount_adjusted_min_rejected(
        self, mock_rpc, mock_record, mock_credit, mock_already, mock_disc_import
    ) -> None:
        from axonos_gate.deposit_verifier import verify_deposit

        sender = "0x1111111111111111111111111111111111111111"
        revenue = "0xrevenue00000000000000000000000000000001"
        # Tier 1 (5%) → adjusted min = 0.000475 ETH; user only paid 0.0004 ETH.
        value_wei = int(Decimal("0.0004") * Decimal(10 ** 18))

        def rpc_side_effect(url, method, params):
            if method == "eth_getTransactionByHash":
                return {"from": sender, "to": revenue, "value": hex(value_wei)}
            if method == "eth_getTransactionReceipt":
                return {"status": "0x1", "blockNumber": "0x10", "logs": []}
            if method == "eth_blockNumber":
                return "0x12"
            return None

        mock_rpc.side_effect = rpc_side_effect
        mock_already.return_value = False

        fake_disc = MagicMock()
        bal = MagicMock(ok=True, balance_axgt=Decimal("250"))
        bal.floor_axgt = MagicMock(return_value=250)
        fake_disc.fetch_axgt_balance = MagicMock(return_value=bal)
        fake_disc.resolve_tier = MagicMock(
            return_value=MagicMock(index=1, label="Tier 1", min_axgt=100, discount_percent=5.0)
        )
        mock_disc_import.return_value = fake_disc

        result = verify_deposit(authenticated_wallet=sender, tx_hash="0x" + "b" * 64)
        self.assertFalse(result["verified"], result)
        self.assertIn("below minimum", result["error"].lower())
        mock_credit.assert_not_called()

    @patch("axonos_gate.deposit_verifier._import_discount")
    @patch("axonos_gate.deposit_ledger.tx_hash_already_credited")
    @patch("axonos_gate.deposit_ledger.credit_eth_deposit")
    @patch("axonos_gate.deposit_verifier._rpc")
    def test_eth_with_failed_balance_check_defaults_to_no_discount(
        self, mock_rpc, mock_credit, mock_already, mock_disc_import
    ) -> None:
        """RPC failure must NOT block payment — user pays full price, no discount."""
        from axonos_gate.deposit_verifier import verify_deposit

        sender = "0x1111111111111111111111111111111111111111"
        revenue = "0xrevenue00000000000000000000000000000001"
        # User paid the *full* base minimum, so no discount is fine.
        value_wei = int(Decimal("0.0005") * Decimal(10 ** 18))

        def rpc_side_effect(url, method, params):
            if method == "eth_getTransactionByHash":
                return {"from": sender, "to": revenue, "value": hex(value_wei)}
            if method == "eth_getTransactionReceipt":
                return {"status": "0x1", "blockNumber": "0x10", "logs": []}
            if method == "eth_blockNumber":
                return "0x12"
            return None

        mock_rpc.side_effect = rpc_side_effect
        mock_already.return_value = False

        fake_disc = MagicMock()
        bal = MagicMock(ok=False, balance_axgt=Decimal("0"), error="RPC down")
        bal.floor_axgt = MagicMock(return_value=0)
        fake_disc.fetch_axgt_balance = MagicMock(return_value=bal)
        fake_disc.resolve_tier = MagicMock(
            return_value=MagicMock(index=0, label="Tier 0", min_axgt=0, discount_percent=0.0)
        )
        mock_disc_import.return_value = fake_disc

        mock_credit.return_value = (True, 60.0, None)
        result = verify_deposit(authenticated_wallet=sender, tx_hash="0x" + "c" * 64)
        self.assertTrue(result["verified"], result)
        self.assertEqual(result["tier"]["discount_percent"], 0.0)
        self.assertFalse(result["tier"]["balance_check_ok"])

    @patch("axonos_gate.deposit_ledger.tx_hash_already_credited")
    @patch("axonos_gate.deposit_ledger.record_verification_reject")
    @patch("axonos_gate.deposit_verifier._rpc")
    def test_axgt_direct_deposit_rejected_in_default_mode(
        self, mock_rpc, mock_record, mock_already
    ) -> None:
        """In ETH-first mode (default), a direct AXGT transfer must be rejected."""
        from axonos_gate.deposit_verifier import verify_deposit

        sender = "0x1111111111111111111111111111111111111111"
        contract = "0x6112c3509a8a787df576028450febb3786a2274d"

        # Tx targets the AXGT contract, not the revenue wallet directly,
        # and has zero ETH value — i.e. an AXGT ERC-20 transfer call.
        def rpc_side_effect(url, method, params):
            if method == "eth_getTransactionByHash":
                return {"from": sender, "to": contract, "value": "0x0"}
            if method == "eth_getTransactionReceipt":
                return {"status": "0x1", "blockNumber": "0x10", "logs": []}
            if method == "eth_blockNumber":
                return "0x12"
            return None

        mock_rpc.side_effect = rpc_side_effect
        mock_already.return_value = False
        result = verify_deposit(authenticated_wallet=sender, tx_hash="0x" + "d" * 64)
        self.assertFalse(result["verified"])
        self.assertIn("ETH", result["error"])  # message references ETH-first model


if __name__ == "__main__":
    unittest.main()
