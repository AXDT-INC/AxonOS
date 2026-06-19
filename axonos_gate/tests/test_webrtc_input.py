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
    def test_click_clears_stuck_button_mask(self, run: mock.MagicMock) -> None:
        env = {"DISPLAY": ":0"}
        self.agent._sync_mouse_buttons(1, env)
        run.reset_mock()
        self.agent._apply_input_json('{"t":"click","button":1,"x":5,"y":5}')
        cmds = [c.args[0] for c in run.call_args_list if c.args]
        self.assertIn(["xdotool", "mouseup", "1"], cmds)
        self.assertIn(["xdotool", "click", "1"], cmds)
        self.assertEqual(self.agent._mouse_button_mask, 0)

    @mock.patch("webrtc_agent_main.subprocess.run")
    def test_apply_clipboard_json_paste(self, run: mock.MagicMock) -> None:
        with mock.patch("webrtc_agent_main._set_x_clipboard", return_value=True) as clip:
            with mock.patch("webrtc_agent_main.time.sleep"):
                self.agent._apply_clipboard_json('{"t":"paste","text":"hello"}')
        clip.assert_called_once()
        cmds = [c.args[0] for c in run.call_args_list if c.args]
        self.assertIn(["xdotool", "key", "--clearmodifiers", "ctrl+v"], cmds)

    @mock.patch("webrtc_agent_main.subprocess.run")
    def test_mousedown_when_already_down_releases_then_presses(self, run: mock.MagicMock) -> None:
        env = {"DISPLAY": ":0"}
        self.agent._sync_mouse_buttons(1, env)
        run.reset_mock()
        self.agent._apply_input_json(
            '{"t":"mousedown","button":1,"buttons":1,"x":0,"y":0}'
        )
        cmds = [c.args[0] for c in run.call_args_list if c.args]
        self.assertIn(["xdotool", "mouseup", "1"], cmds)
        self.assertIn(["xdotool", "mousedown", "1"], cmds)
        self.assertEqual(self.agent._mouse_button_mask, 1)

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

    def test_enqueue_mousedown_evicts_when_queue_full_of_paste(self) -> None:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=2)
        q.put_nowait('{"t":"paste","text":"a"}')
        q.put_nowait('{"t":"paste","text":"b"}')
        self.agent._enqueue_rtc_input(
            q, '{"t":"mousedown","button":1,"buttons":1,"x":0,"y":0}'
        )
        kinds = []
        while not q.empty():
            kinds.append(self.agent._input_kind_from_raw(q.get_nowait()))
        self.assertIn("mousedown", kinds)
        self.assertEqual(kinds[-1], "mousedown")

    def test_enqueue_burst_moves_then_click(self) -> None:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=8)
        for i in range(8):
            q.put_nowait(f'{{"t":"move","x":{i},"y":0,"buttons":0}}')
        self.agent._enqueue_rtc_input(
            q, '{"t":"mousedown","button":1,"buttons":1,"x":0,"y":0}'
        )
        items: list[str] = []
        while not q.empty():
            items.append(q.get_nowait())
        self.assertEqual(self.agent._input_kind_from_raw(items[-1]), "mousedown")

    def test_session_cycle_resets_button_mask(self) -> None:
        env = {"DISPLAY": ":0"}
        with mock.patch("webrtc_agent_main.subprocess.run") as run:
            self.agent._sync_mouse_buttons(1, env)
            self.assertEqual(self.agent._mouse_button_mask, 1)
            self.agent._reset_mouse_button_state()
            self.assertEqual(self.agent._mouse_button_mask, 0)
            run.reset_mock()
            self.agent._reset_mouse_button_state(env)
            self.assertEqual(self.agent._mouse_button_mask, 0)
            self.assertEqual(run.call_count, 0)

    @mock.patch("webrtc_agent_main.subprocess.run")
    def test_mixed_left_right_click_and_drag(self, run: mock.MagicMock) -> None:
        self.agent._apply_input_json(
            '{"t":"mousedown","button":1,"buttons":1,"x":0,"y":0}'
        )
        self.agent._apply_input_json('{"t":"move","x":4,"y":4,"buttons":1}')
        self.agent._apply_input_json(
            '{"t":"mouseup","button":1,"buttons":0,"x":4,"y":4}'
        )
        self.agent._apply_input_json(
            '{"t":"mousedown","button":3,"buttons":4,"x":10,"y":10}'
        )
        self.agent._apply_input_json(
            '{"t":"mouseup","button":3,"buttons":0,"x":10,"y":10}'
        )
        cmds = [c.args[0] for c in run.call_args_list if c.args]
        self.assertIn(["xdotool", "mousedown", "1"], cmds)
        self.assertIn(["xdotool", "mouseup", "1"], cmds)
        self.assertIn(["xdotool", "mousedown", "3"], cmds)
        self.assertIn(["xdotool", "mouseup", "3"], cmds)
        self.assertEqual(self.agent._mouse_button_mask, 0)

    def test_input_kind_helpers_no_throw_on_garbage(self) -> None:
        self.assertEqual(self.agent._input_kind_from_raw("{not json"), "")
        self.assertEqual(self.agent._input_buttons_from_raw("{not json"), 0)
        self.assertEqual(self.agent._input_buttons_from_raw('{"t":"move","buttons":99}'), 3)

    @mock.patch("webrtc_agent_main.subprocess.run")
    def test_mouseup_without_coords_skips_mousemove(self, run: mock.MagicMock) -> None:
        env = {"DISPLAY": ":0"}
        self.agent._sync_mouse_buttons(1, env)
        run.reset_mock()
        self.agent._apply_input_json('{"t":"mouseup","button":1,"buttons":0}')
        cmds = [c.args[0] for c in run.call_args_list if c.args]
        self.assertNotIn(["xdotool", "mousemove", "0", "0"], cmds)
        self.assertIn(["xdotool", "mouseup", "1"], cmds)
        self.assertEqual(self.agent._mouse_button_mask, 0)

    @mock.patch("webrtc_agent_main.subprocess.run")
    def test_wheel_vertical_maps_to_buttons_4_5(self, run: mock.MagicMock) -> None:
        self.agent._apply_input_json('{"t":"wheel","x":10,"y":20,"dx":0,"dy":3}')
        cmds = [c.args[0] for c in run.call_args_list if c.args]
        self.assertIn(["xdotool", "mousemove", "10", "20"], cmds)
        self.assertIn(
            ["xdotool", "click", "--repeat", "3", "5"], cmds,
        )


if __name__ == "__main__":
    unittest.main()
