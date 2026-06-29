"""
HTTP-level tests for POST /api/x402/session on the Flask gate server.

Verifies the x402 Bazaar / Agentic Market contract end-to-end:
  - an unpaid request (no X-PAYMENT) returns 402 — NOT 400 — before any
    wallet_address / ssh_pubkey validation, with a PAYMENT-REQUIRED header whose
    decoded PaymentRequired carries the Bazaar extension at the root;
  - the paid path still validates wallet_address before settling;
  - the prepaid path still validates ssh_pubkey before granting.

Flask is a runtime dependency of the gate server but not of the unit-test
environment, so the whole module skips cleanly when it (or the server) can't be
imported. No paid / on-chain calls are made — settlement is never reached.
"""

import base64
import json
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

_tests_dir = os.path.dirname(os.path.abspath(__file__))
_pkg_dir = os.path.dirname(_tests_dir)
_repo_root = os.path.dirname(_pkg_dir)
for _p in (_pkg_dir, _repo_root):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    import flask  # noqa: F401
    import gate_server  # noqa: F401
    _HAVE_GATE = True
except BaseException:  # ImportError, or SystemExit from a missing hard dep
    _HAVE_GATE = False

_WALLET = "0x" + "a" * 40
_BAD_WALLET = "not-a-wallet"


@unittest.skipUnless(_HAVE_GATE, "flask / gate_server not importable")
class TestX402SessionHttp(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "AXGT_X402_BAZAAR_DISCOVERABLE": "true",
            "AXGT_PUBLIC_BASE_URL": "https://app.axonos.io",
            "X402_RESOURCE": "/api/x402/session",
            "USDC_CONTRACT_ADDRESS": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
            "AXGT_REVENUE_WALLET": "0x1111111111111111111111111111111111111111",
            "USDC_CHAIN_ID": "8453",
        })
        self.env.start()
        # The session endpoint short-circuits to 503 unless the session manager is
        # marked available; force it on so the request reaches the x402 gate.
        self.mgr = patch.object(gate_server, "_session_mgr_available", True)
        self.mgr.start()
        gate_server.app.testing = True
        self.client = gate_server.app.test_client()

    def tearDown(self):
        self.mgr.stop()
        self.env.stop()

    def test_empty_unpaid_request_returns_402_not_400(self):
        resp = self.client.post("/api/x402/session", json={})
        self.assertEqual(resp.status_code, 402)

    def test_402_carries_payment_required_header_with_root_bazaar(self):
        resp = self.client.post("/api/x402/session", json={})
        self.assertEqual(resp.status_code, 402)
        header = resp.headers.get("PAYMENT-REQUIRED")
        self.assertTrue(header, "PAYMENT-REQUIRED header missing on 402")
        decoded = json.loads(base64.b64decode(header).decode())
        self.assertIn("info", decoded["extensions"]["bazaar"])
        self.assertIn("schema", decoded["extensions"]["bazaar"])
        self.assertIn("info", decoded["accepts"][0]["extensions"]["bazaar"])

    def test_paid_path_validates_wallet_before_settling(self):
        # X-PAYMENT present (so the unpaid gate is skipped) but the wallet is
        # invalid → 400 wallet error, and settlement is never invoked.
        with patch.object(gate_server, "settle_x402_payment", MagicMock()) as settle:
            resp = self.client.post(
                "/api/x402/session",
                json={"wallet_address": _BAD_WALLET, "ssh_pubkey": "ssh-ed25519 AAAA"},
                headers={"X-PAYMENT": "dGVzdA=="},
            )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("wallet", resp.get_json().get("error", "").lower())
        settle.assert_not_called()

    def test_prepaid_path_validates_ssh_pubkey_before_granting(self):
        # A prepaid wallet (no payment) must NOT 402, but must still 400 on a
        # missing ssh_pubkey — i.e. inputs are validated after the gate, before any
        # session is claimed.
        prepaid = {"verified": True, "remaining_minutes": 120.0}
        with patch.object(gate_server, "get_wallet_access_status", return_value=prepaid), \
             patch.object(gate_server, "_issue_gate_auth_token", return_value=("tok", 3600)), \
             patch.object(gate_server, "try_claim_session", MagicMock()) as claim:
            resp = self.client.post("/api/x402/session", json={"wallet_address": _WALLET})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("ssh_pubkey", resp.get_json().get("error", "").lower())
        claim.assert_not_called()


if __name__ == "__main__":
    unittest.main()
