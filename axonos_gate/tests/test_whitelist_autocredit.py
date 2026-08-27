"""Tests for the explicit, bounded, token-free test-credit rail."""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_TESTS_DIR = Path(__file__).resolve().parent
_PKG_DIR = _TESTS_DIR.parent
_REPO_ROOT = _PKG_DIR.parent
for _path in (str(_PKG_DIR), str(_REPO_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import axgt_verifier

try:
    import flask  # noqa: F401
    import gate_server
    _HAVE_GATE = True
except BaseException:
    _HAVE_GATE = False


ELIGIBLE = "0x1111111111111111111111111111111111111111"
ELIGIBLE_2 = "0x2222222222222222222222222222222222222222"
INELIGIBLE = "0x3333333333333333333333333333333333333333"
SENTINEL_TX = "0xffffffff" + "ab" * 28
REQUEST_ID = "request-0001"


def make_ledger(result=None):
    ledger = MagicMock()
    ledger.credit_test_grant.return_value = result or {
        "ok": True,
        "replayed": False,
        "credited_minutes": 60.0,
        "remaining_minutes": 60.0,
        "payment_rail": "eth",
    }
    return ledger


class TestTestCreditPolicy(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(
            os.environ,
            {
                "AXONOS_TEST_CREDITS_ENABLED": "true",
                "AXONOS_TEST_CREDIT_WALLETS": f"{ELIGIBLE},{ELIGIBLE_2}",
                "AXONOS_WHITELISTED_WALLETS": "",
                "AXONOS_TEST_CREDIT_GRANT_MINUTES": "60",
                "AXONOS_TEST_CREDIT_MAX_BALANCE_MINUTES": "60",
                "AXONOS_WHITELIST_AUTO_CREDIT_MINUTES": "",
                "AXONOS_WHITELIST_MAX_BALANCE_MINUTES": "",
            },
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_feature_is_disabled_by_default_and_wallet_list_never_enables_it(self):
        with patch.dict(
            os.environ,
            {
                "AXONOS_TEST_CREDITS_ENABLED": "",
                "AXONOS_TEST_CREDIT_WALLETS": "",
                "AXONOS_WHITELISTED_WALLETS": ELIGIBLE,
            },
        ):
            self.assertFalse(axgt_verifier.test_credits_enabled())
            self.assertFalse(axgt_verifier.is_wallet_whitelisted(ELIGIBLE))

    def test_new_wallet_list_requires_explicit_enable(self):
        self.assertTrue(axgt_verifier.test_credits_enabled())
        self.assertTrue(axgt_verifier.is_wallet_whitelisted(ELIGIBLE.upper()))
        self.assertTrue(axgt_verifier.is_wallet_whitelisted(ELIGIBLE_2))
        self.assertFalse(axgt_verifier.is_wallet_whitelisted(INELIGIBLE))
        self.assertFalse(axgt_verifier.is_wallet_whitelisted("not-a-wallet"))

    def test_legacy_wallet_list_is_eligibility_alias_only(self):
        with patch.dict(
            os.environ,
            {
                "AXONOS_TEST_CREDIT_WALLETS": "",
                "AXONOS_WHITELISTED_WALLETS": ELIGIBLE,
            },
        ):
            self.assertTrue(axgt_verifier.is_wallet_whitelisted(ELIGIBLE))

    def test_grant_and_cap_are_finite_and_hard_bounded(self):
        for bad in ("nan", "inf", "-1", "0", "1440.01", "1e999", "junk"):
            with self.subTest(grant=bad), patch.dict(
                os.environ, {"AXONOS_TEST_CREDIT_GRANT_MINUTES": bad}
            ):
                self.assertEqual(axgt_verifier.get_test_credit_grant_minutes(), 60.0)
        for bad in ("nan", "inf", "-1", "0", "10080.01", "1e999", "junk"):
            with self.subTest(cap=bad), patch.dict(
                os.environ, {"AXONOS_TEST_CREDIT_MAX_BALANCE_MINUTES": bad}
            ):
                self.assertEqual(axgt_verifier.get_test_credit_max_balance_minutes(), 60.0)

        with patch.dict(
            os.environ,
            {
                "AXONOS_TEST_CREDIT_GRANT_MINUTES": "1440",
                "AXONOS_TEST_CREDIT_MAX_BALANCE_MINUTES": "10080",
            },
        ):
            self.assertEqual(axgt_verifier.get_test_credit_grant_minutes(), 1440.0)
            self.assertEqual(axgt_verifier.get_test_credit_max_balance_minutes(), 10080.0)

    def test_old_numeric_whitelist_settings_do_not_configure_new_rail(self):
        with patch.dict(
            os.environ,
            {
                "AXONOS_TEST_CREDIT_GRANT_MINUTES": "",
                "AXONOS_TEST_CREDIT_MAX_BALANCE_MINUTES": "",
                "AXONOS_WHITELIST_AUTO_CREDIT_MINUTES": "90",
                "AXONOS_WHITELIST_MAX_BALANCE_MINUTES": "120",
            },
        ):
            self.assertEqual(axgt_verifier.get_test_credit_grant_minutes(), 60.0)
            self.assertEqual(axgt_verifier.get_test_credit_max_balance_minutes(), 60.0)

    def test_success_uses_dedicated_ledger_function_and_provenance(self):
        ledger = make_ledger()
        with patch.object(axgt_verifier, "_get_deposit_ledger", return_value=ledger):
            result = axgt_verifier.grant_test_credit(ELIGIBLE, "ETH", REQUEST_ID)

        self.assertTrue(result["verified"])
        self.assertTrue(result["test_credit"])
        self.assertNotIn("mock", result)
        self.assertTrue(result["test_credit_eligible"])
        self.assertTrue(result["is_whitelisted"])
        self.assertEqual(result["credit_source"], "test_credit")
        self.assertEqual(result["payment_rail"], "eth")
        ledger.credit_test_grant.assert_called_once_with(
            wallet_address=ELIGIBLE,
            grant_minutes=60.0,
            max_balance_minutes=60.0,
            request_id=REQUEST_ID,
            payment_rail="eth",
            additive=True,
        )

    def test_disabled_and_ineligible_requests_never_touch_ledger(self):
        ledger = make_ledger()
        with patch.object(axgt_verifier, "_get_deposit_ledger", return_value=ledger), patch.dict(
            os.environ, {"AXONOS_TEST_CREDITS_ENABLED": "false"}
        ):
            disabled = axgt_verifier.grant_test_credit(ELIGIBLE, "eth", REQUEST_ID)
        self.assertEqual(disabled["error_code"], "test_credits_disabled")

        with patch.object(axgt_verifier, "_get_deposit_ledger", return_value=ledger):
            ineligible = axgt_verifier.grant_test_credit(INELIGIBLE, "eth", REQUEST_ID)
        self.assertEqual(ineligible["error_code"], "not_test_credit_eligible")
        ledger.credit_test_grant.assert_not_called()

    def test_rail_and_request_id_are_strictly_validated(self):
        ledger = make_ledger()
        with patch.object(axgt_verifier, "_get_deposit_ledger", return_value=ledger):
            invalid_rail = axgt_verifier.grant_test_credit(ELIGIBLE, "btc", REQUEST_ID)
            invalid_request = axgt_verifier.grant_test_credit(ELIGIBLE, "usdc", "short")
        self.assertEqual(invalid_rail["error_code"], "invalid_rail")
        self.assertEqual(invalid_request["error_code"], "invalid_request_id")
        ledger.credit_test_grant.assert_not_called()

    def test_cap_is_successful_no_op_and_request_mismatch_is_conflict(self):
        capped_ledger = make_ledger(
            {
                "ok": True,
                "capped": True,
                "no_op": True,
                "replayed": False,
                "credited_minutes": 0.0,
                "remaining_minutes": 60.0,
            }
        )
        with patch.object(axgt_verifier, "_get_deposit_ledger", return_value=capped_ledger):
            capped = axgt_verifier.grant_test_credit(ELIGIBLE, "axgt", REQUEST_ID)
        self.assertTrue(capped["verified"])
        self.assertTrue(capped["capped"])
        self.assertTrue(capped["no_op"])
        self.assertEqual(axgt_verifier.test_credit_http_status(capped), 200)

        mismatch_ledger = make_ledger(
            {"ok": False, "error_code": "request_mismatch", "error": "mismatch"}
        )
        with patch.object(axgt_verifier, "_get_deposit_ledger", return_value=mismatch_ledger):
            mismatch = axgt_verifier.grant_test_credit(ELIGIBLE, "axgt", REQUEST_ID)
        self.assertEqual(axgt_verifier.test_credit_http_status(mismatch), 409)

    def test_wallet_status_exposes_new_and_legacy_eligibility_fields(self):
        ledger = MagicMock()
        ledger.init_once.return_value = True
        ledger.get_deposit_status.return_value = {
            "remaining_minutes": 0.0,
            "consumed_minutes": 0.0,
            "credited_minutes_total": 0.0,
        }
        with patch.object(axgt_verifier, "_get_deposit_ledger", return_value=ledger), patch.object(
            axgt_verifier, "_get_axgt_balance_display", return_value=None
        ):
            status = axgt_verifier.get_wallet_access_status(ELIGIBLE)
        self.assertTrue(status["test_credit_eligible"])
        self.assertEqual(status["test_credit_eligible"], status["is_whitelisted"])
        self.assertEqual(status["test_credit_grant_minutes"], 60.0)
        self.assertEqual(status["test_credit_max_balance_minutes"], 60.0)

    def test_wallet_status_does_not_advertise_test_credit_policy_to_ineligible_wallet(self):
        ledger = MagicMock()
        ledger.init_once.return_value = True
        ledger.get_deposit_status.return_value = {
            "remaining_minutes": 0.0,
            "consumed_minutes": 0.0,
            "credited_minutes_total": 0.0,
        }
        with patch.object(axgt_verifier, "_get_deposit_ledger", return_value=ledger), patch.object(
            axgt_verifier, "_get_axgt_balance_display", return_value=None
        ):
            status = axgt_verifier.get_wallet_access_status(INELIGIBLE)
        self.assertFalse(status["test_credit_eligible"])
        self.assertNotIn("test_credit_grant_minutes", status)
        self.assertNotIn("test_credit_max_balance_minutes", status)


class TestRealDepositVerifiers(unittest.TestCase):
    def test_old_sentinel_hash_no_longer_bypasses_axgt_or_eth_verification(self):
        import deposit_verifier

        with patch.object(deposit_verifier, "_get_revenue_wallet", return_value=""), patch.object(
            deposit_verifier, "_get_rpc_url", return_value=""
        ):
            result = deposit_verifier.verify_deposit(ELIGIBLE, SENTINEL_TX)
        self.assertFalse(result["verified"])
        self.assertNotIn("mock", result)
        self.assertIn("not configured", result["error"].lower())

    def test_old_sentinel_hash_no_longer_bypasses_usdc_verification(self):
        import x402_verifier

        with patch.object(x402_verifier, "usdc_deposits_enabled", return_value=False):
            result = x402_verifier.verify_usdc_deposit(ELIGIBLE, SENTINEL_TX)
        self.assertFalse(result["verified"])
        self.assertNotIn("mock", result)
        self.assertIn("disabled", result["error"].lower())


@unittest.skipUnless(_HAVE_GATE, "Flask / gate_server not importable")
class TestTestCreditHttp(unittest.TestCase):
    def setUp(self):
        gate_server.app.testing = True
        self.client = gate_server.app.test_client()

    def test_endpoint_requires_wallet_bound_auth(self):
        with patch.object(
            gate_server,
            "_require_auth_token",
            return_value=({"verified": False, "error": "Valid auth token required"}, 401),
        ), patch.object(gate_server, "grant_test_credit") as grant:
            response = self.client.post(
                "/api/auth/test-credit",
                json={"wallet_address": ELIGIBLE, "rail": "eth", "request_id": REQUEST_ID},
            )
        self.assertEqual(response.status_code, 401)
        grant.assert_not_called()

    def test_success_rotates_auth_token(self):
        grant_result = {
            "verified": True,
            "test_credit": True,
            "credit_source": "test_credit",
            "payment_rail": "usdc",
            "credited_minutes": 60.0,
            "remaining_minutes": 60.0,
        }
        with patch.object(gate_server, "_require_auth_token", return_value=None), patch.object(
            gate_server, "grant_test_credit", return_value=grant_result
        ) as grant, patch.object(
            gate_server, "_issue_gate_auth_token", return_value=("rotated-token", 3600)
        ):
            response = self.client.post(
                "/api/auth/test-credit",
                json={"wallet_address": ELIGIBLE, "rail": "usdc", "request_id": REQUEST_ID},
            )
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["auth_token"], "rotated-token")
        self.assertEqual(body["credit_source"], "test_credit")
        grant.assert_called_once_with(ELIGIBLE, "usdc", REQUEST_ID)

    def test_websockify_payment_routes_use_exact_dispatch(self):
        source = (_PKG_DIR / "websockify_gate.py").read_text(encoding="utf-8")
        self.assertIn("if ponly == '/api/auth/test-credit':", source)
        self.assertIn("if ponly == '/api/auth/verify-deposit-auto':", source)
        self.assertNotIn("self.path.startswith('/api/auth/verify-deposit')", source)

    def test_telemetry_separates_paid_current_test_and_legacy_test_credit(self):
        for path in (_PKG_DIR / "gate_server.py", _PKG_DIR / "websockify_gate.py"):
            source = path.read_text(encoding="utf-8")
            self.assertIn('"paid_credited_minutes"', source)
            self.assertIn('"test_credited_minutes"', source)
            self.assertIn('"legacy_test_credited_minutes"', source)
            self.assertIn("COALESCE(reference_tx_hash, '') !~", source)


if __name__ == "__main__":
    unittest.main()
