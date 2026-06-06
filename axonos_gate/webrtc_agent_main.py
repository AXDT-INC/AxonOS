#!/usr/bin/env python3
"""In-container WebRTC streaming agent: claims SDP offers from the gate, answers with desktop video.

Environment:
  WEBRTC_ENABLED=true
  WEBRTC_AGENT_INTERNAL_KEY — shared secret with the gate (required)
  WEBRTC_GATE_INTERNAL_URL — gate base URL (default http://127.0.0.1:8889)
  WEBRTC_CAPTURE_DISPLAY — X display (default :0)
  WEBRTC_CAPTURE_MAX_WIDTH — scale bound (default 1920; matches the current session display)
  WEBRTC_CAPTURE_FPS — target FPS (default 15)
  WEBRTC_CAPTURE_BACKEND — auto | mss | nvenc (default auto; NVENC when GPU encode available)
  WEBRTC_CAPTURE_BITRATE — H.264 target bitrate for NVENC (default 8000000)
  WEBRTC_CAPTURE_NVENC_PRESET — NVENC preset (default p4)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("axonos.webrtc_agent")

_AXT = "X-AxonOS-WebRTC-Agent-Key"
_clipboard_owners: dict[str, subprocess.Popen[bytes]] = {}
# RFB-style pressed buttons: 1=left, 2=middle, 4=right.
_mouse_button_mask: int = 0
_last_input_monotonic: float = 0.0
_last_clipboard_write_monotonic: float = 0.0
_MOUSE_BUTTON_BITS = ((1, 1), (2, 2), (4, 3))
# Dedicated thread pool for input processing — avoids starving the asyncio event
# loop (which aiortc needs for SCTP acks / data-channel reads) and avoids
# competing with clipboard ops on the default executor.
_input_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="rtc-input")


def _truthy(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in ("1", "true", "yes", "on")


def _gate_url() -> str:
    return (os.getenv("WEBRTC_GATE_INTERNAL_URL") or "http://127.0.0.1:8889").rstrip("/")


def _agent_key() -> str:
    return (os.getenv("WEBRTC_AGENT_INTERNAL_KEY") or "").strip()


def _display() -> str:
    return (os.getenv("WEBRTC_CAPTURE_DISPLAY") or ":0").strip()


def _xauthority_path() -> str:
    return (os.getenv("XAUTHORITY") or "/home/aXonian/.Xauthority").strip()


def _display_env() -> dict[str, str]:
    return {**os.environ, "DISPLAY": _display(), "XAUTHORITY": _xauthority_path()}


_display_ready_cached = False


def _display_wait_timeout_seconds() -> float:
    raw = (os.getenv("WEBRTC_DISPLAY_WAIT_SECONDS") or "120").strip()
    try:
        return max(5.0, min(300.0, float(raw)))
    except ValueError:
        return 120.0


def _wait_for_display_ready() -> bool:
    """Block until X11 on WEBRTC_CAPTURE_DISPLAY accepts connections (session containers need this)."""
    env = _display_env()
    timeout_s = _display_wait_timeout_seconds()
    interval_s = 1.0
    deadline = time.monotonic() + timeout_s
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        try:
            probe = subprocess.run(
                ["xset", "q"],
                env=env,
                capture_output=True,
                timeout=4,
                check=False,
            )
            if probe.returncode == 0:
                try:
                    import mss

                    with mss.mss() as sct:
                        mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
                        sct.grab(mon)
                except Exception as exc:
                    logger.debug("display ready (xset ok) but mss probe failed (attempt %s): %s", attempt, exc)
                else:
                    logger.info(
                        "WebRTC display ready on %s after %s attempt(s)",
                        _display(),
                        attempt,
                    )
                    return True
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.debug("display wait attempt %s: %s", attempt, exc)
        time.sleep(interval_s)
    logger.error(
        "WebRTC display %s not ready within %.0fs (%s attempts)",
        _display(),
        timeout_s,
        attempt,
    )
    return False


def _ensure_display_ready() -> bool:
    """Wait for X11 once per container; re-check quickly if already warmed."""
    global _display_ready_cached
    if _display_ready_cached:
        try:
            probe = subprocess.run(
                ["xset", "q"],
                env=_display_env(),
                capture_output=True,
                timeout=3,
                check=False,
            )
            if probe.returncode == 0:
                return True
        except (OSError, subprocess.TimeoutExpired):
            pass
        _display_ready_cached = False
    if _wait_for_display_ready():
        _display_ready_cached = True
        return True
    return False


def _normalize_sdp(sdp: str) -> str:
    """Keep SDP line endings in the strict CRLF form Chrome's parser expects."""
    normalized = (sdp or "").replace("\r\n", "\n").replace("\r", "\n")
    normalized = "\r\n".join(normalized.split("\n"))
    return normalized.rstrip("\r\n") + "\r\n"


