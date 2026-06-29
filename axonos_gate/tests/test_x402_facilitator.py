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
        self.assertIsNotNone(ext)
        self.assertEqual(ext["bazaar"]["discoverable"], True)
        self.assertEqual(ext["bazaar"]["category"], "compute")
        self.assertEqual(ext["bazaar"]["tags"], ["gpu", "compute", "ssh", "linux"])
        self.assertIn("info", ext["bazaar"])
        self.assertIn("schema", ext["bazaar"])

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

    def test_v2_bazaar_schema_fields(self):
        with patch.dict(os.environ, {"AXGT_X402_FACILITATOR_ENABLED": "true"}):
            ext = fac.bazaar_discovery_extension()
        self.assertIsNotNone(ext)
        bazaar = ext.get("bazaar", {})
        self.assertEqual(bazaar.get("info", {}).get("input", {}).get("method"), "POST")
        self.assertEqual(bazaar.get("info", {}).get("input", {}).get("bodyType"), "json")
        self.assertEqual(bazaar.get("schema", {}).get("$schema"), "https://json-schema.org/draft/2020-12/schema")


class TestVerifySettleParsing(unittest.TestCase):
    _PAYLOAD = {"x402Version": 1, "scheme": "exact", "network": "base", "payload": {}}
    _REQS = {"scheme": "exact", "network": "base", "asset": "0xusdc", "payTo": "0xrev"}

    def test_verify_valid(self):
        with patch.object(fac, "_post", return_value=({"isValid": True, "payer": "0xabc"}, None, {"X-PAYMENT-RESPONSE": "test"})):
            ok, reason, ext, hdrs = fac.facilitator_verify(self._PAYLOAD, self._REQS)
        self.assertTrue(ok)
        self.assertIsNone(reason)
        self.assertIsNone(ext)
        self.assertEqual(hdrs, {"X-PAYMENT-RESPONSE": "test"})

    def test_verify_invalid_surfaces_reason(self):
        with patch.object(fac, "_post", return_value=({"isValid": False, "invalidReason": "insufficient_funds"}, None, None)):
            ok, reason, ext, hdrs = fac.facilitator_verify(self._PAYLOAD, self._REQS)
        self.assertFalse(ok)
        self.assertEqual(reason, "insufficient_funds")
        self.assertIsNone(ext)
        self.assertIsNone(hdrs)

    def test_verify_http_error(self):
        with patch.object(fac, "_post", return_value=(None, "Facilitator HTTP 401: bad jwt", None)):
            ok, reason, ext, hdrs = fac.facilitator_verify(self._PAYLOAD, self._REQS)
        self.assertFalse(ok)
        self.assertIn("401", reason)
        self.assertIsNone(ext)
        self.assertIsNone(hdrs)

    def test_verify_with_extension_responses(self):
        ext_data = {"bazaar": {"status": "success"}}
        with patch.object(fac, "_post", return_value=({"isValid": True, "extensionResponses": ext_data}, None, None)):
            ok, reason, ext, hdrs = fac.facilitator_verify(self._PAYLOAD, self._REQS)
        self.assertTrue(ok)
        self.assertEqual(ext, ext_data)
        self.assertIsNone(hdrs)

    def test_settle_success_returns_tx(self):
        with patch.object(fac, "_post", return_value=({"success": True, "transaction": "0x" + "ab" * 32}, None, {"PAYMENT-RESPONSE": "test"})):
            tx, err, ext, hdrs = fac.facilitator_settle(self._PAYLOAD, self._REQS)
        self.assertEqual(tx, "0x" + "ab" * 32)
        self.assertIsNone(err)
        self.assertIsNone(ext)
        self.assertEqual(hdrs, {"PAYMENT-RESPONSE": "test"})

    def test_settle_failure_surfaces_reason(self):
        with patch.object(fac, "_post", return_value=({"success": False, "errorReason": "expired"}, None, None)):
            tx, err, ext, hdrs = fac.facilitator_settle(self._PAYLOAD, self._REQS)
        self.assertIsNone(tx)
        self.assertEqual(err, "expired")
        self.assertIsNone(ext)
        self.assertIsNone(hdrs)

    def test_settle_success_but_no_tx_hash(self):
        with patch.object(fac, "_post", return_value=({"success": True}, None, None)):
            tx, err, ext, hdrs = fac.facilitator_settle(self._PAYLOAD, self._REQS)
        self.assertIsNone(tx)
        self.assertIn("no transaction hash", err)
        self.assertIsNone(ext)
        self.assertIsNone(hdrs)

    def test_settle_with_extension_responses(self):
        ext_data = {"bazaar": {"status": "processing"}}
        with patch.object(fac, "_post", return_value=({"success": True, "transaction": "0x" + "ab" * 32, "extensionResponses": ext_data}, None, None)):
            tx, err, ext, hdrs = fac.facilitator_settle(self._PAYLOAD, self._REQS)
        self.assertEqual(tx, "0x" + "ab" * 32)
        self.assertEqual(ext, ext_data)
        self.assertIsNone(hdrs)


