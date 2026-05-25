"""Unit tests for VP8 bitrate env patch (MSS capture on public-beta)."""

from __future__ import annotations

import importlib
import os
import sys
import unittest

_tests_dir = os.path.dirname(os.path.abspath(__file__))
_axonos_gate_root = os.path.dirname(_tests_dir)
if _axonos_gate_root not in sys.path:
    sys.path.insert(0, _axonos_gate_root)


class WebrtcVp8BitrateTests(unittest.TestCase):
    def setUp(self) -> None:
        import webrtc_agent_main as agent

        self.agent = agent
        agent._vp8_bitrate_patch_applied = False
        for key in list(os.environ.keys()):
            if key.startswith("WEBRTC_VP8_"):
                del os.environ[key]

    def test_apply_raises_aiortc_limits(self) -> None:
        os.environ["WEBRTC_VP8_MAX_BITRATE"] = "3000000"
        os.environ["WEBRTC_VP8_DEFAULT_BITRATE"] = "2800000"
        os.environ["WEBRTC_VP8_MIN_BITRATE"] = "600000"
        self.agent._apply_vp8_bitrate_patch()
        import aiortc.codecs.vpx as vpx

        self.assertEqual(vpx.MAX_BITRATE, 3_000_000)
        self.assertEqual(vpx.DEFAULT_BITRATE, 2_800_000)
        self.assertEqual(vpx.MIN_BITRATE, 600_000)

    def test_apply_is_idempotent(self) -> None:
        os.environ["WEBRTC_VP8_MAX_BITRATE"] = "2000000"
        self.agent._apply_vp8_bitrate_patch()
        import aiortc.codecs.vpx as vpx

        vpx.MAX_BITRATE = 999
        self.agent._apply_vp8_bitrate_patch()
        self.assertEqual(vpx.MAX_BITRATE, 999)


if __name__ == "__main__":
    unittest.main()
