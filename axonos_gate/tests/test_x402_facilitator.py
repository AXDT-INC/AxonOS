"""
Tests for x402_facilitator: the opt-in CDP facilitator settlement rail (Bazaar
listing). Covers the enable flag, the Bazaar discovery extension shape, and the
verify/settle response parsing (the HTTP boundary `_post` is mocked — no network,
no CDP keys needed).
"""

import os
import sys
import unittest
from unittest.mock import patch

_tests_dir = os.path.dirname(os.path.abspath(__file__))
_repo_root = os.path.dirname(os.path.dirname(_tests_dir))
_gate_dir = os.path.dirname(_tests_dir)
for _p in (_gate_dir, _repo_root):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import x402_facilitator as fac


class TestFacilitatorEnabled(unittest.TestCase):
    def test_default_off(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AXGT_X402_FACILITATOR_ENABLED", None)
            self.assertFalse(fac.facilitator_enabled())

    def test_on_when_truthy(self):
        for v in ("true", "1", "yes", "on", "TRUE"):
            with patch.dict(os.environ, {"AXGT_X402_FACILITATOR_ENABLED": v}):
                self.assertTrue(fac.facilitator_enabled(), v)

    def test_off_when_falsey(self):
        for v in ("false", "0", "no", "off", ""):
            with patch.dict(os.environ, {"AXGT_X402_FACILITATOR_ENABLED": v}):
                self.assertFalse(fac.facilitator_enabled(), v)


class TestDiscoveryExtension(unittest.TestCase):
    def test_none_when_disabled(self):
        with patch.dict(os.environ, {"AXGT_X402_FACILITATOR_ENABLED": "false"}):
            os.environ.pop("AXGT_X402_BAZAAR_DISCOVERABLE", None)
            self.assertIsNone(fac.bazaar_discovery_extension())

    def test_default_shape_when_enabled(self):
        with patch.dict(os.environ, {"AXGT_X402_FACILITATOR_ENABLED": "true"}):
            for k in ("AXGT_X402_BAZAAR_DISCOVERABLE", "AXGT_X402_BAZAAR_CATEGORY", "AXGT_X402_BAZAAR_TAGS"):
                os.environ.pop(k, None)
            ext = fac.bazaar_discovery_extension()
        self.assertEqual(ext, {"bazaar": {
            "discoverable": True,
            "category": "compute",
            "tags": ["gpu", "compute", "ssh", "linux"],
        }})

    def test_custom_category_and_tags(self):
        with patch.dict(os.environ, {
            "AXGT_X402_FACILITATOR_ENABLED": "true",
            "AXGT_X402_BAZAAR_CATEGORY": "ai-compute",
            "AXGT_X402_BAZAAR_TAGS": "gpu, h100 , cuda",
        }):
            ext = fac.bazaar_discovery_extension()
        self.assertEqual(ext["bazaar"]["category"], "ai-compute")
        self.assertEqual(ext["bazaar"]["tags"], ["gpu", "h100", "cuda"])

    def test_discoverable_can_be_forced_off_with_facilitator_on(self):
        with patch.dict(os.environ, {
            "AXGT_X402_FACILITATOR_ENABLED": "true",
            "AXGT_X402_BAZAAR_DISCOVERABLE": "false",
        }):
            self.assertIsNone(fac.bazaar_discovery_extension())


class TestVerifySettleParsing(unittest.TestCase):
    _PAYLOAD = {"x402Version": 1, "scheme": "exact", "network": "base", "payload": {}}
    _REQS = {"scheme": "exact", "network": "base", "asset": "0xusdc", "payTo": "0xrev"}

    def test_verify_valid(self):
        with patch.object(fac, "_post", return_value=({"isValid": True, "payer": "0xabc"}, None)):
            ok, reason = fac.facilitator_verify(self._PAYLOAD, self._REQS)
        self.assertTrue(ok)
        self.assertIsNone(reason)

    def test_verify_invalid_surfaces_reason(self):
        with patch.object(fac, "_post", return_value=({"isValid": False, "invalidReason": "insufficient_funds"}, None)):
            ok, reason = fac.facilitator_verify(self._PAYLOAD, self._REQS)
        self.assertFalse(ok)
        self.assertEqual(reason, "insufficient_funds")

    def test_verify_http_error(self):
        with patch.object(fac, "_post", return_value=(None, "Facilitator HTTP 401: bad jwt")):
            ok, reason = fac.facilitator_verify(self._PAYLOAD, self._REQS)
        self.assertFalse(ok)
        self.assertIn("401", reason)

    def test_settle_success_returns_tx(self):
        with patch.object(fac, "_post", return_value=({"success": True, "transaction": "0x" + "ab" * 32}, None)):
            tx, err = fac.facilitator_settle(self._PAYLOAD, self._REQS)
        self.assertEqual(tx, "0x" + "ab" * 32)
        self.assertIsNone(err)

    def test_settle_failure_surfaces_reason(self):
        with patch.object(fac, "_post", return_value=({"success": False, "errorReason": "expired"}, None)):
            tx, err = fac.facilitator_settle(self._PAYLOAD, self._REQS)
        self.assertIsNone(tx)
        self.assertEqual(err, "expired")

    def test_settle_success_but_no_tx_hash(self):
        with patch.object(fac, "_post", return_value=({"success": True}, None)):
            tx, err = fac.facilitator_settle(self._PAYLOAD, self._REQS)
        self.assertIsNone(tx)
        self.assertIn("no transaction hash", err)


class TestJwtMissingKeys(unittest.TestCase):
    def test_no_keys_returns_none(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CDP_API_KEY_ID", None)
            os.environ.pop("CDP_API_KEY_SECRET", None)
            self.assertIsNone(fac._generate_cdp_jwt("POST", "/platform/v2/x402/settle"))


if __name__ == "__main__":
    unittest.main()
