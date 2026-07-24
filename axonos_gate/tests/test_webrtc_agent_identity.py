"""Focused tests for fail-closed per-session WebRTC agent identity."""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from unittest import mock


_tests_dir = os.path.dirname(os.path.abspath(__file__))
_axonos_gate_root = os.path.dirname(_tests_dir)
if _axonos_gate_root not in sys.path:
    sys.path.insert(0, _axonos_gate_root)


_WALLET = "0x" + ("ab" * 20)
_IDENTITY_ENV = {
    "WEBRTC_ENABLED": "true",
    "WEBRTC_AGENT_INTERNAL_KEY": "fleet-agent-key",
    "AXGT_SESSION_ID": "248",
    "AXGT_WALLET_ADDRESS": _WALLET,
    "AXGT_SESSION_FILES_KEY": "per-session-files-key",
}


class _StopLoop(RuntimeError):
    pass


class WebrtcAgentIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        import webrtc_agent_main as agent

        self.agent = agent

    def test_agent_headers_include_complete_canonical_identity(self) -> None:
        env = {**_IDENTITY_ENV, "AXGT_SESSION_ID": "00248", "AXGT_WALLET_ADDRESS": _WALLET.upper()}
        with mock.patch.dict(os.environ, env, clear=True):
            headers = self.agent._agent_headers(json_content=True)

        self.assertEqual(
            headers,
            {
                "X-AxonOS-WebRTC-Agent-Key": "fleet-agent-key",
                "X-AXGT-Session-ID": "248",
                "X-Wallet-Address": _WALLET,
                "X-AXGT-Session-Key": "per-session-files-key",
                "Content-Type": "application/json",
            },
        )

    def test_documented_legacy_single_container_uses_key_only_identity(self) -> None:
        env = {
            "WEBRTC_AGENT_INTERNAL_KEY": "fleet-agent-key",
            "AXGT_MULTI_SESSION_ENABLED": "false",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            headers = self.agent._agent_headers(json_content=True)
            self.assertEqual(
                headers,
                {
                    "X-AxonOS-WebRTC-Agent-Key": "fleet-agent-key",
                    "Content-Type": "application/json",
                },
            )
            self.assertTrue(
                self.agent._job_matches_agent_identity(
                    {
                        "compute_session_id": 73,
                        "wallet_address": _WALLET,
                    },
                    {"X-AxonOS-WebRTC-Agent-Key": "fleet-agent-key"},
                )
            )

        with mock.patch.dict(
            os.environ,
            {"WEBRTC_AGENT_INTERNAL_KEY": "fleet-agent-key"},
            clear=True,
        ):
            with self.assertRaises(ValueError):
                self.agent._agent_headers()

    def test_agent_headers_reject_missing_or_malformed_identity(self) -> None:
        invalid_overrides = {
            "WEBRTC_AGENT_INTERNAL_KEY": "",
            "AXGT_SESSION_ID": "",
            "AXGT_WALLET_ADDRESS": "",
            "AXGT_SESSION_FILES_KEY": "",
        }
        for name, value in invalid_overrides.items():
            with self.subTest(missing=name):
                with mock.patch.dict(os.environ, {**_IDENTITY_ENV, name: value}, clear=True):
                    with self.assertRaises(ValueError):
                        self.agent._agent_headers()

        for session_id in ("0", "-1", "not-an-id"):
            with self.subTest(session_id=session_id):
                with mock.patch.dict(
                    os.environ,
                    {**_IDENTITY_ENV, "AXGT_SESSION_ID": session_id},
                    clear=True,
                ):
                    with self.assertRaisesRegex(ValueError, "AXGT_SESSION_ID"):
                        self.agent._agent_headers()

        with mock.patch.dict(
            os.environ,
            {**_IDENTITY_ENV, "AXGT_WALLET_ADDRESS": "not-a-wallet"},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "AXGT_WALLET_ADDRESS"):
                self.agent._agent_headers()

    def test_job_identity_requires_compute_session_and_wallet_match(self) -> None:
        with mock.patch.dict(os.environ, _IDENTITY_ENV, clear=True):
            headers = self.agent._agent_headers()

        good = {
            "session_id": "signal-id",
            "compute_session_id": 248,
            "wallet_address": _WALLET.upper(),
        }
        self.assertTrue(self.agent._job_matches_agent_identity(good, headers))

        for changed in (
            {**good, "compute_session_id": 249},
            {**good, "compute_session_id": None},
            {**good, "wallet_address": "0x" + ("cd" * 20)},
            {**good, "wallet_address": None},
        ):
            with self.subTest(job=changed):
                self.assertFalse(self.agent._job_matches_agent_identity(changed, headers))

    def test_missing_identity_stops_before_next_poll(self) -> None:
        async def stop_sleep(_delay: float) -> None:
            raise _StopLoop

        env = {**_IDENTITY_ENV, "AXGT_SESSION_FILES_KEY": ""}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(self.agent.asyncio, "sleep", side_effect=stop_sleep), \
             mock.patch.object(self.agent, "_http_get_job") as get_job:
            with self.assertRaises(_StopLoop):
                asyncio.run(self.agent.main_loop())

        get_job.assert_not_called()

    def test_mismatched_next_job_is_not_processed(self) -> None:
        real_sleep = asyncio.sleep
        seen_headers: list[dict[str, str]] = []

        async def controlled_sleep(delay: float) -> None:
            if delay == 0:
                await real_sleep(0)
                return
            raise _StopLoop

        def return_wrong_job(_url: str, headers: dict[str, str]):
            seen_headers.append(dict(headers))
            return 200, {
                "session_id": "signal-for-another-container",
                "compute_session_id": 249,
                "wallet_address": _WALLET,
                "offer_sdp": "v=0\r\n",
                "offer_type": "offer",
            }

        with mock.patch.dict(os.environ, _IDENTITY_ENV, clear=True), \
             mock.patch.object(self.agent, "_prewarm_session_capabilities", new=mock.AsyncMock()), \
             mock.patch.object(self.agent, "_http_get_job", side_effect=return_wrong_job), \
             mock.patch.object(self.agent, "_run_session", new=mock.AsyncMock()) as run_session, \
             mock.patch.object(self.agent.asyncio, "sleep", side_effect=controlled_sleep):
            with self.assertRaises(_StopLoop):
                asyncio.run(self.agent.main_loop())

        self.assertEqual(len(seen_headers), 1)
        self.assertEqual(seen_headers[0]["X-AXGT-Session-ID"], "248")
        self.assertEqual(seen_headers[0]["X-Wallet-Address"], _WALLET)
        self.assertEqual(seen_headers[0]["X-AXGT-Session-Key"], "per-session-files-key")
        self.assertEqual(seen_headers[0]["X-AxonOS-WebRTC-Agent-Key"], "fleet-agent-key")
        run_session.assert_not_awaited()

    def test_fail_report_uses_bound_identity_and_missing_identity_sends_nothing(self) -> None:
        with mock.patch.dict(os.environ, _IDENTITY_ENV, clear=True), \
             mock.patch("urllib.request.urlopen") as urlopen:
            self.agent._agent_fail("signal-id", "test-failure")

        request = urlopen.call_args.args[0]
        headers = {name.lower(): value for name, value in request.header_items()}
        self.assertEqual(headers["x-axonos-webrtc-agent-key"], "fleet-agent-key")
        self.assertEqual(headers["x-axgt-session-id"], "248")
        self.assertEqual(headers["x-wallet-address"], _WALLET)
        self.assertEqual(headers["x-axgt-session-key"], "per-session-files-key")

        missing = {**_IDENTITY_ENV, "AXGT_SESSION_ID": ""}
        with mock.patch.dict(os.environ, missing, clear=True), \
             mock.patch("urllib.request.urlopen") as missing_urlopen:
            self.agent._agent_fail("signal-id", "test-failure")
        missing_urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
