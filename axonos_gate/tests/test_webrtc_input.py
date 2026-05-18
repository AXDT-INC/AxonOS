"""Unit tests for WebRTC input button tracking (no display)."""

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


class WebrtcInputTests(unittest.TestCase):
    def setUp(self) -> None:
        import webrtc_agent_main as agent

        self.agent = agent
        agent._mouse_button_mask = 0

    def test_button_bit(self) -> None:
        self.assertEqual(self.agent._button_bit(1), 1)
        self.assertEqual(self.agent._button_bit(2), 2)
        self.assertEqual(self.agent._button_bit(3), 4)

    @mock.patch("webrtc_agent_main.subprocess.run")
    def test_move_with_buttons_presses_before_move(self, run: mock.MagicMock) -> None:
        env = {"DISPLAY": ":0"}
        self.agent._sync_mouse_buttons(1, env)
        run.reset_mock()
        self.agent._apply_input_json(
            '{"t":"move","x":10,"y":20,"buttons":1}'
        )
        cmds = [c.args[0] for c in run.call_args_list if c.args]
        self.assertIn(["xdotool", "mousemove", "10", "20"], cmds)

    @mock.patch("webrtc_agent_main.subprocess.run")
    def test_mousedown_then_move_then_mouseup(self, run: mock.MagicMock) -> None:
        self.agent._apply_input_json(
            '{"t":"mousedown","button":1,"buttons":1,"x":0,"y":0}'
        )
        self.agent._apply_input_json('{"t":"move","x":5,"y":5,"buttons":1}')
        self.agent._apply_input_json(
            '{"t":"mouseup","button":1,"buttons":0,"x":5,"y":5}'
        )
        cmds = [c.args[0] for c in run.call_args_list if c.args]
        self.assertIn(["xdotool", "mousedown", "1"], cmds)
        self.assertIn(["xdotool", "mousemove", "5", "5"], cmds)
        self.assertIn(["xdotool", "mouseup", "1"], cmds)
        self.assertEqual(self.agent._mouse_button_mask, 0)

    @mock.patch("webrtc_agent_main.subprocess.run")
    def test_reset_mouse_button_state_releases_buttons(self, run: mock.MagicMock) -> None:
        env = {"DISPLAY": ":0"}
        self.agent._sync_mouse_buttons(1, env)
        run.reset_mock()
        self.agent._reset_mouse_button_state(env)
        self.assertEqual(self.agent._mouse_button_mask, 0)
        cmds = [c.args[0] for c in run.call_args_list if c.args]
        self.assertIn(["xdotool", "mouseup", "1"], cmds)

    def test_enqueue_mousedown_flushes_queued_moves(self) -> None:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=4)
        for i in range(4):
            q.put_nowait(f'{{"t":"move","x":{i},"y":0}}')
        self.agent._enqueue_rtc_input(
            q, '{"t":"mousedown","button":1,"buttons":1,"x":0,"y":0}'
        )
        items: list[str] = []
        while not q.empty():
            items.append(q.get_nowait())
        self.assertEqual(len(items), 1)
        self.assertEqual(self.agent._input_kind_from_raw(items[0]), "mousedown")

    def test_enqueue_plain_move_dropped_when_full(self) -> None:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
        q.put_nowait('{"t":"paste","text":"x"}')
        self.agent._enqueue_rtc_input(q, '{"t":"move","x":1,"y":2,"buttons":0}')
        self.assertEqual(q.qsize(), 1)
        self.assertEqual(self.agent._input_kind_from_raw(q.get_nowait()), "paste")

    def test_enqueue_drag_move_flushes_plain_moves(self) -> None:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=2)
        q.put_nowait('{"t":"move","x":0,"y":0}')
        q.put_nowait('{"t":"move","x":1,"y":1}')
        self.agent._enqueue_rtc_input(q, '{"t":"move","x":2,"y":2,"buttons":1}')
        items: list[str] = []
        while not q.empty():
            items.append(q.get_nowait())
        self.assertTrue(
            any(self.agent._input_buttons_from_raw(item) for item in items)
        )


if __name__ == "__main__":
    unittest.main()