def _xdotool_key(obj: dict[str, Any]) -> str:
    key = str(obj.get("key") or "")
    code = str(obj.get("code") or "")
    by_key = {
        " ": "space",
        "Enter": "Return",
        "Backspace": "BackSpace",
        "Tab": "Tab",
        "Escape": "Escape",
        "Delete": "Delete",
        "ArrowLeft": "Left",
        "ArrowRight": "Right",
        "ArrowUp": "Up",
        "ArrowDown": "Down",
        "Home": "Home",
        "End": "End",
        "PageUp": "Page_Up",
        "PageDown": "Page_Down",
        "Insert": "Insert",
        "Shift": "Shift_L" if code != "ShiftRight" else "Shift_R",
        "Control": "Control_L" if code != "ControlRight" else "Control_R",
        "Alt": "Alt_L" if code != "AltRight" else "Alt_R",
        "Meta": "Super_L" if code != "MetaRight" else "Super_R",
        "CapsLock": "Caps_Lock",
        "-": "minus",
        "=": "equal",
        "[": "bracketleft",
        "]": "bracketright",
        ";": "semicolon",
        "'": "apostrophe",
        ",": "comma",
        ".": "period",
        "/": "slash",
        "\\": "backslash",
        "`": "grave",
        "~": "asciitilde",
        "!": "exclam",
        "@": "at",
        "#": "numbersign",
        "$": "dollar",
        "%": "percent",
        "^": "asciicircum",
        "&": "ampersand",
        "*": "asterisk",
        "(": "parenleft",
        ")": "parenright",
        "_": "underscore",
        "+": "plus",
        ":": "colon",
        '"': "quotedbl",
        "<": "less",
        ">": "greater",
        "?": "question",
        "|": "bar",
    }
    if key in by_key:
        return by_key[key]
    if code.startswith("F") and code[1:].isdigit():
        return code
    if code.startswith("Numpad") and code[len("Numpad"):].isdigit():
        return "KP_" + code[len("Numpad"):]
    if len(key) == 1:
        return key
    return ""


def _reap_xclip_popen(proc: subprocess.Popen[bytes] | None, *, fast: bool = False) -> None:
    """Wait for a prior xclip Popen to finish; avoid SIGTERM during its handoff.

    xclip (silent mode) claims the selection, forks a child to serve it, and the
    parent exits quickly. If we SIGTERM the parent while it is still between
    those steps—or before the child is ready—CLIPBOARD can be left empty or
    stale while GTK/Qt \"Paste\" reads it. Back-to-back ``t:clipboard`` messages
    (e.g. overlapping ``navigator.clipboard.readText()``) used to call
    ``terminate()`` here whenever ``poll()`` was still None, which matched that
    failure mode; Ctrl+V usually sends a single ``t:paste`` so it did not hit it.
    """
    if proc is None:
        return
    if fast:
        try:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=0.2)
        except (OSError, subprocess.TimeoutExpired):
            pass
        return
    try:
        proc.wait(timeout=3.0)
    except subprocess.TimeoutExpired:
        try:
            if proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=0.2)
        except (subprocess.TimeoutExpired, OSError):
            try:
                proc.kill()
                proc.wait(timeout=0.2)
            except (subprocess.TimeoutExpired, OSError):
                pass
    except OSError:
        pass


def _set_x_clipboard(text: str, env: dict[str, str]) -> bool:
    max_bytes = max(4096, int(os.getenv("WEBRTC_CLIPBOARD_MAX_BYTES", "524288")))
    data = text.encode("utf-8", errors="ignore")
    if len(data) > max_bytes:
        data = data[:max_bytes]
        logger.debug("clipboard truncated to %s bytes", max_bytes)
    ok = False
    for selection in ("clipboard", "primary"):
        old = _clipboard_owners.pop(selection, None)
        _reap_xclip_popen(old, fast=True)
        try:
            p = subprocess.Popen(
                ["xclip", "-selection", selection],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
            )
            try:
                p.communicate(input=data, timeout=5.0)
                _clipboard_owners[selection] = p
                ok = True
            except subprocess.TimeoutExpired:
                p.kill()
                p.communicate()
        except OSError:
            continue
    return ok


