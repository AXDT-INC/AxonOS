"""Unit tests for WebRTC codecs monkeypatching."""

from __future__ import annotations

import os
import sys
import unittest
import importlib

# Ensure the root of axonos_gate is in python path
_tests_dir = os.path.dirname(os.path.abspath(__file__))
_axonos_gate_root = os.path.dirname(_tests_dir)
if _axonos_gate_root not in sys.path:
    sys.path.insert(0, _axonos_gate_root)


class WebrtcCodecTests(unittest.TestCase):
    def setUp(self) -> None:
        # Save original environment
        self.original_env = dict(os.environ)

    def tearDown(self) -> None:
        # Restore original environment
        os.environ.clear()
        os.environ.update(self.original_env)
        
        # Reset defaults after testing custom environment
        import aiortc.codecs.vpx
        import aiortc.codecs.h264
        import webrtc_agent_main
        
        # Clear custom env vars so reload resets to default values
        for k in list(os.environ.keys()):
            if k.startswith("WEBRTC_VP8_") or k.startswith("WEBRTC_H264_"):
                del os.environ[k]
                
        importlib.reload(webrtc_agent_main)

    def test_monkeypatched_defaults(self) -> None:
        import aiortc.codecs.vpx
        import aiortc.codecs.h264
        import webrtc_agent_main

        # Ensure webrtc_agent_main is loaded/reloaded with default env
        importlib.reload(webrtc_agent_main)

        # Check VP8 defaults
        self.assertEqual(aiortc.codecs.vpx.DEFAULT_BITRATE, 3500000)
        self.assertEqual(aiortc.codecs.vpx.MIN_BITRATE, 1000000)
        self.assertEqual(aiortc.codecs.vpx.MAX_BITRATE, 8000000)

        # Check H.264 defaults
        self.assertEqual(aiortc.codecs.h264.DEFAULT_BITRATE, 4000000)
        self.assertEqual(aiortc.codecs.h264.MIN_BITRATE, 1500000)
        self.assertEqual(aiortc.codecs.h264.MAX_BITRATE, 10000000)

    def test_monkeypatched_custom_env(self) -> None:
        import aiortc.codecs.vpx
        import aiortc.codecs.h264
        
        # Set custom env vars
        os.environ["WEBRTC_VP8_DEFAULT_BITRATE"] = "5000000"
        os.environ["WEBRTC_VP8_MIN_BITRATE"] = "2000000"
        os.environ["WEBRTC_VP8_MAX_BITRATE"] = "12000000"
        
        os.environ["WEBRTC_H264_DEFAULT_BITRATE"] = "6000000"
        os.environ["WEBRTC_H264_MIN_BITRATE"] = "3000000"
        os.environ["WEBRTC_H264_MAX_BITRATE"] = "15000000"

        # Reload webrtc_agent_main to trigger monkeypatching with new env variables
        import webrtc_agent_main
        importlib.reload(webrtc_agent_main)

        self.assertEqual(aiortc.codecs.vpx.DEFAULT_BITRATE, 5000000)
        self.assertEqual(aiortc.codecs.vpx.MIN_BITRATE, 2000000)
        self.assertEqual(aiortc.codecs.vpx.MAX_BITRATE, 12000000)

        self.assertEqual(aiortc.codecs.h264.DEFAULT_BITRATE, 6000000)
        self.assertEqual(aiortc.codecs.h264.MIN_BITRATE, 3000000)
        self.assertEqual(aiortc.codecs.h264.MAX_BITRATE, 15000000)


if __name__ == "__main__":
    unittest.main()
