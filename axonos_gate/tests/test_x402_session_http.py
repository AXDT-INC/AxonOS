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
            "AXGT_X402_FACILITATOR_ENABLED": "false",
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

    @patch("axonos_gate.gate_server.verify_agentlink_header")
    @patch("gate_server.verify_agentlink_header")
    def test_agentlink_verified_annotates_session(self, mock_verify1, mock_verify2):
        # When AgentLink is enabled, and header is verified
        mock_val = {
            "verified": True,
            "agent": "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266",
            "owner": "0x0f1dd54cb5351d91ed6fb0d4208b6eda894c4ca7",
            "chainId": "eip155:8453",
            "reason": "active"
        }
        mock_verify1.return_value = mock_val
        mock_verify2.return_value = mock_val
        
        prepaid = {"verified": True, "remaining_minutes": 120.0}
        claim_result = {"granted": True, "remaining_seconds": 7200, "ssh_host": "ssh.example.com", "ssh_port": 2222}
        
        with patch.dict(os.environ, {"AXGT_AGENTLINK_ENABLED": "true"}), \
             patch.object(gate_server, "get_wallet_access_status", return_value=prepaid), \
             patch.object(gate_server, "_issue_gate_auth_token", return_value=("tok", 3600)), \
             patch.object(gate_server, "validate_ssh_public_key", return_value="ssh-ed25519 AAAA"), \
             patch.object(gate_server, "try_claim_session", return_value=claim_result):
            
            resp = self.client.post(
                "/api/x402/session",
                json={"wallet_address": _WALLET, "ssh_pubkey": "ssh-ed25519 AAAA"},
                headers={"agentlink": "valid_base64_payload"}
            )
            
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["granted"])
        self.assertIn("agentlink", body)
        self.assertTrue(body["agentlink"]["verified"])
        self.assertEqual(body["agentlink"]["chainId"], "eip155:8453")
        # Addresses must be masked
        self.assertEqual(body["agentlink"]["agent"], "0xf39f...2266")
        self.assertEqual(body["agentlink"]["owner"], "0x0f1d...4ca7")

    @patch("axonos_gate.gate_server.verify_agentlink_header")
    @patch("gate_server.verify_agentlink_header")
    def test_agentlink_failed_annotates_session_and_continues(self, mock_verify1, mock_verify2):
        # When AgentLink is enabled, and header fails verification, we do not block
        mock_val = {
            "verified": False,
            "reason": "expired_issued_at",
            "error": "issuedAt is too old"
        }
        mock_verify1.return_value = mock_val
        mock_verify2.return_value = mock_val
        
        prepaid = {"verified": True, "remaining_minutes": 120.0}
        claim_result = {"granted": True, "remaining_seconds": 7200, "ssh_host": "ssh.example.com", "ssh_port": 2222}
        
        with patch.dict(os.environ, {"AXGT_AGENTLINK_ENABLED": "true"}), \
             patch.object(gate_server, "get_wallet_access_status", return_value=prepaid), \
             patch.object(gate_server, "_issue_gate_auth_token", return_value=("tok", 3600)), \
             patch.object(gate_server, "validate_ssh_public_key", return_value="ssh-ed25519 AAAA"), \
             patch.object(gate_server, "try_claim_session", return_value=claim_result):
            
            resp = self.client.post(
                "/api/x402/session",
                json={"wallet_address": _WALLET, "ssh_pubkey": "ssh-ed25519 AAAA"},
                headers={"agentlink": "expired_base64_payload"}
            )
            
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["granted"])
        self.assertIn("agentlink", body)
        self.assertFalse(body["agentlink"]["verified"])
        self.assertEqual(body["agentlink"]["reason"], "expired_issued_at")

    @patch("axonos_gate.gate_server.verify_agentlink_header")
    @patch("gate_server.verify_agentlink_header")
    def test_agentlink_disabled_ignores_header(self, mock_verify1, mock_verify2):
        # When AgentLink is disabled, the header is ignored entirely
        prepaid = {"verified": True, "remaining_minutes": 120.0}
        claim_result = {"granted": True, "remaining_seconds": 7200, "ssh_host": "ssh.example.com", "ssh_port": 2222}
        
        with patch.dict(os.environ, {"AXGT_AGENTLINK_ENABLED": "false"}), \
             patch.object(gate_server, "get_wallet_access_status", return_value=prepaid), \
             patch.object(gate_server, "_issue_gate_auth_token", return_value=("tok", 3600)), \
             patch.object(gate_server, "validate_ssh_public_key", return_value="ssh-ed25519 AAAA"), \
             patch.object(gate_server, "try_claim_session", return_value=claim_result):
            
            resp = self.client.post(
                "/api/x402/session",
                json={"wallet_address": _WALLET, "ssh_pubkey": "ssh-ed25519 AAAA"},
                headers={"agentlink": "some_payload"}
            )
            
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["granted"])
        self.assertNotIn("agentlink", body)
        mock_verify1.assert_not_called()
        mock_verify2.assert_not_called()

    @patch("gate_server.verify_agentlink_header")
    def test_x402_resource_url_no_double_append(self, mock_verify):
        mock_verify.return_value = {"verified": False, "reason": "test"}
        prepaid = {"verified": True, "remaining_minutes": 120.0}
        claim_result = {"granted": True, "remaining_seconds": 7200, "ssh_host": "ssh.example.com", "ssh_port": 2222}
        
        with patch.dict(os.environ, {
            "AXGT_AGENTLINK_ENABLED": "true",
            "X402_RESOURCE_URL": "https://custom.axonos.io/api/x402/session"
        }), \
             patch.object(gate_server, "get_wallet_access_status", return_value=prepaid), \
             patch.object(gate_server, "_issue_gate_auth_token", return_value=("tok", 3600)), \
             patch.object(gate_server, "validate_ssh_public_key", return_value="ssh-ed25519 AAAA"), \
             patch.object(gate_server, "try_claim_session", return_value=claim_result):
            
            self.client.post(
                "/api/x402/session",
                json={"wallet_address": _WALLET, "ssh_pubkey": "ssh-ed25519 AAAA"},
                headers={"agentlink": "valid_payload"}
            )
        
        mock_verify.assert_called_once_with("valid_payload", "https://custom.axonos.io/api/x402/session")

    @patch("gate_server.verify_agentlink_header")
    def test_x402_access_expected_uri(self, mock_verify):
        mock_verify.return_value = {"verified": False, "reason": "test"}
        status = {"verified": True, "remaining_minutes": 10.0}
        
        with patch.dict(os.environ, {
            "AXGT_AGENTLINK_ENABLED": "true",
            "X402_ACCESS_RESOURCE_URL": "https://custom.axonos.io/custom-access-endpoint"
        }), \
             patch.object(gate_server, "get_wallet_access_status", return_value=status):
            
            self.client.get(
                "/api/x402/access?minutes=10",
                headers={"agentlink": "valid_payload"}
            )
            
        mock_verify.assert_called_once_with("valid_payload", "https://custom.axonos.io/custom-access-endpoint")

    def test_discovery_extensions_supported_chains(self):
        from x402_verifier import build_agentlink_declaration
        with patch.dict(os.environ, {
            "AXGT_AGENTLINK_ENABLED": "true",
            "USDC_CHAIN_ID": "8453"
        }):
            decl = build_agentlink_declaration()
            supported = decl.get("supportedChains", [])
            types = [c["type"] for c in supported]
            self.assertIn("eip191", types)
            self.assertNotIn("eip1271", types)

if __name__ == "__main__":
    unittest.main()
