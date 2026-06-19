"""
Tests for x402_verifier: EIP-3009 (TransferWithAuthorization) signature recovery
and the settle_x402_payment validation gates, using a real signed fixture.

A fixed private key signs a real EIP-712 TransferWithAuthorization message; the
verifier must recover exactly that signer. On-chain settlement and the tx-hash
verifier are mocked so no RPC is needed.
"""

import base64
import json
import os
import sys
import time
import unittest
from unittest.mock import patch

_tests_dir = os.path.dirname(os.path.abspath(__file__))
_repo_root = os.path.dirname(os.path.dirname(_tests_dir))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

try:
    from eth_account import Account
    from eth_account.messages import encode_typed_data
    _HAVE_ETH = True
except ImportError:
    _HAVE_ETH = False

# Deterministic test key (well-known Hardhat account #0 — never use in prod).
_PRIVKEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
_SIGNER = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"  # address of _PRIVKEY

_REVENUE = "0x1111111111111111111111111111111111111111"
_USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"  # Base USDC
_CHAIN_ID = 8453


def _env():
    return {
        "AXGT_REVENUE_WALLET": _REVENUE,
        "USDC_CONTRACT_ADDRESS": _USDC,
        "USDC_RPC_URL": "https://base.example.com",
        "USDC_CHAIN_ID": str(_CHAIN_ID),
        "USDC_NETWORK": "base",
        "USDC_MIN_DEPOSIT": "1",
        "X402_SETTLEMENT_PRIVATE_KEY": _PRIVKEY,
    }


def _sign_authorization(value_units, *, to=_REVENUE, frm=_SIGNER, key=_PRIVKEY,
                        valid_after=0, valid_before=None):
    if valid_before is None:
        valid_before = int(time.time()) + 600
    nonce = "0x" + ("11" * 32)
    domain = {
        "name": "USD Coin",
        "version": "2",
        "chainId": _CHAIN_ID,
        "verifyingContract": _USDC,
    }
    types = {
        "TransferWithAuthorization": [
            {"name": "from", "type": "address"},
            {"name": "to", "type": "address"},
            {"name": "value", "type": "uint256"},
            {"name": "validAfter", "type": "uint256"},
            {"name": "validBefore", "type": "uint256"},
            {"name": "nonce", "type": "bytes32"},
        ],
    }
    message = {
        "from": frm, "to": to, "value": int(value_units),
        "validAfter": valid_after, "validBefore": valid_before, "nonce": nonce,
    }
    signable = encode_typed_data(domain_data=domain, message_types=types, message_data=message)
    signed = Account.sign_message(signable, private_key=key)
    sig = signed.signature.hex()
    if not sig.startswith("0x"):
        sig = "0x" + sig
    authorization = {
        "from": frm, "to": to, "value": str(int(value_units)),
        "validAfter": str(valid_after), "validBefore": str(valid_before), "nonce": nonce,
    }
    return authorization, sig


def _x_payment_header(authorization, signature):
    payload = {
        "x402Version": 1,
        "scheme": "exact",
        "network": "base",
        "payload": {"authorization": authorization, "signature": signature},
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


@unittest.skipUnless(_HAVE_ETH, "eth_account not installed")
class TestEip3009Recovery(unittest.TestCase):
    def setUp(self):
        self.patcher = patch.dict(os.environ, _env())
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def test_recovers_known_signer(self):
        import x402_verifier as x
        authorization, sig = _sign_authorization(1_000_000)
        recovered = x._recover_eip3009_signer(authorization, sig, _USDC)
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered.lower(), _SIGNER.lower())

    def test_tampered_value_breaks_recovery(self):
        import x402_verifier as x
        authorization, sig = _sign_authorization(1_000_000)
        authorization["value"] = "2000000"  # tamper after signing
        recovered = x._recover_eip3009_signer(authorization, sig, _USDC)
        self.assertNotEqual((recovered or "").lower(), _SIGNER.lower())


@unittest.skipUnless(_HAVE_ETH, "eth_account not installed")
class TestSettleX402Gates(unittest.TestCase):
    def setUp(self):
        self.patcher = patch.dict(os.environ, _env())
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def _settle(self, wallet, header):
        import x402_verifier as x
        return x.settle_x402_payment(authenticated_wallet=wallet, x_payment_header=header)

    def test_happy_path_settles_and_credits(self):
        import x402_verifier as x
        authorization, sig = _sign_authorization(1_000_000)
        header = _x_payment_header(authorization, sig)
        with patch.object(x, "_submit_transfer_with_authorization", return_value=("0x" + "ab" * 32, None)) as submit, \
             patch.object(x, "verify_usdc_deposit", return_value={"verified": True, "credited_minutes": 60.0, "remaining_minutes": 60.0}) as verify:
            result = self._settle(_SIGNER, header)
        submit.assert_called_once()
        verify.assert_called_once()
        self.assertTrue(result["verified"])
        self.assertTrue(result["x402"])
        self.assertEqual(result["settlement_tx_hash"], "0x" + "ab" * 32)

    def test_rejects_signer_wallet_mismatch(self):
        # Authenticated wallet differs from the authorization.from / signer.
        authorization, sig = _sign_authorization(1_000_000)
        header = _x_payment_header(authorization, sig)
        result = self._settle("0x2222222222222222222222222222222222222222", header)
        self.assertFalse(result["verified"])
        self.assertIn("does not match authenticated wallet", result["error"])

    def test_rejects_wrong_recipient(self):
        authorization, sig = _sign_authorization(
            1_000_000, to="0x9999999999999999999999999999999999999999"
        )
        header = _x_payment_header(authorization, sig)
        result = self._settle(_SIGNER, header)
        self.assertFalse(result["verified"])
        self.assertIn("recipient is not the revenue wallet", result["error"])

    def test_rejects_expired_authorization(self):
        authorization, sig = _sign_authorization(
            1_000_000, valid_before=int(time.time()) - 10
        )
        header = _x_payment_header(authorization, sig)
        result = self._settle(_SIGNER, header)
        self.assertFalse(result["verified"])
        self.assertIn("expired", result["error"])

    def test_rejects_below_minimum(self):
        authorization, sig = _sign_authorization(500_000)  # 0.5 USDC < 1 USDC min
        header = _x_payment_header(authorization, sig)
        result = self._settle(_SIGNER, header)
        self.assertFalse(result["verified"])
        self.assertIn("below minimum", result["error"])

    def test_rejects_forged_signature(self):
        # Valid fields, but signature from a different key → recovery mismatch.
        authorization, _ = _sign_authorization(1_000_000)
        _, other_sig = _sign_authorization(
            1_000_000,
            key="0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d",
        )
        header = _x_payment_header(authorization, other_sig)
        result = self._settle(_SIGNER, header)
        self.assertFalse(result["verified"])
        self.assertIn("signature does not match", result["error"])


