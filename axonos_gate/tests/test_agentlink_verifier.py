import unittest
import os
import sys
import time
import json
import base64
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta

# Ensure local imports work
_tests_dir = os.path.dirname(os.path.abspath(__file__))
_pkg_dir = os.path.dirname(_tests_dir)
if _pkg_dir not in sys.path:
    sys.path.insert(0, _pkg_dir)

from eth_account import Account
from eth_account.messages import encode_defunct
import agentlink_verifier

class MockCursor:
    def __init__(self, db_state):
        self.db_state = db_state
        self.result = None
        self.rowcount = 0

    def execute(self, query, params=None):
        q = query.lower()
        if "select 1 from axgt_agentlink_nonces" in q:
            nonce = params[0]
            now = params[1]
            if nonce in self.db_state and self.db_state[nonce]["expires_at"] > now:
                self.result = (1,)
            else:
                self.result = None
        elif "delete from axgt_agentlink_nonces" in q:
            now = params[0]
            for n in list(self.db_state.keys()):
                if self.db_state[n]["expires_at"] <= now:
                    del self.db_state[n]
        elif "insert into axgt_agentlink_nonces" in q:
            nonce, agent_address, chain_id, resource_uri_hash, issued_at, expires_at = params
            if nonce in self.db_state:
                self.rowcount = 0
            else:
                self.db_state[nonce] = {
                    "agent_address": agent_address,
                    "chain_id": chain_id,
                    "resource_uri_hash": resource_uri_hash,
                    "issued_at": issued_at,
                    "expires_at": expires_at
                }
                self.rowcount = 1

    def fetchone(self):
        return self.result

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

class MockConnection:
    def __init__(self, db_state):
        self.db_state = db_state

    def cursor(self):
        return MockCursor(self.db_state)

    def commit(self):
        pass

    def close(self):
        pass