class TestJwtMissingKeys(unittest.TestCase):
    def test_no_keys_returns_none(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CDP_API_KEY_ID", None)
            os.environ.pop("CDP_API_KEY_SECRET", None)
            self.assertIsNone(fac._generate_cdp_jwt("POST", "/platform/v2/x402/settle"))


class TestFacilitatorEnhancements(unittest.TestCase):
    def test_json_extension_casing(self):
        d1 = {"extensionResponses": {"bazaar": {"status": "success"}}}
        self.assertEqual(fac._get_extension_from_json(d1), {"bazaar": {"status": "success"}})
        
        d2 = {"extension_responses": {"bazaar": {"status": "processing"}}}
        self.assertEqual(fac._get_extension_from_json(d2), {"bazaar": {"status": "processing"}})
        
        d3 = {"extension-responses": {"bazaar": {"status": "rejected"}}}
        self.assertEqual(fac._get_extension_from_json(d3), {"bazaar": {"status": "rejected"}})
        
        self.assertIsNone(fac._get_extension_from_json(None))
        self.assertIsNone(fac._get_extension_from_json("string"))

    def test_header_casing_capture(self):
        class FakeResponse:
            def __init__(self, headers, json_data, status_code=200):
                from requests.structures import CaseInsensitiveDict
                self.headers = CaseInsensitiveDict(headers)
                self.json_data = json_data
                self.status_code = status_code
                self.text = "raw response"
            def json(self):
                return self.json_data

        fake_headers = {
            "x-extension-responses": "eyJiYXphYXIiOnsic3RhdHVzIjoic3VjY2VzcyJ9fQ==",
            "x-payment-response": "dGVzdF94X3BheW1lbnRfcmVzcG9uc2U="
        }
        
        with patch("requests.post", return_value=FakeResponse(fake_headers, {"isValid": True})):
            with patch.object(fac, "_generate_cdp_jwt", return_value="fake_jwt"):
                data, err, hdrs = fac._post("verify", {})
                
        self.assertIsNotNone(hdrs)
        self.assertEqual(hdrs.get("X-EXTENSION-RESPONSES"), "eyJiYXphYXIiOnsic3RhdHVzIjoic3VjY2VzcyJ9fQ==")
        self.assertEqual(hdrs.get("X-PAYMENT-RESPONSE"), "dGVzdF94X3BheW1lbnRfcmVzcG9uc2U=")

    def test_debug_logging_safe(self):
        class FakeResponse:
            def __init__(self):
                from requests.structures import CaseInsensitiveDict
                self.headers = CaseInsensitiveDict({"payment-response": "foo"})
                self.status_code = 200
                self.text = "raw text"
            def json(self):
                return {"success": True, "transaction": "0xsettlementtx"}

        fake_body = {
            "x402Version": 2,
            "paymentPayload": {
                "resource": {
                    "url": "https://app.axonos.io/api/x402/session",
                    "description": "GPU session",
                    "mimeType": "application/json"
                },
                "accepted": {
                    "network": "eip155:8453",
                    "payTo": "0xrevenue",
                    "extensions": {
                        "bazaar": {
                            "info": {},
                            "schema": {}
                        }
                    }
                },
                "payload": {
                    "authorization": {
                        "from": "0xpayer",
                        "to": "0xrevenue",
                        "value": "1000000"
                    },
                    "signature": "0xsigningproof"
                }
            }
        }
        
        import logging
        old_level = fac.logger.level
        fac.logger.setLevel(logging.INFO)
        try:
            with patch.dict(os.environ, {"AXGT_X402_DEBUG": "true"}):
                with patch("requests.post", return_value=FakeResponse()):
                    with patch.object(fac, "_generate_cdp_jwt", return_value="fake_jwt"):
                        with self.assertLogs(fac.logger, level="INFO") as cm:
                            data, err, hdrs = fac._post("settle", fake_body)
                            self.assertEqual(data.get("transaction"), "0xsettlementtx")
        finally:
            fac.logger.setLevel(old_level)

        # Combine all log outputs into one text string
        all_logs = "\n".join(cm.output)

        # Assert none of the sensitive substrings exist in any log message (case-insensitive)
        sensitive_strings = [
            "0xsigningproof",
            "authorization",
            "private",
            "bearer",
            "jwt_token",
            "X-PAYMENT",
            "auth_token"
        ]
        for s in sensitive_strings:
            self.assertNotIn(s.lower(), all_logs.lower())


if __name__ == "__main__":
    unittest.main()
