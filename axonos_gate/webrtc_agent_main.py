#!/usr/bin/env python3
"""In-container WebRTC streaming agent: claims SDP offers from the gate, answers with desktop video.

Environment:
  WEBRTC_ENABLED=true
  WEBRTC_AGENT_INTERNAL_KEY — shared secret with the gate (required)
  WEBRTC_GATE_INTERNAL_URL — gate base URL (default http://127.0.0.1:8889)
  WEBRTC_CAPTURE_DISPLAY — X display (default :0)
  WEBRTC_CAPTURE_MAX_WIDTH — scale bound (default 1920; matches the current session display)
  WEBRTC_CAPTURE_FPS — target FPS (default 15)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from fractions import Fraction
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
_MOUSE_BUTTON_BITS = ((1, 1), (2, 2), (4, 3))


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


def _max_width() -> int:
    raw = (os.getenv("WEBRTC_CAPTURE_MAX_WIDTH") or "1920").strip()
    try:
        return max(320, min(3840, int(raw)))
    except ValueError:
        return 1920


def _fps() -> float:
    raw = (os.getenv("WEBRTC_CAPTURE_FPS") or "15").strip()
    try:
        return float(raw)
    except ValueError:
        return 15.0


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


def _reap_xclip_popen(proc: subprocess.Popen[bytes] | None) -> None:
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
    data = text.encode("utf-8", errors="ignore")
    ok = False
    for selection in ("clipboard", "primary"):
        old = _clipboard_owners.pop(selection, None)
        _reap_xclip_popen(old)
        try:
            p = subprocess.Popen(
                ["xclip", "-selection", selection],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
            )
            if p.stdin:
                p.stdin.write(data)
                p.stdin.close()
            _clipboard_owners[selection] = p
            ok = True
        except (BrokenPipeError, OSError):
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
            subprocess.run(
                ["xdotool", "mousedown", str(btn)],
                check=False,
                timeout=2,
                env=env,
            )
        elif was and not now:
            subprocess.run(
                ["xdotool", "mouseup", str(btn)],
                check=False,
                timeout=2,
                env=env,
            )
    _mouse_button_mask = mask


def _mousemove(x: float, y: float, env: dict[str, str]) -> None:
    subprocess.run(
        ["xdotool", "mousemove", str(int(x)), str(int(y))],
        check=False,
        timeout=2,
        env=env,
    )


def _reset_mouse_button_state(env: dict[str, str] | None = None) -> None:
    """Clear tracked mask; optionally release stuck buttons on the X display."""
    global _mouse_button_mask
    if _mouse_button_mask and env is not None:
        _sync_mouse_buttons(0, env)
    else:
        _mouse_button_mask = 0


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


def _enqueue_rtc_input(input_queue: asyncio.Queue[str], raw: str) -> None:
    kind = _input_kind_from_raw(raw)
    is_move = kind in ("move", "mousemove")
    buttons = _input_buttons_from_raw(raw) if is_move else 0
    critical_button = kind in ("mousedown", "mouseup")
    critical_move = is_move and buttons != 0

    # Plain hover moves may be dropped when the queue is saturated.
    if is_move and input_queue.full() and not critical_move:
        return

    try:
        input_queue.put_nowait(raw)
        return
    except asyncio.QueueFull:
        pass

    if critical_button or critical_move:
        _flush_queued_move_events(input_queue)
        try:
            input_queue.put_nowait(raw)
            return
        except asyncio.QueueFull:
            if critical_button:
                logger.warning("Could not queue critical button event")
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


def _apply_input_json(raw: str) -> None:
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return
    if not isinstance(obj, dict):
        return
    t = (obj.get("t") or obj.get("type") or "").strip().lower()
    env = _display_env()
    try:
        if t in ("move", "mousemove"):
            x = float(obj.get("x", 0))
            y = float(obj.get("y", 0))
            if "buttons" in obj:
                _sync_mouse_buttons(int(obj.get("buttons", 0)), env)
            _mousemove(x, y, env)
        elif t in ("mousedown",):
            b = int(obj.get("button", 1))
            if "x" in obj and "y" in obj:
                _mousemove(float(obj.get("x", 0)), float(obj.get("y", 0)), env)
            mask = int(obj.get("buttons", _mouse_button_mask | _button_bit(b)))
            _sync_mouse_buttons(mask, env)
        elif t in ("mouseup",):
            b = int(obj.get("button", 1))
            if "x" in obj and "y" in obj:
                _mousemove(float(obj.get("x", 0)), float(obj.get("y", 0)), env)
            mask = int(obj.get("buttons", _mouse_button_mask & ~_button_bit(b)))
            _sync_mouse_buttons(mask, env)
        elif t in ("click",):
            _sync_mouse_buttons(0, env)
            b = int(obj.get("button", 1))
            if "x" in obj and "y" in obj:
                _mousemove(float(obj.get("x", 0)), float(obj.get("y", 0)), env)
            subprocess.run(["xdotool", "click", str(b)], check=False, timeout=2, env=env)
        elif t in ("key",):
            text = str(obj.get("key", ""))[:64]
            if text:
                subprocess.run(["xdotool", "type", "--delay", "5", text], check=False, timeout=5, env=env)
        elif t in ("clipboard", "paste"):
            text = str(obj.get("text") or "")
            _set_x_clipboard(text, env)
            if t == "paste":
                time.sleep(0.08)
                subprocess.run(["xdotool", "key", "--clearmodifiers", "ctrl+v"], check=False, timeout=2, env=env)
        elif t in ("keydown", "keyup"):
            key_name = _xdotool_key(obj)
            if not key_name:
                return
            if t == "keydown" and len(str(obj.get("key") or "")) == 1 and not any(
                bool(obj.get(k)) for k in ("ctrlKey", "altKey", "metaKey")
            ):
                subprocess.run(["xdotool", "type", "--delay", "5", str(obj.get("key"))], check=False, timeout=5, env=env)
                return
            action = "keydown" if t == "keydown" else "keyup"
            subprocess.run(["xdotool", action, key_name], check=False, timeout=2, env=env)
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
        from aiortc import RTCIceCandidate, RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
        from av import VideoFrame
    except ImportError as e:
        logger.error("missing aiortc/av: %s", e)
        _agent_fail(job.get("session_id", ""), "missing_aiortc")
        return

    import aiohttp
    import mss
    import numpy as np

    session_id = job["session_id"]
    if not _ensure_display_ready():
        _agent_fail(session_id, "display_not_ready")
        return

    offer_sdp = job["offer_sdp"]
    offer_type = (job.get("offer_type") or "offer").lower()
    max_w = _max_width()
    target_fps = max(5.0, min(60.0, _fps()))
    interval = 1.0 / target_fps

    pc = RTCPeerConnection(_build_rtc_configuration())
    key = _agent_key()
    gate = _gate_url()
    applied_ice: set[str] = set()

    @pc.on("datachannel")
    def on_dc(channel) -> None:  # type: ignore[no-untyped-def]
        if channel.label != "axonos-input":
            return
        clipboard_env = _display_env()
        _reset_mouse_button_state(clipboard_env)
        last_clipboard = ""

        async def poll_remote_clipboard() -> None:
            nonlocal last_clipboard
            while pc.connectionState not in ("failed", "closed"):
                try:
                    # Use CLIPBOARD only here. `_get_x_clipboard` also reads PRIMARY;
                    # on Xfce the desktop icon label (e.g. "New File") lives in PRIMARY
                    # when the icon is selected. Pushing that to the browser runs
                    # `navigator.clipboard.writeText`, which stomps the host OS
                    # clipboard so `readText()` + host→remote sync paste "New File"
                    # instead of what the user copied on the host. Explicit remote
                    # copies still hit CLIPBOARD; PRIMARY-only apps can set
                    # WEBRTC_CLIPBOARD_POLL_PRIMARY=1 to restore the old behavior.
                    text = await asyncio.to_thread(_get_x_clipboard_for_browser_poll, clipboard_env)
                    if text and text != last_clipboard:
                        last_clipboard = text
                        channel.send(json.dumps({"t": "clipboard", "text": text}))
                except Exception as e:
                    logger.debug("clipboard poll: %s", e)
                await asyncio.sleep(1.0)

        clipboard_task = asyncio.create_task(poll_remote_clipboard())

        # Single-consumer queue keeps the data-channel callback non-blocking while
        # preserving message order. Without this, a blocking xdotool/xclip call in
        # `_apply_input_json` (notably the 80 ms sleep + ctrl+v injection on paste)
        # stalls the event loop and freezes subsequent mouse clicks.
        input_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=512)

        async def input_worker() -> None:
            while True:
                msg = await input_queue.get()
                try:
                    await asyncio.to_thread(_apply_input_json, msg)
                except Exception as e:
                    logger.debug("input worker: %s", e)
                finally:
                    input_queue.task_done()

        input_task = asyncio.create_task(input_worker())

        @channel.on("message")
        def on_msg(message) -> None:  # type: ignore[no-untyped-def]
            if not isinstance(message, str):
                return
            _enqueue_rtc_input(input_queue, message)

        @channel.on("close")
        def on_close() -> None:
            clipboard_task.cancel()
            input_task.cancel()
            _reset_mouse_button_state(clipboard_env)

    try:
        from PIL import Image  # type: ignore[import-untyped]
    except ImportError:
        Image = None  # type: ignore[assignment, misc]

    class ScreenVideoTrack(VideoStreamTrack):  # type: ignore[misc, valid-type]
        kind = "video"

        def __init__(self) -> None:
            super().__init__()
            self._sct = mss.mss()
            self._mon = self._sct.monitors[1] if len(self._sct.monitors) > 1 else self._sct.monitors[0]
            self._last = 0.0
            self._pts = 0
            self._pts_step = max(1, int(90_000 / target_fps))
            self._frames = 0

        async def recv(self) -> VideoFrame:  # type: ignore[override]
            try:
                now = time.monotonic()
                wait = interval - (now - self._last)
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last = time.monotonic()
                shot = self._sct.grab(self._mon)
                arr = np.array(shot)[:, :, :3].copy()
                h, w = arr.shape[:2]
                if w > max_w and Image is not None:
                    nh = max(1, int(h * max_w / float(w)))
                    rgb = arr[:, :, ::-1]
                    im = Image.fromarray(rgb)
                    try:
                        im = im.resize((max_w, nh), Image.Resampling.LANCZOS)  # Pillow 9+
                    except AttributeError:
                        im = im.resize((max_w, nh), Image.LANCZOS)
                    rgb = np.asarray(im)
                elif w > max_w:
                    step = w / max_w
                    idx = (np.arange(max_w) * step).astype(int)
                    arr = arr[:, idx, :]
                    rgb = arr[:, :, ::-1]
                else:
                    rgb = arr[:, :, ::-1]
                vf = VideoFrame.from_ndarray(rgb, format="rgb24")
                vf.pts = self._pts
                vf.time_base = Fraction(1, 90_000)
                self._pts += self._pts_step
                self._frames += 1
                if self._frames == 1 or self._frames % 150 == 0:
                    logger.info("WebRTC captured frame session=%s size=%sx%s frames=%s", session_id[:16], w, h, self._frames)
                return vf
            except Exception:
                logger.exception("WebRTC frame capture failed session=%s", session_id[:16])
                raise

    pc.addTrack(ScreenVideoTrack())
    await pc.setRemoteDescription(RTCSessionDescription(sdp=offer_sdp, type=offer_type))

    async def poll_client_ice() -> None:
        async with aiohttp.ClientSession() as session:
            url = f"{gate}/api/webrtc/agent/row"
            while pc.connectionState not in ("failed", "closed"):
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

    done = asyncio.Event()

    @pc.on("icegatheringstatechange")
    def on_ice_state() -> None:
        if pc.iceGatheringState == "complete":
            done.set()

    try:
        await asyncio.wait_for(done.wait(), timeout=25.0)
    except asyncio.TimeoutError:
        logger.warning("ICE gathering timeout; continuing with partial SDP")

    sdp_local = pc.localDescription
    if sdp_local is None:
        ice_task.cancel()
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
                await pc.close()
                _agent_fail(session_id, "answer_post_failed")
                return

    logger.info("WebRTC answer stored session=%s", session_id[:16])

    try:
        while pc.connectionState not in ("failed", "closed"):
            await asyncio.sleep(0.5)
    finally:
        ice_task.cancel()
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
