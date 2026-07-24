"""Focused tests for fail-closed per-session WebRTC agent identity."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock


_tests_dir = os.path.dirname(os.path.abspath(__file__))
_axonos_gate_root = os.path.dirname(_tests_dir)
if _axonos_gate_root not in sys.path:
    sys.path.insert(0, _axonos_gate_root)


_WALLET = "0x" + ("ab" * 20)
_IDENTITY_ENV = {
    "WEBRTC_ENABLED": "true",
    "AXGT_SESSION_ID": "248",
    "AXGT_WALLET_ADDRESS": _WALLET,
    "AXGT_WEBRTC_AGENT_TOKEN": "signed-session-capability",
}


class _StopLoop(RuntimeError):
    pass


class WebrtcAgentIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        import webrtc_agent_main as agent

        self.agent = agent
        self._token_state = tempfile.TemporaryDirectory()
        original_state_path = self.agent._AGENT_TOKEN_STATE_PATH
        self.agent._AGENT_TOKEN_STATE_PATH = os.path.join(
            self._token_state.name,
            "runtime",
            "webrtc-agent-token",
        )
        self.addCleanup(self._token_state.cleanup)
        self.addCleanup(
            setattr,
            self.agent,
            "_AGENT_TOKEN_STATE_PATH",
            original_state_path,
        )
        self.agent._runtime_agent_identity = None
        self.agent._runtime_agent_token = None
        self.addCleanup(setattr, self.agent, "_runtime_agent_identity", None)
        self.addCleanup(setattr, self.agent, "_runtime_agent_token", None)

    @staticmethod
    def _scheduled_token(
        issued_at: float,
        expires_at: float,
        *,
        session_id: int = 248,
        wallet: str = _WALLET,
    ) -> str:
        raw = json.dumps(
            {
                "iat": issued_at,
                "exp": expires_at,
                "sid": session_id,
                "wallet": wallet,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        payload = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        return f"header.{payload}.signature"

    def test_agent_headers_include_complete_canonical_identity(self) -> None:
        env = {**_IDENTITY_ENV, "AXGT_SESSION_ID": "00248", "AXGT_WALLET_ADDRESS": _WALLET.upper()}
        with mock.patch.dict(os.environ, env, clear=True):
            headers = self.agent._agent_headers(json_content=True)

        self.assertEqual(
            headers,
            {
                "X-AXGT-Session-ID": "248",
                "X-Wallet-Address": _WALLET,
                "X-AXGT-WebRTC-Token": "signed-session-capability",
                "Content-Type": "application/json",
            },
        )

    def test_gate_url_is_forced_to_internal_listener_by_runtime_mode(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"WEBRTC_GATE_INTERNAL_URL": "http://127.0.0.1:8889"},
            clear=True,
        ):
            self.assertEqual(self.agent._gate_url(), "http://127.0.0.1:8890")
        with mock.patch.dict(
            os.environ,
            {
                "AXGT_SESSION_ID": "248",
                "WEBRTC_GATE_INTERNAL_URL": "http://axonos-gate:8890/",
            },
            clear=True,
        ):
            self.assertEqual(self.agent._gate_url(), "http://axonos-gate:8890")

    def test_multi_session_identity_does_not_require_fleet_or_files_secret(self) -> None:
        with mock.patch.dict(os.environ, _IDENTITY_ENV, clear=True):
            headers = self.agent._agent_headers()

        self.assertEqual(headers["X-AXGT-WebRTC-Token"], "signed-session-capability")
        self.assertNotIn("X-AxonOS-WebRTC-Agent-Key", headers)
        self.assertNotIn("X-AXGT-Session-Key", headers)

    def test_capability_refresh_schedule_uses_signed_lifetime_window(self) -> None:
        token = self._scheduled_token(1000.0, 1900.0)
        headers = {"X-AXGT-WebRTC-Token": token}

        self.assertFalse(self.agent._capability_refresh_due(headers, now=1599.0))
        self.assertTrue(self.agent._capability_refresh_due(headers, now=1600.0))
        self.assertFalse(
            self.agent._capability_refresh_due(
                {"X-AXGT-WebRTC-Token": "not-a-jwt"},
                now=999999.0,
            )
        )

    def test_refresh_replaces_only_the_exact_runtime_identity_token(self) -> None:
        refreshed_token = self._scheduled_token(2000.0, 2900.0)

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return json.dumps({"ok": True, "token": refreshed_token}).encode()

        with mock.patch.dict(os.environ, _IDENTITY_ENV, clear=True), \
             mock.patch.object(self.agent.time, "time", return_value=2000.0), \
             mock.patch("urllib.request.urlopen", return_value=_Response()) as urlopen:
            current = self.agent._agent_headers()
            updated = self.agent._refresh_agent_capability(
                "http://axonos-gate:8890",
                current,
            )
            dynamic = self.agent._agent_headers()
            # Simulate Supervisor replacing only the agent process: its globals
            # are lost while the container's root-owned /run state survives.
            self.agent._runtime_agent_identity = None
            self.agent._runtime_agent_token = None
            restarted = self.agent._agent_headers()

        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertEqual(updated["X-AXGT-WebRTC-Token"], refreshed_token)
        self.assertEqual(dynamic["X-AXGT-WebRTC-Token"], refreshed_token)
        self.assertEqual(restarted["X-AXGT-WebRTC-Token"], refreshed_token)
        state_mode = stat.S_IMODE(
            os.stat(self.agent._AGENT_TOKEN_STATE_PATH).st_mode
        )
        self.assertEqual(state_mode, 0o600)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://axonos-gate:8890/api/webrtc/agent/refresh")
        self.assertEqual(request.method, "POST")

        foreign_token = self._scheduled_token(
            2000.0,
            2900.0,
            session_id=249,
        )
        with self.assertRaisesRegex(ValueError, "invalid refreshed"):
            self.agent._set_runtime_agent_token(current, foreign_token)
        self.assertEqual(self.agent._runtime_agent_token, refreshed_token)

    def test_documented_legacy_single_container_uses_key_only_identity(self) -> None:
        env = {
            "WEBRTC_AGENT_INTERNAL_KEY": "fleet-agent-key",
            "AXGT_USER_CONTAINER_ENABLED": "false",
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
            {
                "WEBRTC_AGENT_INTERNAL_KEY": "fleet-agent-key",
                "AXGT_USER_CONTAINER_ENABLED": "true",
                "AXGT_MULTI_SESSION_ENABLED": "false",
            },
            clear=True,
        ):
            with self.assertRaises(ValueError):
                self.agent._agent_headers()

    def test_agent_headers_reject_missing_or_malformed_identity(self) -> None:
        invalid_overrides = {
            "AXGT_SESSION_ID": "",
            "AXGT_WALLET_ADDRESS": "",
            "AXGT_WEBRTC_AGENT_TOKEN": "",
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

        with mock.patch.dict(
            os.environ,
            {**_IDENTITY_ENV, "AXGT_WEBRTC_AGENT_TOKEN": "token\nsmuggling"},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "agent credential"):
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

        env = {**_IDENTITY_ENV, "AXGT_WEBRTC_AGENT_TOKEN": ""}
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
        self.assertEqual(
            seen_headers[0]["X-AXGT-WebRTC-Token"],
            "signed-session-capability",
        )
        self.assertNotIn("X-AXGT-Session-Key", seen_headers[0])
        self.assertNotIn("X-AxonOS-WebRTC-Agent-Key", seen_headers[0])
        run_session.assert_not_awaited()

    def test_fail_report_uses_bound_identity_and_missing_identity_sends_nothing(self) -> None:
        with mock.patch.dict(os.environ, _IDENTITY_ENV, clear=True), \
             mock.patch("urllib.request.urlopen") as urlopen:
            self.agent._agent_fail("signal-id", "test-failure")

        request = urlopen.call_args.args[0]
        headers = {name.lower(): value for name, value in request.header_items()}
        self.assertEqual(headers["x-axgt-session-id"], "248")
        self.assertEqual(headers["x-wallet-address"], _WALLET)
        self.assertEqual(
            headers["x-axgt-webrtc-token"],
            "signed-session-capability",
        )
        self.assertNotIn("x-axonos-webrtc-agent-key", headers)
        self.assertNotIn("x-axgt-session-key", headers)

        missing = {**_IDENTITY_ENV, "AXGT_SESSION_ID": ""}
        with mock.patch.dict(os.environ, missing, clear=True), \
             mock.patch("urllib.request.urlopen") as missing_urlopen:
            self.agent._agent_fail("signal-id", "test-failure")
        missing_urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
