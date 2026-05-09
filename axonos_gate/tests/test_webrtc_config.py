"""Unit tests for WebRTC configuration helpers (no network)."""

from __future__ import annotations

import os
import sys
import unittest

_tests_dir = os.path.dirname(os.path.abspath(__file__))
_axonos_gate_root = os.path.dirname(_tests_dir)
if _axonos_gate_root not in sys.path:
    sys.path.insert(0, _axonos_gate_root)


class WebrtcConfigTests(unittest.TestCase):
    def tearDown(self) -> None:
        for k in list(os.environ.keys()):
            if k.startswith("WEBRTC_"):
                del os.environ[k]

    def test_default_stun_when_empty(self) -> None:
        from webrtc.config import ice_servers_for_client

        servers = ice_servers_for_client()
        self.assertTrue(len(servers) >= 1)
        self.assertIn("urls", servers[0])

    def test_validate_sdp_minimal(self) -> None:
        from webrtc.config import validate_sdp

        self.assertTrue(validate_sdp("v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\n"))
        self.assertFalse(validate_sdp(""))
        self.assertFalse(validate_sdp("garbage" * 10000))


if __name__ == "__main__":
    unittest.main()
