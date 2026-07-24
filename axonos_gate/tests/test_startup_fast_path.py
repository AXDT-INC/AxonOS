"""Regression guards for the desktop/WebRTC startup critical path."""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import unittest


_TESTS_DIR = Path(__file__).resolve().parent
_GATE_ROOT = _TESTS_DIR.parent
_REPO_ROOT = _GATE_ROOT.parent
if str(_GATE_ROOT) not in sys.path:
    sys.path.insert(0, str(_GATE_ROOT))


class StartupFastPathSourceTests(unittest.TestCase):
    def test_supervisor_is_the_only_ipfs_daemon_owner(self) -> None:
        startup = (_REPO_ROOT / "startup.sh").read_text(encoding="utf-8")
        supervisor = (_REPO_ROOT / "supervisord.conf").read_text(encoding="utf-8")

        self.assertNotIn("ipfs daemon", startup)
        self.assertIn("[program:ipfs]", supervisor)
        ipfs_block = supervisor.split("[program:ipfs]", 1)[1].split("[program:", 1)[0]
        self.assertIn("command=/usr/local/bin/ipfs daemon --enable-gc --routing=dht", ipfs_block)
        self.assertIn("user=aXonian", ipfs_block)
        self.assertIn('IPFS_PATH="/home/aXonian/.ipfs"', ipfs_block)
        self.assertNotIn("su - aXonian", ipfs_block)

    def test_webrtc_agent_supervisor_has_no_fixed_readiness_delay(self) -> None:
        source = (_REPO_ROOT / "supervisord.conf").read_text(encoding="utf-8")
        block = source.split("[program:webrtc-agent]", 1)[1].split("[program:", 1)[0]

        self.assertIn("exec /usr/bin/python3 /axonos_gate/webrtc_agent_main.py", block)
        self.assertNotIn("xset q", block)
        self.assertNotIn("sleep 3", block)


class IceGatheringFastPathTests(unittest.IsolatedAsyncioTestCase):
    class Peer:
        def __init__(self, state: str) -> None:
            self.iceGatheringState = state
            self.callback = None

        def on(self, _event_name: str):
            def register(callback):
                self.callback = callback
                return callback

            return register

    async def test_already_complete_ice_returns_without_timeout(self) -> None:
        import webrtc_agent_main as agent

        peer = self.Peer("complete")
        completed = await agent._wait_for_ice_gathering_complete(peer, timeout_s=0.01)

        self.assertTrue(completed)

    async def test_ice_transition_resolves_waiter(self) -> None:
        import webrtc_agent_main as agent

        peer = self.Peer("gathering")
        waiter = asyncio.create_task(
            agent._wait_for_ice_gathering_complete(peer, timeout_s=0.1)
        )
        await asyncio.sleep(0)
        peer.iceGatheringState = "complete"
        self.assertIsNotNone(peer.callback)
        peer.callback()

        self.assertTrue(await waiter)

    async def test_ice_timeout_returns_false(self) -> None:
        import webrtc_agent_main as agent

        peer = self.Peer("gathering")

        self.assertFalse(
            await agent._wait_for_ice_gathering_complete(peer, timeout_s=0.001)
        )


if __name__ == "__main__":
    unittest.main()
