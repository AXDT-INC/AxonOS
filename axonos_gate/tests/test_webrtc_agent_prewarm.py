"""Focused tests for overlapping WebRTC runtime capability prewarm."""

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


class WebrtcAgentPrewarmTests(unittest.TestCase):
    def test_display_video_and_audio_probes_overlap(self) -> None:
        import webrtc_agent_main as agent
        from webrtc import capture

        capture._reset_runtime_probe_caches_for_tests()
        started = 0
        max_active = 0
        active = 0
        all_started: asyncio.Event | None = None

        async def fake_to_thread(fn, *args):  # type: ignore[no-untyped-def]
            nonlocal started, max_active, active, all_started
            if all_started is None:
                all_started = asyncio.Event()
            started += 1
            active += 1
            max_active = max(max_active, active)
            if started == 3:
                all_started.set()
            await asyncio.wait_for(all_started.wait(), timeout=1)
            try:
                return fn(*args)
            finally:
                active -= 1

        with mock.patch.object(agent.asyncio, "to_thread", side_effect=fake_to_thread), \
             mock.patch.object(agent, "_ensure_display_ready", return_value=True), \
             mock.patch.object(capture, "audio_enabled", return_value=True), \
             mock.patch.object(capture, "resolve_capture_backend", return_value="nvenc"), \
             mock.patch.object(capture, "pulse_runtime_ok", return_value=True):
            asyncio.run(agent._prewarm_session_capabilities())

        self.assertEqual(max_active, 3)

    def test_audio_disabled_skips_pulse_probe(self) -> None:
        import webrtc_agent_main as agent
        from webrtc import capture

        async def immediate_to_thread(fn, *args):  # type: ignore[no-untyped-def]
            return fn(*args)

        with mock.patch.object(agent.asyncio, "to_thread", side_effect=immediate_to_thread), \
             mock.patch.object(agent, "_ensure_display_ready", return_value=True), \
             mock.patch.object(capture, "audio_enabled", return_value=False), \
             mock.patch.object(capture, "resolve_capture_backend", return_value="mss"), \
             mock.patch.object(capture, "pulse_runtime_ok") as pulse_probe:
            asyncio.run(agent._prewarm_session_capabilities())

        pulse_probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
