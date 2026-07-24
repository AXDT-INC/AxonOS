"""Security-focused tests for session-scoped WebRTC agent capabilities."""

from __future__ import annotations

import os
import time
import unittest
from unittest import mock

import jwt

from axonos_gate.webrtc import capability


class WebrtcCapabilityTests(unittest.TestCase):
    wallet = "0x1234567890123456789012345678901234567890"
    signing_secret = "central-test-signing-secret-with-sufficient-entropy"
    files_key = "high-entropy-per-session-files-key"

    def setUp(self) -> None:
        self.env = mock.patch.dict(
            os.environ,
            {"WEBRTC_AGENT_INTERNAL_KEY": self.signing_secret},
            clear=True,
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def _issue(self, session_id=73, wallet=None, files_key=None):
        issued = capability.issue(
            session_id,
            wallet or self.wallet,
            files_key or self.files_key,
        )
        self.assertIsNotNone(issued)
        return issued

    def test_issue_and_verify_returns_only_bound_revocation_metadata(self) -> None:
        issued = self._issue(session_id="073", wallet=self.wallet.upper())

        verified = capability.verify(
            f"  {issued['token']}  ",
            "73",
            self.wallet.upper(),
        )

        self.assertEqual(
            verified,
            {
                "id": 73,
                "wallet_address": self.wallet,
                "files_key_fingerprint": issued["files_key_fingerprint"],
                "jti_hash": issued["jti_hash"],
                "expires_at": issued["expires_at"],
            },
        )
        self.assertEqual(
            issued["files_key_fingerprint"],
            capability.files_key_fingerprint(self.files_key),
        )
        self.assertNotIn("files_key", verified)
        self.assertNotIn("jti", verified)

    def test_capability_is_bound_to_exact_compute_session_and_wallet(self) -> None:
        issued = self._issue()

        self.assertIsNone(capability.verify(issued["token"], 74, self.wallet))
        self.assertIsNone(
            capability.verify(
                issued["token"],
                73,
                "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            )
        )

    def test_tampered_malformed_and_oversized_tokens_are_rejected(self) -> None:
        token = self._issue()["token"]
        header, payload, signature = token.split(".")
        changed_signature = ("A" if signature[0] != "A" else "B") + signature[1:]
        tampered = ".".join((header, payload, changed_signature))

        for candidate in (
            tampered,
            "not-a-jwt",
            "x" * 4097,
            "",
        ):
            with self.subTest(token=candidate[:24]):
                self.assertIsNone(capability.verify(candidate, 73, self.wallet))

    def test_token_signed_with_another_central_secret_is_rejected(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"WEBRTC_AGENT_INTERNAL_KEY": "a-different-central-signing-secret"},
            clear=True,
        ):
            foreign = self._issue()

        self.assertIsNone(capability.verify(foreign["token"], 73, self.wallet))

    def test_expired_token_is_rejected(self) -> None:
        real_now = time.time()
        with mock.patch.object(capability.time, "time", return_value=real_now - 172_800):
            expired = self._issue()

        self.assertLess(expired["expires_at"], real_now)
        self.assertIsNone(capability.verify(expired["token"], 73, self.wallet))

    def test_renew_preserves_revocation_identity_and_advances_expiry(self) -> None:
        real_now = time.time()
        with mock.patch.dict(
            os.environ,
            {
                "WEBRTC_AGENT_INTERNAL_KEY": self.signing_secret,
                "WEBRTC_AGENT_CAPABILITY_TTL_SECONDS": "600",
            },
            clear=True,
        ), mock.patch.object(capability.time, "time", return_value=real_now - 100):
            issued = self._issue()

        with mock.patch.dict(
            os.environ,
            {
                "WEBRTC_AGENT_INTERNAL_KEY": self.signing_secret,
                "WEBRTC_AGENT_CAPABILITY_TTL_SECONDS": "600",
            },
            clear=True,
        ), mock.patch.object(capability.time, "time", return_value=real_now):
            renewed = capability.renew(issued["token"], 73, self.wallet)

        self.assertIsNotNone(renewed)
        assert renewed is not None
        self.assertEqual(renewed["jti_hash"], issued["jti_hash"])
        self.assertEqual(
            renewed["files_key_fingerprint"],
            issued["files_key_fingerprint"],
        )
        self.assertGreater(renewed["expires_at"], issued["expires_at"])
        self.assertIsNotNone(capability.verify(issued["token"], 73, self.wallet))
        self.assertIsNotNone(capability.verify(renewed["token"], 73, self.wallet))

    def test_header_type_is_pinned_even_with_a_valid_signature(self) -> None:
        issued = self._issue()
        claims = jwt.decode(issued["token"], options={"verify_signature": False})
        private_key, _public_key, kid = capability._keypair()
        wrong_type = jwt.encode(
            claims,
            private_key,
            algorithm="EdDSA",
            headers={"kid": kid, "typ": "JWT"},
        )

        self.assertIsNone(capability.verify(wrong_type, 73, self.wallet))

    def test_issue_fails_closed_for_invalid_inputs_or_missing_signing_key(self) -> None:
        invalid = (
            (0, self.wallet, self.files_key),
            ("not-an-id", self.wallet, self.files_key),
            (73, "not-a-wallet", self.files_key),
            (73, self.wallet, ""),
        )
        for session_id, wallet, files_key in invalid:
            with self.subTest(session_id=session_id, wallet=wallet, files_key=files_key):
                self.assertIsNone(capability.issue(session_id, wallet, files_key))

        for signing_secret in ("", "line-one\nline-two"):
            with self.subTest(signing_secret=signing_secret), mock.patch.dict(
                os.environ,
                {"WEBRTC_AGENT_INTERNAL_KEY": signing_secret},
                clear=True,
            ):
                self.assertIsNone(capability.issue(73, self.wallet, self.files_key))


if __name__ == "__main__":
    unittest.main()