def _get_x_clipboard(env: dict[str, str]) -> str:
    # Prefer CLIPBOARD (Ctrl+C / explicit right-click Copy in modern apps) but
    # fall back to PRIMARY for the small set of apps (xterm, some legacy GTK
    # menus) whose right-click Copy only populates PRIMARY. The CLIPBOARD-first
    # order keeps text-selection PRIMARY from stomping the host clipboard once
    # CLIPBOARD has any content, which it does for the rest of any normal
    # session after the first explicit copy.
    for selection in ("clipboard", "primary"):
        try:
            p = subprocess.run(
                ["xclip", "-selection", selection, "-o"],
                check=False,
                timeout=2,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            if p.returncode == 0 and p.stdout:
                text = p.stdout.decode("utf-8", errors="ignore")
                if text:
                    return text
        except (OSError, subprocess.TimeoutExpired):
            continue
    return ""


def _get_x_clipboard_for_browser_poll(env: dict[str, str]) -> str:
    """CLIPBOARD only for WebRTC host sync — PRIMARY often mirrors icon labels / titles."""
    if _truthy("WEBRTC_CLIPBOARD_POLL_PRIMARY"):
        return _get_x_clipboard(env)
    try:
        p = subprocess.run(
            ["xclip", "-selection", "clipboard", "-o"],
            check=False,
            timeout=2,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if p.returncode == 0 and p.stdout:
            text = p.stdout.decode("utf-8", errors="ignore")
            if text:
                return text
    except (OSError, subprocess.TimeoutExpired):
        pass
    return ""


def _button_bit(button: int) -> int:
    b = max(1, min(3, int(button)))
    return 1 << (b - 1)


def _sync_mouse_buttons(mask: int, env: dict[str, str]) -> None:
    global _mouse_button_mask
    mask &= 7
    for bit, btn in _MOUSE_BUTTON_BITS:
        was = (_mouse_button_mask & bit) != 0
        now = (mask & bit) != 0
        if now and not was:
            try:
                from webrtc.x11_input import xtest_mousedown
                if xtest_mousedown(btn, env):
                    continue
            except Exception:
                pass
            subprocess.run(
                ["xdotool", "mousedown", str(btn)],
                check=False,
                timeout=2,
                env=env,
            )
        elif was and not now:
            try:
                from webrtc.x11_input import xtest_mouseup
                if xtest_mouseup(btn, env):
                    continue
            except Exception:
                pass
            subprocess.run(
                ["xdotool", "mouseup", str(btn)],
                check=False,
                timeout=2,
                env=env,
            )
    _mouse_button_mask = mask


# Cache warp_pointer at module level to avoid repeated import overhead on every
# move event (import machinery acquires locks, which adds latency under contention).
_warp_pointer_fn = None
_warp_pointer_loaded = False


def _get_warp_pointer():
    global _warp_pointer_fn, _warp_pointer_loaded
    if _warp_pointer_loaded:
        return _warp_pointer_fn
    _warp_pointer_loaded = True
    try:
        from webrtc.x11_input import warp_pointer
        _warp_pointer_fn = warp_pointer
    except Exception:
        _warp_pointer_fn = None
    return _warp_pointer_fn


def _mousemove(x: float, y: float, env: dict[str, str], drag: bool = False) -> None:
    ix, iy = int(x), int(y)
    try:
        from webrtc.x11_input import xtest_mousemove
        if xtest_mousemove(ix, iy, env):
            return
    except Exception:
        pass
    if not drag:
        warp = _get_warp_pointer()
        if warp is not None:
            try:
                if warp(ix, iy, env):
                    return
            except Exception:
                pass
    subprocess.run(
        ["xdotool", "mousemove", str(ix), str(iy)],
        check=False,
        timeout=2,
        env=env,
    )


def _wheel_scroll(dx: int, dy: int, env: dict[str, str]) -> None:
    """Send scroll clicks: X buttons 4/5 vertical, 6/7 horizontal (xdotool)."""
    cap = 40
    dv = max(-cap, min(cap, int(dy)))
    dh = max(-cap, min(cap, int(dx)))
    try:
        from webrtc.x11_input import xtest_click
        xtest_ok = True
        if dv > 0:
            xtest_ok = xtest_ok and xtest_click(5, dv, env)
        elif dv < 0:
            xtest_ok = xtest_ok and xtest_click(4, -dv, env)
        if dh > 0:
            xtest_ok = xtest_ok and xtest_click(7, dh, env)
        elif dh < 0:
            xtest_ok = xtest_ok and xtest_click(6, -dh, env)
        if xtest_ok and (dv != 0 or dh != 0):
            return
    except Exception:
        pass

    if dv > 0:
        subprocess.run(
            ["xdotool", "click", "--repeat", str(dv), "5"],
            check=False,
            timeout=4,
            env=env,
        )
    elif dv < 0:
        subprocess.run(
            ["xdotool", "click", "--repeat", str(-dv), "4"],
            check=False,
            timeout=4,
            env=env,
        )
    if dh > 0:
        subprocess.run(
            ["xdotool", "click", "--repeat", str(dh), "7"],
            check=False,
            timeout=4,
            env=env,
        )
    elif dh < 0:
        subprocess.run(
            ["xdotool", "click", "--repeat", str(-dh), "6"],
            check=False,
            timeout=4,
            env=env,
        )


def _release_mouse_button_if_held(button: int, env: dict[str, str]) -> None:
    """Release one X button when mask is stuck from a lost mouseup."""
    global _mouse_button_mask
    bit = _button_bit(button)
    if _mouse_button_mask & bit:
        try:
            from webrtc.x11_input import xtest_mouseup
            if xtest_mouseup(button, env):
                _mouse_button_mask &= ~bit
                return
        except Exception:
            pass
        subprocess.run(
            ["xdotool", "mouseup", str(button)],
            check=False,
            timeout=2,
            env=env,
        )
        _mouse_button_mask &= ~bit


def _reset_mouse_button_state(env: dict[str, str] | None = None) -> None:
    """Clear tracked mask; optionally release stuck buttons on the X display."""
    global _mouse_button_mask
    if _mouse_button_mask and env is not None:
        _sync_mouse_buttons(0, env)
    else:
        _mouse_button_mask = 0


def _force_release_all_mouse_buttons(env: dict[str, str]) -> None:
    """Release every X mouse button even when the tracked mask is out of sync."""
    global _mouse_button_mask
    for btn in (1, 2, 3):
        try:
            from webrtc.x11_input import xtest_mouseup
            if xtest_mouseup(btn, env):
                continue
        except Exception:
            pass
        subprocess.run(
            ["xdotool", "mouseup", str(btn)],
            check=False,
            timeout=1,
            env=env,
        )
    _mouse_button_mask = 0


def _release_all_modifiers(env: dict[str, str]) -> None:
    """Release all keyboard modifier keys in X11 to prevent stuck key states."""
    cmd = [
        "xdotool",
        "keyup", "Shift_L",
        "keyup", "Shift_R",
        "keyup", "Control_L",
        "keyup", "Control_R",
        "keyup", "Alt_L",
        "keyup", "Alt_R",
        "keyup", "Super_L",
        "keyup", "Super_R",
    ]
    subprocess.run(cmd, check=False, timeout=2, env=env)


def _touch_input_activity() -> None:
    global _last_input_monotonic
    _last_input_monotonic = time.monotonic()


def _input_kind_from_raw(raw: str) -> str:
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    if not isinstance(obj, dict):
        return ""
    return (obj.get("t") or obj.get("type") or "").strip().lower()


def _input_buttons_from_raw(raw: str) -> int:
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return 0
    if not isinstance(obj, dict):
        return 0
    try:
        return int(obj.get("buttons", 0)) & 7
    except (TypeError, ValueError):
        return 0


def _flush_queued_move_events(input_queue: asyncio.Queue[str]) -> None:
    """Drop all queued move/mousemove events; preserve everything else in order."""
    backlog: list[str] = []
    while True:
        try:
            old = input_queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        input_queue.task_done()
        if _input_kind_from_raw(old) in ("move", "mousemove"):
            continue
        backlog.append(old)
    for item in backlog:
        try:
            input_queue.put_nowait(item)
        except asyncio.QueueFull:
            logger.warning("Could not re-queue non-move input after flush")
            break


def _make_room_for_critical_event(input_queue: asyncio.Queue[str]) -> None:
    """Evict queued items until a critical mousedown/mouseup/drag-move can fit.

    Prefer dropping plain hover moves first, then the oldest non-button event.
    Never evict mousedown/mouseup — those must not be starved by paste/key bursts.
    """
    if not input_queue.full():
        return
    _flush_queued_move_events(input_queue)
    if not input_queue.full():
        return
    backlog: list[str] = []
    dropped = False
    while True:
        try:
            old = input_queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        input_queue.task_done()
        kind = _input_kind_from_raw(old)
        if not dropped and kind not in ("mousedown", "mouseup", "click", "wheel"):
            dropped = True
            continue
        backlog.append(old)
    for item in backlog:
        try:
            input_queue.put_nowait(item)
        except asyncio.QueueFull:
            logger.warning("Could not re-queue input after critical eviction")
            break


def _drain_pending_moves(
    input_queue: asyncio.Queue[str], first: str
) -> tuple[str, str | None]:
    """Coalesce a run of queued move events; return (latest_move, next_non_move_or_none)."""
    latest = first
    while True:
        try:
            nxt = input_queue.get_nowait()
        except asyncio.QueueEmpty:
            return latest, None
        input_queue.task_done()
        if _input_kind_from_raw(nxt) in ("move", "mousemove"):
            latest = nxt
            continue
        return latest, nxt


def _enqueue_rtc_input(input_queue: asyncio.Queue[str], raw: str) -> None:
    kind = _input_kind_from_raw(raw)
    is_move = kind in ("move", "mousemove")
    buttons = _input_buttons_from_raw(raw) if is_move else 0
    critical_button = kind in ("mousedown", "mouseup", "click")
    critical_move = is_move and buttons != 0
    critical_wheel = kind == "wheel"

    # Plain hover moves may be dropped when the queue is saturated.
    if is_move and input_queue.full() and not critical_move:
        return

    try:
        input_queue.put_nowait(raw)
        return
    except asyncio.QueueFull:
        pass

    if critical_button or critical_move or critical_wheel:
        _make_room_for_critical_event(input_queue)
        try:
            input_queue.put_nowait(raw)
            return
        except asyncio.QueueFull:
            if critical_button:
                logger.warning("Could not queue critical button event after eviction")
            return

    backlog: list[str] = []
    dropped_move = False
    while True:
        try:
            old = input_queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        input_queue.task_done()
        if not dropped_move and _input_kind_from_raw(old) in ("move", "mousemove"):
            dropped_move = True
            continue
        backlog.append(old)
    for item in backlog:
        try:
            input_queue.put_nowait(item)
        except asyncio.QueueFull:
            break
    try:
        input_queue.put_nowait(raw)
    except asyncio.QueueFull:
        if not is_move:
            logger.debug("input queue full; dropped %s", kind or "message")


def _apply_clipboard_json(raw: str) -> None:
    """Apply clipboard/paste on a background worker so xclip never blocks mouse input."""
    global _last_clipboard_write_monotonic
    _last_clipboard_write_monotonic = time.monotonic()
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return
    if not isinstance(obj, dict):
        return
    t = (obj.get("t") or obj.get("type") or "").strip().lower()
    if t not in ("clipboard", "paste"):
        return
    env = _display_env()
    try:
        text = str(obj.get("text") or "")
        _set_x_clipboard(text, env)
        if t == "paste":
            time.sleep(0.08)
            subprocess.run(["xdotool", "key", "--clearmodifiers", "ctrl+v"], check=False, timeout=2, env=env)
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        logger.debug("clipboard skip: %s", e)


def _apply_input_json(raw: str) -> None:
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return
    if not isinstance(obj, dict):
        return
    t = (obj.get("t") or obj.get("type") or "").strip().lower()
    env = _display_env()
    _touch_input_activity()
    try:
        if t in ("move", "mousemove"):
            x = float(obj.get("x", 0))
            y = float(obj.get("y", 0))
            buttons = int(obj.get("buttons", 0)) if "buttons" in obj else _mouse_button_mask
            if "buttons" in obj:
                _sync_mouse_buttons(buttons, env)
            _mousemove(x, y, env, drag=(buttons != 0))
        elif t in ("mousedown",):
            b = int(obj.get("button", 1))
            if "x" in obj and "y" in obj:
                _mousemove(float(obj.get("x", 0)), float(obj.get("y", 0)), env)
            bit = _button_bit(b)
            # Lost mouseup leaves the X button down; _sync_mouse_buttons skips a
            # repeat press, so clicks stop until agent restart. Release first.
            _release_mouse_button_if_held(b, env)
            mask = int(obj.get("buttons", _mouse_button_mask | bit))
            _sync_mouse_buttons(mask, env)
        elif t in ("mouseup",):
            b = int(obj.get("button", 1))
            if "x" in obj and "y" in obj:
                _mousemove(float(obj.get("x", 0)), float(obj.get("y", 0)), env)
            mask = int(obj.get("buttons", _mouse_button_mask & ~_button_bit(b)))
            _sync_mouse_buttons(mask, env)
        elif t in ("click",):
            _force_release_all_mouse_buttons(env)
            b = int(obj.get("button", 1))
            ix, iy = int(float(obj.get("x", 0))), int(float(obj.get("y", 0)))
            _mousemove(float(ix), float(iy), env)
            try:
                from webrtc.x11_input import xtest_click
                if xtest_click(b, 1, env):
                    return
            except Exception:
                pass
            subprocess.run(
                ["xdotool", "click", str(b)],
                check=False,
                timeout=2,
                env=env,
            )
        elif t in ("wheel",):
            x = float(obj.get("x", 0))
            y = float(obj.get("y", 0))
            _mousemove(x, y, env)
            _wheel_scroll(int(obj.get("dx", 0)), int(obj.get("dy", 0)), env)
        elif t in ("key",):
            text = str(obj.get("key", ""))[:64]
            if text:
                subprocess.run(["xdotool", "type", "--delay", "5", text], check=False, timeout=5, env=env)
        elif t in ("clipboard", "paste"):
            return
        elif t in ("keydown", "keyup"):
            key_name = _xdotool_key(obj)
            if not key_name:
                return
            is_press = (t == "keydown")
            try:
                from webrtc.x11_input import xtest_keyevent
                if xtest_keyevent(key_name, is_press, env):
                    return
            except Exception:
                pass
            if t == "keydown" and len(str(obj.get("key") or "")) == 1 and not any(
                bool(obj.get(k)) for k in ("ctrlKey", "altKey", "metaKey")
            ):
                subprocess.run(["xdotool", "type", "--delay", "5", str(obj.get("key"))], check=False, timeout=5, env=env)
                return
            action = "keydown" if t == "keydown" else "keyup"
            subprocess.run(["xdotool", action, "--", key_name], check=False, timeout=2, env=env)
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        logger.debug("input skip: %s", e)


def _build_rtc_configuration():  # type: ignore[no-untyped-def]
    from aiortc.rtcconfiguration import RTCConfiguration, RTCIceServer

    sys.path.insert(0, "/axonos_gate")
    try:
        from webrtc import config as wcfg

        specs = wcfg.ice_servers_for_client()
    except Exception:
        specs = [{"urls": "stun:stun.l.google.com:19302"}]
    servers = []
    for s in specs:
        if not isinstance(s, dict):
            continue
        urls = s.get("urls")
        if not urls:
            continue
        kwargs = {"urls": urls}
        if s.get("username"):
            kwargs["username"] = s["username"]
        if s.get("credential"):
            kwargs["credential"] = s["credential"]
        servers.append(RTCIceServer(**kwargs))
    if not servers:
        servers.append(RTCIceServer(urls="stun:stun.l.google.com:19302"))
    return RTCConfiguration(iceServers=servers)


def _agent_fail(session_id: str, error: str) -> None:
    key = _agent_key()
    gate = _gate_url()
    try:
        import urllib.request

        data = json.dumps({"session_id": session_id, "error": error}).encode("utf-8")
        req = urllib.request.Request(
            f"{gate}/api/webrtc/agent/fail",
            data=data,
            headers={"Content-Type": "application/json", _AXT: key},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        logger.warning("agent fail report: %s", e)


async def _run_session(job: dict[str, Any]) -> None:
    try:
        from aiortc import RTCIceCandidate, RTCPeerConnection, RTCSessionDescription
    except ImportError as e:
        logger.error("missing aiortc/av: %s", e)
        _agent_fail(job.get("session_id", ""), "missing_aiortc")
        return

    import aiohttp

    sys.path.insert(0, "/axonos_gate")
    from webrtc.capture import (
        _video_codec_label_from_sdp,
        open_capture,
        prefer_h264_for_pc,
    )

    session_id = job["session_id"]
    if not _ensure_display_ready():
        _agent_fail(session_id, "display_not_ready")
        return

    # Each WebRTC session gets a clean X mouse-button mask (agent process is long-lived).
    _reset_mouse_button_state()

    offer_sdp = job["offer_sdp"]
    offer_type = (job.get("offer_type") or "offer").lower()

    pc = RTCPeerConnection(_build_rtc_configuration())
    capture_handle = None
    key = _agent_key()
    gate = _gate_url()
    applied_ice: set[str] = set()
    clipboard_env = _display_env()
    _release_all_modifiers(clipboard_env)
    clip_channel_out: list[Any] = [None]
    input_channel_out: list[Any] = [None]
    last_clipboard = ""
    clipboard_queue: asyncio.Queue[str] | None = None
    clipboard_worker_task: asyncio.Task | None = None
    clipboard_poll_task: asyncio.Task | None = None
    cursor_poll_task: asyncio.Task | None = None
    input_worker_task: asyncio.Task | None = None
    mouse_watchdog_task: asyncio.Task | None = None
    input_queue: asyncio.Queue[str] | None = None
    session_tasks_started = False

    def _enqueue_client_clipboard(raw: str) -> None:
        if clipboard_queue is None:
            return
        if len(raw) > 600_000:
            logger.debug("clipboard message too large (%s bytes); dropped", len(raw))
            return
        try:
            clipboard_queue.put_nowait(raw)
        except asyncio.QueueFull:
            try:
                clipboard_queue.get_nowait()
                clipboard_queue.task_done()
            except asyncio.QueueEmpty:
                pass
            try:
                clipboard_queue.put_nowait(raw)
            except asyncio.QueueFull:
                logger.debug("clipboard queue full; dropped inbound")

    def _ensure_clipboard_worker() -> None:
        nonlocal clipboard_queue, clipboard_worker_task
        if clipboard_worker_task is not None:
            return
        clipboard_queue = asyncio.Queue(maxsize=32)

        async def clipboard_worker() -> None:
            nonlocal last_clipboard
            assert clipboard_queue is not None
            while True:
                msg = await clipboard_queue.get()
                try:
                    # Sync incoming text to last_clipboard to prevent echo-back loop
                    try:
                        obj = json.loads(msg)
                        if isinstance(obj, dict):
                            t = (obj.get("t") or obj.get("type") or "").strip().lower()
                            if t in ("clipboard", "paste"):
                                text = str(obj.get("text") or "")
                                if text:
                                    last_clipboard = text
                    except Exception:
                        pass
                    await asyncio.to_thread(_apply_clipboard_json, msg)
                except Exception as e:
                    logger.debug("clipboard worker: %s", e)
                finally:
                    clipboard_queue.task_done()

        clipboard_worker_task = asyncio.create_task(clipboard_worker())

    async def poll_remote_clipboard() -> None:
        nonlocal last_clipboard
        while pc.connectionState not in ("failed", "closed"):
            # Skip polling when there is active user interaction to avoid X11 server contention
            idle_s = time.monotonic() - _last_input_monotonic
            if idle_s < 1.0:
                await asyncio.sleep(0.5)
                continue

            # Skip polling if the agent recently wrote to the clipboard (to avoid stale X11 read race)
            write_elapsed = time.monotonic() - _last_clipboard_write_monotonic
            if write_elapsed < 2.0:
                await asyncio.sleep(0.5)
                continue

            try:
                text = await asyncio.to_thread(_get_x_clipboard_for_browser_poll, clipboard_env)
                if text and text != last_clipboard:
                    last_clipboard = text
                    outbound = clip_channel_out[0] or input_channel_out[0]
                    if outbound is not None:
                        try:
                            outbound.send(json.dumps({"t": "clipboard", "text": text}))
                        except Exception as e:
                            logger.debug("clipboard poll send: %s", e)
            except Exception as e:
                logger.debug("clipboard poll: %s", e)
            await asyncio.sleep(1.0)

    async def poll_mouse_button_watchdog() -> None:
        """Release stuck buttons when mouseup events were lost during congestion."""
        while pc.connectionState not in ("failed", "closed"):
            await asyncio.sleep(5.0)
            if not _mouse_button_mask:
                continue
            idle_s = time.monotonic() - _last_input_monotonic
            if idle_s < 3.0:
                continue
            logger.info(
                "releasing stale mouse buttons after %.1fs idle (mask=%s)",
                idle_s,
                _mouse_button_mask,
            )
            await asyncio.to_thread(_reset_mouse_button_state, clipboard_env)

    last_cursor_serial = 0

    async def poll_cursor_shape() -> None:
        nonlocal last_cursor_serial
        await asyncio.sleep(0.5)
        try:
            from webrtc.x11_input import get_cursor_info
        except ImportError:
            return

        while pc.connectionState not in ("failed", "closed"):
            try:
                res = await asyncio.to_thread(get_cursor_info, clipboard_env, last_cursor_serial)
                if res is not None:
                    if res.get("changed"):
                        last_cursor_serial = res["serial"]
                        outbound = input_channel_out[0]
                        if outbound is not None and outbound.readyState == "open":
                            try:
                                outbound.send(json.dumps({
                                    "t": "cursor",
                                    "name": res.get("name"),
                                    "width": res.get("width"),
                                    "height": res.get("height"),
                                    "xhot": res.get("xhot"),
                                    "yhot": res.get("yhot"),
                                    "img": res.get("img")
                                }))
                            except Exception:
                                pass
                    else:
                        last_cursor_serial = res["serial"]
            except Exception as e:
                logger.debug("cursor poll: %s", e)
            await asyncio.sleep(0.12)

    def _start_session_io_tasks() -> None:
        nonlocal clipboard_poll_task, mouse_watchdog_task, cursor_poll_task, session_tasks_started
        if session_tasks_started:
            return
        session_tasks_started = True
        _ensure_clipboard_worker()
        clipboard_poll_task = asyncio.create_task(poll_remote_clipboard())
        mouse_watchdog_task = asyncio.create_task(poll_mouse_button_watchdog())
        cursor_poll_task = asyncio.create_task(poll_cursor_shape())

    def _cancel_session_io_tasks() -> None:
        if clipboard_poll_task is not None:
            clipboard_poll_task.cancel()
        if mouse_watchdog_task is not None:
            mouse_watchdog_task.cancel()
        if cursor_poll_task is not None:
            cursor_poll_task.cancel()
        if clipboard_worker_task is not None:
            clipboard_worker_task.cancel()
        if input_worker_task is not None:
            input_worker_task.cancel()
        _reset_mouse_button_state(clipboard_env)
        _release_all_modifiers(clipboard_env)

    def _ensure_input_worker() -> asyncio.Queue[str]:
        nonlocal input_worker_task, input_queue
        if input_queue is not None:
            return input_queue
        input_queue = asyncio.Queue(maxsize=96)
        loop = asyncio.get_running_loop()

        async def input_worker() -> None:
            assert input_queue is not None
            pending: str | None = None
            while True:
                msg = pending if pending is not None else await input_queue.get()
                pending = None
                try:
                    kind = _input_kind_from_raw(msg)
                    if kind in ("move", "mousemove"):
                        latest, pending = _drain_pending_moves(input_queue, msg)
                        # Run ALL input in the dedicated thread pool so the
                        # asyncio event loop is never blocked.  A blocked loop
                        # starves aiortc SCTP acks and eventually kills the
                        # data channels — the root cause of input dying.
                        await loop.run_in_executor(_input_executor, _apply_input_json, latest)
                        if pending is None:
                            continue
                        msg = pending
                        pending = None
                        kind = _input_kind_from_raw(msg)
                    if kind == "ping":
                        # Health check from the client — echo back immediately.
                        outbound = input_channel_out[0]
                        if outbound is not None:
                            try:
                                outbound.send('{"t":"pong"}')
                            except Exception:
                                pass
                        continue
                    await loop.run_in_executor(_input_executor, _apply_input_json, msg)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.debug("input worker: %s", e)
                finally:
                    input_queue.task_done()

        async def input_worker_guarded() -> None:
            """Restart the inner worker on crash so input can recover."""
            while True:
                try:
                    await input_worker()
                except asyncio.CancelledError:
                    return
                except Exception:
                    logger.exception("input worker crashed — restarting in 0.5s")
                    await asyncio.sleep(0.5)

        input_worker_task = asyncio.create_task(input_worker_guarded())
        return input_queue

    def _bind_input_channel(channel: Any, *, moves_only: bool) -> None:
        queue = _ensure_input_worker()

        @channel.on("message")
        def on_msg(message) -> None:  # type: ignore[no-untyped-def]
            if not isinstance(message, str):
                return
            kind = _input_kind_from_raw(message)
            if moves_only:
                if kind not in ("move", "mousemove"):
                    return
                _enqueue_rtc_input(queue, message)
                return
            if kind in ("clipboard", "paste"):
                _enqueue_client_clipboard(message)
                return
            _enqueue_rtc_input(queue, message)

        if not moves_only:

            @channel.on("close")
            def on_close() -> None:  # type: ignore[no-untyped-def]
                _cancel_session_io_tasks()

    @pc.on("datachannel")
    def on_dc(channel) -> None:  # type: ignore[no-untyped-def]
        if channel.label == "axonos-clipboard":
            clip_channel_out[0] = channel
            _start_session_io_tasks()

            @channel.on("message")
            def on_clip_msg(message) -> None:  # type: ignore[no-untyped-def]
                if isinstance(message, str):
                    _enqueue_client_clipboard(message)

            return

        if channel.label == "axonos-input-moves":
            _reset_mouse_button_state(clipboard_env)
            _start_session_io_tasks()
            _bind_input_channel(channel, moves_only=True)
            return

        if channel.label != "axonos-input":
            return

        input_channel_out[0] = channel
        _reset_mouse_button_state(clipboard_env)
        _start_session_io_tasks()
        _bind_input_channel(channel, moves_only=False)

    try:
        capture_handle = open_capture(
            session_id=session_id,
            display=_display(),
            env=clipboard_env,
        )
        logger.info(
            "WebRTC capture backend=%s session=%s",
            capture_handle.backend,
            session_id[:16],
        )
        pc.addTrack(capture_handle.track)
        prefer_h264_for_pc(pc, capture_handle.backend, offer_sdp)
    except Exception:
        logger.exception("WebRTC capture setup failed session=%s", session_id[:16])
        await pc.close()
        _agent_fail(session_id, "capture_setup_failed")
        return

    await pc.setRemoteDescription(RTCSessionDescription(sdp=offer_sdp, type=offer_type))

    async def poll_client_ice() -> None:
        async with aiohttp.ClientSession() as session:
            url = f"{gate}/api/webrtc/agent/row"
            while pc.connectionState not in ("failed", "closed", "connected", "completed"):
                try:
                    async with session.get(
                        url,
                        headers={_AXT: key},
                        params={"session_id": session_id},
                        timeout=aiohttp.ClientTimeout(total=6),
                    ) as resp:
                        if resp.status != 200:
                            await asyncio.sleep(0.15)
                            continue
                        data = await resp.json()
                        for c in data.get("client_ice") or []:
                            if not isinstance(c, dict):
                                continue
                            cand = c.get("candidate")
                            if not cand:
                                continue
                            # Skip mDNS-obfuscated candidates (Chrome .local hostnames):
                            # the server cannot resolve them, they only waste ICE slots.
                            if ".local " in cand:
                                continue
                            sig = f"{c.get('sdpMid')}|{c.get('sdpMLineIndex')}|{cand}"
                            if sig in applied_ice:
                                continue
                            applied_ice.add(sig)
                            try:
                                ice = RTCIceCandidate(
                                    sdpMid=c.get("sdpMid"),
                                    sdpMLineIndex=c.get("sdpMLineIndex"),
                                    candidate=cand,
                                )
                                await pc.addIceCandidate(ice)
                            except Exception as e:
                                logger.debug("addIceCandidate: %s", e)
                except Exception as e:
                    logger.debug("poll ice: %s", e)
                await asyncio.sleep(0.12)

    ice_task = asyncio.create_task(poll_client_ice())
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)
    if capture_handle.backend == "nvenc" and answer.sdp:
        logger.info(
            "WebRTC answer video codec=%s session=%s",
            _video_codec_label_from_sdp(answer.sdp),
            session_id[:16],
        )

    done = asyncio.Event()

    @pc.on("icegatheringstatechange")
    def on_ice_state() -> None:
        if pc.iceGatheringState == "complete":
            done.set()

    try:
        await asyncio.wait_for(done.wait(), timeout=8.0)
    except asyncio.TimeoutError:
        logger.warning("ICE gathering timeout; continuing with partial SDP")

    sdp_local = pc.localDescription
    if sdp_local is None:
        ice_task.cancel()
        try:
            capture_handle.cleanup()
        except Exception:
            pass
        await pc.close()
        _agent_fail(session_id, "no_local_description")
        return

    payload = {"session_id": session_id, "sdp": _normalize_sdp(sdp_local.sdp), "type": sdp_local.type}
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{gate}/api/webrtc/agent/answer",
            headers={_AXT: key, "Content-Type": "application/json"},
            json=payload,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                logger.error("answer POST failed: %s %s", resp.status, (await resp.text())[:400])
                ice_task.cancel()
                try:
                    capture_handle.cleanup()
                except Exception:
                    pass
                await pc.close()
                _agent_fail(session_id, "answer_post_failed")
                return

    logger.info("WebRTC answer stored session=%s", session_id[:16])

    try:
        while pc.connectionState not in ("failed", "closed"):
            await asyncio.sleep(0.5)
    finally:
        ice_task.cancel()
        if capture_handle is not None:
            try:
                capture_handle.cleanup()
            except Exception:
                pass
        try:
            await pc.close()
        except Exception:
            pass


def _http_get_job(url: str, headers: dict[str, str]) -> tuple[int, dict[str, Any] | None]:
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            if resp.status == 204:
                return 204, None
            body = resp.read().decode("utf-8", errors="ignore")
            return resp.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="ignore")
        except Exception:
            body = ""
        if e.code == 204:
            return 204, None
        logger.debug("GET error %s %s", e.code, body[:200])
        return e.code, None
    except urllib.error.URLError as e:
        # Common on startup: axgt-api sleeps 4s before gate_server binds 8889 — connection refused
        # must not kill the agent process or supervisord will flap until the gate is up.
        logger.debug("agent/next unreachable: %s", getattr(e, "reason", e) or e)
        return -1, None


async def main_loop() -> None:
    if not _agent_key():
        logger.error("WEBRTC_AGENT_INTERNAL_KEY unset")
        while True:
            await asyncio.sleep(3600)

    gate = _gate_url()
    poll_url = f"{gate}/api/webrtc/agent/next"
    logger.info("WebRTC agent polling gate at %s", poll_url)

    if (os.getenv("AXGT_SESSION_ID") or "").strip():

        async def _prewarm_display() -> None:
            logger.info("session container: pre-warming display in background")
            warmed = await asyncio.to_thread(_ensure_display_ready)
            if not warmed:
                logger.warning("session container: display pre-warm incomplete; will retry when offer arrives")

        asyncio.create_task(_prewarm_display())

    while True:
        if not _truthy("WEBRTC_ENABLED"):
            await asyncio.sleep(5)
            continue
        status, job = _http_get_job(poll_url, {_AXT: _agent_key()})
        if status == -1:
            await asyncio.sleep(0.75)
            continue
        if status == 204 or job is None:
            await asyncio.sleep(0.35)
            continue
        if status != 200 or not job.get("session_id"):
            await asyncio.sleep(1.0)
            continue
        try:
            await _run_session(job)
        except Exception as e:
            logger.exception("session error")
            detail = f"exception:{type(e).__name__}:{str(e)[:500]}"
            _agent_fail(str(job.get("session_id", "")), detail)


if __name__ == "__main__":
    if not _truthy("WEBRTC_ENABLED"):
        logger.info("WEBRTC_ENABLED off; exiting")
        sys.exit(0)
    if not _agent_key():
        logger.warning("WEBRTC_AGENT_INTERNAL_KEY unset; sleep")
        time.sleep(999999)
        sys.exit(0)
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        sys.exit(0)
