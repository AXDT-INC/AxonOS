"""Unit tests for low-latency X11 input helper functions."""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

_tests_dir = os.path.dirname(os.path.abspath(__file__))
_axonos_gate_root = os.path.dirname(_tests_dir)
if _axonos_gate_root not in sys.path:
    sys.path.insert(0, _axonos_gate_root)


class WebrtcX11InputTests(unittest.TestCase):
    def setUp(self) -> None:
        import webrtc.x11_input as x11
        x11._display_ptr = None
        x11._display_key = None
        x11._reconnect_cooldown = 0.0
        x11._lib = None
        x11._libfixes = None
        x11._libxtst = None

    @mock.patch("webrtc.x11_input._load_lib")
    @mock.patch("webrtc.x11_input._load_libfixes")
    @mock.patch("webrtc.x11_input._open_display")
    def test_get_cursor_info_success(
        self,
        mock_open_display: mock.Mock,
        mock_load_libfixes: mock.Mock,
        mock_load_lib: mock.Mock,
    ) -> None:
        from webrtc.x11_input import get_cursor_info

        # Setup mock library and structures
        mock_lib = mock.Mock()
        mock_load_lib.return_value = mock_lib
        mock_libfixes = mock.Mock()
        mock_load_libfixes.return_value = mock_libfixes
        
        mock_open_display.return_value = mock.sentinel.dpy
        
        # Mock struct return
        mock_img = mock.Mock()
        mock_img.cursor_serial = 12345
        mock_img.width = 2
        mock_img.height = 2
        mock_img.xhot = 1
        mock_img.yhot = 1
        mock_img.name = b"hand2"
        mock_img.pixels = [0] * 4
        
        mock_img_ptr = mock.Mock()
        mock_img_ptr.contents = mock_img
        
        mock_libfixes.XFixesGetCursorImage.return_value = mock_img_ptr

        res = get_cursor_info({"DISPLAY": ":0"})
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.get("serial"), 12345)
        self.assertTrue(res.get("changed"))
        self.assertEqual(res.get("name"), "hand2")
        self.assertEqual(res.get("width"), 2)
        self.assertEqual(res.get("height"), 2)
        self.assertEqual(res.get("xhot"), 1)
        self.assertEqual(res.get("yhot"), 1)
        self.assertIsNotNone(res.get("img"))
        
        mock_libfixes.XFixesGetCursorImage.assert_called_once_with(mock.sentinel.dpy)
        mock_lib.XFree.assert_called_once_with(mock_img_ptr)

    @mock.patch("webrtc.x11_input._load_lib")
    @mock.patch("webrtc.x11_input._load_libfixes")
    @mock.patch("webrtc.x11_input._open_display")
    def test_get_cursor_info_none_img(
        self,
        mock_open_display: mock.Mock,
        mock_load_libfixes: mock.Mock,
        mock_load_lib: mock.Mock,
    ) -> None:
        from webrtc.x11_input import get_cursor_info

        mock_load_lib.return_value = mock.Mock()
        mock_libfixes = mock.Mock()
        mock_load_libfixes.return_value = mock_libfixes
        mock_open_display.return_value = mock.sentinel.dpy
        
        mock_libfixes.XFixesGetCursorImage.return_value = None

        res = get_cursor_info({"DISPLAY": ":0"})
        self.assertIsNone(res)

    @mock.patch("webrtc.x11_input._load_lib")
    @mock.patch("webrtc.x11_input._load_libxtst")
    @mock.patch("webrtc.x11_input._open_display")
    def test_xtest_mousemove(
        self,
        mock_open_display: mock.Mock,
        mock_load_libxtst: mock.Mock,
        mock_load_lib: mock.Mock,
    ) -> None:
        from webrtc.x11_input import xtest_mousemove
        mock_lib = mock.Mock()
        mock_load_lib.return_value = mock_lib
        mock_libxtst = mock.Mock()
        mock_load_libxtst.return_value = mock_libxtst
        mock_open_display.return_value = mock.sentinel.dpy

        res = xtest_mousemove(100, 200, {"DISPLAY": ":0"})
        self.assertTrue(res)
        mock_libxtst.XTestFakeMotionEvent.assert_called_once_with(mock.sentinel.dpy, -1, 100, 200, 0)
        mock_lib.XFlush.assert_called_once_with(mock.sentinel.dpy)

    @mock.patch("webrtc.x11_input._load_lib")
    @mock.patch("webrtc.x11_input._load_libxtst")
    @mock.patch("webrtc.x11_input._open_display")
    def test_xtest_mousedown(
        self,
        mock_open_display: mock.Mock,
        mock_load_libxtst: mock.Mock,
        mock_load_lib: mock.Mock,
    ) -> None:
        from webrtc.x11_input import xtest_mousedown
        mock_lib = mock.Mock()
        mock_load_lib.return_value = mock_lib
        mock_libxtst = mock.Mock()
        mock_load_libxtst.return_value = mock_libxtst
        mock_open_display.return_value = mock.sentinel.dpy

        res = xtest_mousedown(1, {"DISPLAY": ":0"})
        self.assertTrue(res)
        mock_libxtst.XTestFakeButtonEvent.assert_called_once_with(mock.sentinel.dpy, 1, 1, 0)
        mock_lib.XFlush.assert_called_once_with(mock.sentinel.dpy)

    @mock.patch("webrtc.x11_input._load_lib")
    @mock.patch("webrtc.x11_input._load_libxtst")
    @mock.patch("webrtc.x11_input._open_display")
    def test_xtest_mouseup(
        self,
        mock_open_display: mock.Mock,
        mock_load_libxtst: mock.Mock,
        mock_load_lib: mock.Mock,
    ) -> None:
        from webrtc.x11_input import xtest_mouseup
        mock_lib = mock.Mock()
        mock_load_lib.return_value = mock_lib
        mock_libxtst = mock.Mock()
        mock_load_libxtst.return_value = mock_libxtst
        mock_open_display.return_value = mock.sentinel.dpy

        res = xtest_mouseup(1, {"DISPLAY": ":0"})
        self.assertTrue(res)
        mock_libxtst.XTestFakeButtonEvent.assert_called_once_with(mock.sentinel.dpy, 1, 0, 0)
        mock_lib.XFlush.assert_called_once_with(mock.sentinel.dpy)

    @mock.patch("webrtc.x11_input._load_lib")
    @mock.patch("webrtc.x11_input._load_libxtst")
    @mock.patch("webrtc.x11_input._open_display")
    def test_xtest_click(
        self,
        mock_open_display: mock.Mock,
        mock_load_libxtst: mock.Mock,
        mock_load_lib: mock.Mock,
    ) -> None:
        from webrtc.x11_input import xtest_click
        mock_lib = mock.Mock()
        mock_load_lib.return_value = mock_lib
        mock_libxtst = mock.Mock()
        mock_load_libxtst.return_value = mock_libxtst
        mock_open_display.return_value = mock.sentinel.dpy

        res = xtest_click(1, 2, {"DISPLAY": ":0"})
        self.assertTrue(res)
        self.assertEqual(mock_libxtst.XTestFakeButtonEvent.call_count, 4)
        mock_libxtst.XTestFakeButtonEvent.assert_has_calls([
            mock.call(mock.sentinel.dpy, 1, 1, 0),
            mock.call(mock.sentinel.dpy, 1, 0, 0),
            mock.call(mock.sentinel.dpy, 1, 1, 0),
            mock.call(mock.sentinel.dpy, 1, 0, 0),
        ])
        self.assertEqual(mock_lib.XFlush.call_count, 4)


if __name__ == "__main__":
    unittest.main()