class TestAbiStringDecode(unittest.TestCase):
    """_decode_abi_string parses ABI-encoded string returns (name()/version())."""

    def _encode(self, s):
        b = s.encode("utf-8")
        offset = (32).to_bytes(32, "big")
        length = len(b).to_bytes(32, "big")
        body = b + b"\x00" * ((32 - len(b) % 32) % 32)
        return "0x" + (offset + length + body).hex()

    def test_decodes_usd_coin(self):
        import x402_verifier as x
        self.assertEqual(x._decode_abi_string(self._encode("USD Coin")), "USD Coin")

    def test_decodes_testnet_usdc(self):
        import x402_verifier as x
        self.assertEqual(x._decode_abi_string(self._encode("USDC")), "USDC")

    def test_empty_and_garbage(self):
        import x402_verifier as x
        self.assertIsNone(x._decode_abi_string("0x"))
        self.assertIsNone(x._decode_abi_string(None))
        self.assertIsNone(x._decode_abi_string("0xdeadbeef"))

    def test_probe_uses_name_and_version_selectors(self):
        import x402_verifier as x
        calls = []

        def fake_rpc(url, method, params):
            calls.append(params[0]["data"])
            sel = params[0]["data"]
            if sel == "0x06fdde03":  # name()
                return self._encode("USDC")
            if sel == "0x54fd4d50":  # version()
                return self._encode("2")
            return None

        with patch.object(x._dv, "_rpc", side_effect=fake_rpc):
            out = x.probe_usdc_eip712_domain("https://rpc", _USDC)
        self.assertEqual(out, {"name": "USDC", "version": "2"})
        self.assertIn("0x06fdde03", calls)
        self.assertIn("0x54fd4d50", calls)


if __name__ == "__main__":
    unittest.main()


class TestDepositRouterAuto(unittest.TestCase):
    """verify_deposit_auto precedence: verified > already-credited > pending > fail."""

    def _run(self, eth_res, usdc_res, eth_pending=False, usdc_pending=False):
        import deposit_router as dr
        return dr.verify_deposit_auto(
            authenticated_wallet="0x" + "a" * 40,
            tx_hash="0x" + "b" * 64,
            verify_eth=lambda **k: eth_res,
            eth_is_pending=lambda r: bool(r.get("pending")),
            verify_usdc=lambda **k: usdc_res,
            usdc_is_pending=lambda r: bool(r.get("pending")),
        )

    def test_verified_on_usdc_wins(self):
        res, pending = self._run(
            {"verified": False, "error": "No valid AXGT transfer to revenue wallet"},
            {"verified": True, "deposit_currency": "USDC", "credited_minutes": 60},
        )
        self.assertTrue(res["verified"]); self.assertFalse(pending)
        self.assertEqual(res["deposit_currency"], "USDC")

    def test_verified_on_eth_wins(self):
        res, pending = self._run(
            {"verified": True, "deposit_currency": "ETH", "credited_minutes": 60},
            {"verified": False, "error": "No valid USDC transfer to revenue wallet"},
        )
        self.assertTrue(res["verified"]); self.assertFalse(pending)

    def test_already_credited_beats_plain_fail(self):
        res, pending = self._run(
            {"verified": False, "error": "Transaction already credited"},
            {"verified": False, "error": "No valid USDC transfer to revenue wallet"},
        )
        self.assertFalse(pending)
        self.assertIn("already credited", res["error"].lower())

    def test_pending_when_a_rail_still_confirming(self):
        # USDC tx pasted while ETH rail says pending (not yet indexed) → pending,
        # not a wrong-rail hard fail.
        res, pending = self._run(
            {"verified": False, "pending": True, "error": "Insufficient confirmations"},
            {"verified": False, "error": "No valid USDC transfer to revenue wallet"},
            eth_pending=True,
        )
        self.assertTrue(pending)

    def test_hard_fail_prefers_specific_error(self):
        res, pending = self._run(
            {"verified": False, "error": "Transaction failed"},
            {"verified": False, "error": "No USDC transfer from your wallet to the revenue wallet found"},
        )
        self.assertFalse(pending)
        self.assertIn("revenue wallet", res["error"].lower())

    def test_no_rails_available(self):
        import deposit_router as dr
        res, pending = dr.verify_deposit_auto(
            authenticated_wallet="0x" + "a" * 40, tx_hash="0x" + "b" * 64,
            verify_eth=None, eth_is_pending=None, verify_usdc=None, usdc_is_pending=None,
        )
        self.assertFalse(res["verified"]); self.assertFalse(pending)