class TestAgentLinkVerifier(unittest.TestCase):
    def setUp(self):
        self.db_state = {}
        self.db_patcher = patch("agentlink_verifier._get_connection", side_effect=lambda: MockConnection(self.db_state))
        self.db_patcher.start()
        
        self.env_patcher = patch.dict(os.environ, {
            "AXGT_AGENTLINK_ENABLED": "true",
            "AXGT_AGENTLINK_MODE": "verify_only",
            "AXGT_AGENTLINK_MAX_AGE_SECONDS": "300"
        })
        self.env_patcher.start()

        # Generate a test keypair
        self.priv_key = "0x" + "b" * 64
        self.account = Account.from_key(self.priv_key)
        self.agent_address = self.account.address
        self.owner_address = "0x" + "c" * 40
        self.uri = "https://app.axonos.io/api/x402/session"
        
    def tearDown(self):
        self.db_patcher.stop()
        self.env_patcher.stop()

    def _build_valid_payload(self, custom_fields=None):
        now_str = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        exp_str = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
        payload = {
            "domain": "app.axonos.io",
            "address": self.agent_address,
            "uri": self.uri,
            "version": "1",
            "chainId": "eip155:8453",
            "type": "eip191",
            "nonce": "testnonce12345",
            "issuedAt": now_str,
            "expirationTime": exp_str
        }
        if custom_fields:
            payload.update(custom_fields)
            
        # Reconstruct and sign
        siwe_message = agentlink_verifier.format_siwe_message(payload)
        signable = encode_defunct(text=siwe_message)
        signature = self.account.sign_message(signable).signature.hex()
        if not signature.startswith("0x"):
            signature = "0x" + signature
        payload["signature"] = signature
        return payload

    def test_invalid_header_size(self):
        huge_header = "A" * 8193
        res = agentlink_verifier.verify_agentlink_header(huge_header, self.uri)
        self.assertFalse(res["verified"])
        self.assertEqual(res["reason"], "invalid_header")

    def test_invalid_json_size(self):
        huge_payload = {"dummy": "x" * 4096}
        encoded = base64.b64encode(json.dumps(huge_payload).encode()).decode()
        res = agentlink_verifier.verify_agentlink_header(encoded, self.uri)
        self.assertFalse(res["verified"])
        self.assertEqual(res["reason"], "bad_json")

    def test_malformed_base64(self):
        res = agentlink_verifier.verify_agentlink_header("not_base64_!!!", self.uri)
        self.assertFalse(res["verified"])
        self.assertEqual(res["reason"], "bad_base64")

    def test_malformed_json(self):
        encoded = base64.b64encode(b"invalid_json{").decode()
        res = agentlink_verifier.verify_agentlink_header(encoded, self.uri)
        self.assertFalse(res["verified"])
        self.assertEqual(res["reason"], "bad_json")

    def test_missing_required_fields(self):
        payload = self._build_valid_payload()
        del payload["domain"]
        encoded = base64.b64encode(json.dumps(payload).encode()).decode()
        res = agentlink_verifier.verify_agentlink_header(encoded, self.uri)
        self.assertFalse(res["verified"])
        self.assertEqual(res["reason"], "missing_fields")

    def test_reject_erc1271(self):
        # Explicit type rejection
        payload = self._build_valid_payload({"type": "eip1271"})
        encoded = base64.b64encode(json.dumps(payload).encode()).decode()
        res = agentlink_verifier.verify_agentlink_header(encoded, self.uri)
        self.assertFalse(res["verified"])
        self.assertEqual(res["reason"], "eip1271_unsupported")

        # Explicit signatureScheme rejection
        payload = self._build_valid_payload({"signatureScheme": "eip1271"})
        encoded = base64.b64encode(json.dumps(payload).encode()).decode()
        res = agentlink_verifier.verify_agentlink_header(encoded, self.uri)
        self.assertFalse(res["verified"])
        self.assertEqual(res["reason"], "eip1271_unsupported")

    def test_domain_mismatch(self):
        payload = self._build_valid_payload({"domain": "wrongdomain.com"})
        encoded = base64.b64encode(json.dumps(payload).encode()).decode()
        res = agentlink_verifier.verify_agentlink_header(encoded, self.uri)
        self.assertFalse(res["verified"])
        self.assertEqual(res["reason"], "domain_mismatch")

    def test_uri_mismatch(self):
        payload = self._build_valid_payload({"uri": "https://wronguri.com/api"})
        encoded = base64.b64encode(json.dumps(payload).encode()).decode()
        res = agentlink_verifier.verify_agentlink_header(encoded, self.uri)
        self.assertFalse(res["verified"])
        self.assertEqual(res["reason"], "uri_mismatch")

    def test_future_issued_at(self):
        future_time = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat().replace("+00:00", "Z")
        payload = self._build_valid_payload({"issuedAt": future_time})
        encoded = base64.b64encode(json.dumps(payload).encode()).decode()
        res = agentlink_verifier.verify_agentlink_header(encoded, self.uri)
        self.assertFalse(res["verified"])
        self.assertEqual(res["reason"], "future_issued_at")

    def test_expired_issued_at(self):
        past_time = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat().replace("+00:00", "Z")
        payload = self._build_valid_payload({"issuedAt": past_time})
        encoded = base64.b64encode(json.dumps(payload).encode()).decode()
        res = agentlink_verifier.verify_agentlink_header(encoded, self.uri)
        self.assertFalse(res["verified"])
        self.assertEqual(res["reason"], "expired_issued_at")

    def test_expired_time(self):
        past_exp = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat().replace("+00:00", "Z")
        payload = self._build_valid_payload({"expirationTime": past_exp})
        encoded = base64.b64encode(json.dumps(payload).encode()).decode()
        res = agentlink_verifier.verify_agentlink_header(encoded, self.uri)
        self.assertFalse(res["verified"])
        self.assertEqual(res["reason"], "expired")

    def test_not_yet_valid(self):
        future_nb = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat().replace("+00:00", "Z")
        payload = self._build_valid_payload({"notBefore": future_nb})
        encoded = base64.b64encode(json.dumps(payload).encode()).decode()
        res = agentlink_verifier.verify_agentlink_header(encoded, self.uri)
        self.assertFalse(res["verified"])
        self.assertEqual(res["reason"], "not_yet_valid")

    @patch("agentlink_verifier.verify_agent_on_chain_registry")
    def test_happy_path_success(self, mock_registry):
        # Registry returns active agent and its owner
        mock_registry.return_value = (True, self.owner_address, "active")
        
        payload = self._build_valid_payload()
        encoded = base64.b64encode(json.dumps(payload).encode()).decode()
        
        res = agentlink_verifier.verify_agentlink_header(encoded, self.uri)
        self.assertTrue(res["verified"])
        self.assertEqual(res["agent"], self.agent_address)
        self.assertEqual(res["owner"], self.owner_address)
        self.assertEqual(res["chainId"], "eip155:8453")

    @patch("agentlink_verifier.verify_agent_on_chain_registry")
    def test_replay_protection_prevents_reuse(self, mock_registry):
        mock_registry.return_value = (True, self.owner_address, "active")
        
        payload = self._build_valid_payload()
        encoded = base64.b64encode(json.dumps(payload).encode()).decode()
        
        # First verification succeeds
        res1 = agentlink_verifier.verify_agentlink_header(encoded, self.uri)
        self.assertTrue(res1["verified"])
        
        # Second verification with same nonce fails
        res2 = agentlink_verifier.verify_agentlink_header(encoded, self.uri)
        self.assertFalse(res2["verified"])
        self.assertEqual(res2["reason"], "replayed_nonce")

    @patch("urllib.request.urlopen")
    def test_registry_rpc_active(self, mock_urlopen):
        # Hex response containing:
        # Word 0: Owner Address (0x0f1DD54cb5351D91ED6fb0D4208B6Eda894C4CA7)
        # Word 1: Generation Pointer (uint256)
        # Word 2: Active Boolean (true = 1)
        owner_hex = "0f1DD54cb5351D91ED6fb0D4208B6Eda894C4CA7".lower().zfill(64)
        gen_hex = "0".zfill(64)
        active_hex = "1".zfill(64)
        rpc_result_hex = "0x" + owner_hex + gen_hex + active_hex
        
        # Mock response object
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"result": rpc_result_hex}).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp
        
        verified, owner, reason = agentlink_verifier.verify_agent_on_chain_registry(
            self.agent_address, "eip155:8453"
        )
        self.assertTrue(verified)
        self.assertEqual(owner, "0x0f1dd54cb5351d91ed6fb0d4208b6eda894c4ca7")
        self.assertEqual(reason, "active")

    @patch("urllib.request.urlopen")
    def test_registry_rpc_inactive(self, mock_urlopen):
        # Word 2 is 0 (false)
        owner_hex = "0f1DD54cb5351D91ED6fb0D4208B6Eda894C4CA7".lower().zfill(64)
        gen_hex = "0".zfill(64)
        active_hex = "0".zfill(64)
        rpc_result_hex = "0x" + owner_hex + gen_hex + active_hex
        
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"result": rpc_result_hex}).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp
        
        verified, owner, reason = agentlink_verifier.verify_agent_on_chain_registry(
            self.agent_address, "eip155:8453"
        )
        self.assertFalse(verified)
        self.assertEqual(owner, "0x0f1dd54cb5351d91ed6fb0d4208b6eda894c4ca7")
        self.assertEqual(reason, "inactive")

    @patch("urllib.request.urlopen")
    def test_registry_rpc_not_linked(self, mock_urlopen):
        # Word 0 is zero address (not linked)
        owner_hex = "0".zfill(64)
        gen_hex = "0".zfill(64)
        active_hex = "0".zfill(64)
        rpc_result_hex = "0x" + owner_hex + gen_hex + active_hex
        
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"result": rpc_result_hex}).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp
        
        verified, owner, reason = agentlink_verifier.verify_agent_on_chain_registry(
            self.agent_address, "eip155:8453"
        )
        self.assertFalse(verified)
        self.assertIsNone(owner)
        self.assertEqual(reason, "not_linked")

    @patch("agentlink_verifier.verify_agent_on_chain_registry")
    def test_atomic_replay_race_returns_replayed_nonce(self, mock_registry):
        mock_registry.return_value = (True, self.owner_address, "active")
        
        payload = self._build_valid_payload({"nonce": "race_nonce"})
        encoded = base64.b64encode(json.dumps(payload).encode()).decode()
        
        # Inject the nonce into db_state beforehand to simulate a parallel thread inserting it first
        self.db_state["race_nonce"] = {
            "agent_address": self.agent_address,
            "chain_id": "eip155:8453",
            "resource_uri_hash": "somehash",
            "issued_at": time.time(),
            "expires_at": time.time() + 300
        }
        
        # Verification should fail immediately with replayed_nonce
        res = agentlink_verifier.verify_agentlink_header(encoded, self.uri)
        self.assertFalse(res["verified"])
        self.assertEqual(res["reason"], "replayed_nonce")

if __name__ == "__main__":
    unittest.main()
