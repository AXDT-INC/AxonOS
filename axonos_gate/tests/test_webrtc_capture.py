"""Unit tests for WebRTC capture backend helpers."""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

_tests_dir = os.path.dirname(os.path.abspath(__file__))
_axonos_gate_root = os.path.dirname(_tests_dir)
if _axonos_gate_root not in sys.path:
    sys.path.insert(0, _axonos_gate_root)


class WebrtcCaptureTests(unittest.TestCase):
    def tearDown(self) -> None:
        for k in list(os.environ.keys()):
            if k.startswith("WEBRTC_CAPTURE_"):
                del os.environ[k]

    def test_capture_backend_aliases(self) -> None:
        from webrtc.capture import capture_backend_name

        os.environ["WEBRTC_CAPTURE_BACKEND"] = "cpu"
        self.assertEqual(capture_backend_name(), "mss")
        os.environ["WEBRTC_CAPTURE_BACKEND"] = "gpu"
        self.assertEqual(capture_backend_name(), "nvenc")
        os.environ["WEBRTC_CAPTURE_BACKEND"] = "auto"
        self.assertEqual(capture_backend_name(), "auto")

    def test_bitrate_clamped(self) -> None:
        from webrtc.capture import capture_bitrate_bps

        os.environ["WEBRTC_CAPTURE_BITRATE"] = "500000"
        self.assertEqual(capture_bitrate_bps(), 1_000_000)
        os.environ["WEBRTC_CAPTURE_BITRATE"] = "12000000"
        self.assertEqual(capture_bitrate_bps(), 12_000_000)
        os.environ["WEBRTC_CAPTURE_BITRATE"] = "999999999"
        self.assertEqual(capture_bitrate_bps(), 20_000_000)
        os.environ["WEBRTC_CAPTURE_BITRATE"] = "8000000"
        os.environ["WEBRTC_CAPTURE_FPS"] = "30"
        self.assertEqual(capture_bitrate_bps(), 8_000_000)

    def test_default_fps(self) -> None:
        from webrtc.capture import capture_fps

        self.assertEqual(capture_fps(), 15.0)

    def test_x11grab_input(self) -> None:
        from webrtc.capture import x11grab_input

        self.assertEqual(x11grab_input(":0"), ":0.0+0,0")
        self.assertEqual(x11grab_input(":1"), ":1.0+0,0")
        self.assertEqual(x11grab_input(":0.0+100,50"), ":0.0+100,50")

    def test_scaled_output_size(self) -> None:
        from webrtc.capture import scaled_output_size

        self.assertEqual(scaled_output_size(1280, 720, 1920), (1280, 720, 1280, 720))
        src_w, src_h, out_w, out_h = scaled_output_size(3840, 2160, 1920)
        self.assertEqual((src_w, src_h), (3840, 2160))
        self.assertEqual(out_w, 1920)
        self.assertEqual(out_h % 2, 0)

    def test_build_nvenc_ffmpeg_cmd(self) -> None:
        from webrtc.capture import build_nvenc_ffmpeg_cmd

        cmd = build_nvenc_ffmpeg_cmd(
            display=":0",
            env={},
            src_w=1920,
            src_h=1080,
            out_w=1920,
            out_h=1080,
            fps=30.0,
            bitrate_bps=8_000_000,
            preset="p4",
        )
        joined = " ".join(cmd)
        self.assertIn("x11grab", joined)
        self.assertIn("h264_nvenc", joined)
        self.assertIn(":0.0+0,0", joined)
        self.assertIn("-b:v 8000000", joined)
        self.assertNotIn("scale=", joined)

    def test_build_nvenc_ffmpeg_cmd_scales(self) -> None:
        from webrtc.capture import build_nvenc_ffmpeg_cmd

        cmd = build_nvenc_ffmpeg_cmd(
            display=":0",
            env={},
            src_w=3840,
            src_h=2160,
            out_w=1920,
            out_h=1080,
            fps=30.0,
            bitrate_bps=8_000_000,
            preset="p4",
        )
        self.assertIn("scale=1920:1080:flags=lanczos", cmd)

    @mock.patch("webrtc.capture.nvenc_runtime_ok", return_value=False)
    def test_resolve_backend_falls_back_to_mss(self, _mock_nvenc: mock.Mock) -> None:
        from webrtc.capture import resolve_capture_backend

        os.environ["WEBRTC_CAPTURE_BACKEND"] = "auto"
        self.assertEqual(resolve_capture_backend({}), "mss")

    @mock.patch("webrtc.capture.nvenc_runtime_ok", return_value=True)
    def test_resolve_backend_auto_prefers_nvenc(self, _mock_nvenc: mock.Mock) -> None:
        from webrtc.capture import resolve_capture_backend

        os.environ["WEBRTC_CAPTURE_BACKEND"] = "auto"
        self.assertEqual(resolve_capture_backend({}), "nvenc")

    def test_prefer_h264_skips_mss(self) -> None:
        from webrtc.capture import prefer_h264_for_pc

        pc = mock.Mock()
        prefer_h264_for_pc(pc, "mss")
        pc.getTransceivers.assert_not_called()

    @mock.patch("webrtc.capture.logger")
    def test_prefer_h264_sets_transceiver(self, _log: mock.Mock) -> None:
        from webrtc.capture import prefer_h264_for_pc

        class FakeCap:
            mimeType = "video/H264"

        transceiver = mock.Mock(kind="video")
        pc = mock.Mock()
        pc.getTransceivers.return_value = [transceiver]

        with mock.patch("aiortc.RTCRtpSender") as sender:
            sender.getCapabilities.return_value = mock.Mock(codecs=[FakeCap()])
            prefer_h264_for_pc(pc, "nvenc")

        transceiver.setCodecPreferences.assert_called_once()


if __name__ == "__main__":
    unittest.main()
