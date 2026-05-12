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


def _truthy(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in ("1", "true", "yes", "on")


def _gate_url() -> str:
    return (os.getenv("WEBRTC_GATE_INTERNAL_URL") or "http://127.0.0.1:8889").rstrip("/")


def _agent_key() -> str:
    return (os.getenv("WEBRTC_AGENT_INTERNAL_KEY") or "").strip()


def _display() -> str:
    return (os.getenv("WEBRTC_CAPTURE_DISPLAY") or ":0").strip()


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


def _set_x_clipboard(text: str, env: dict[str, str]) -> bool:
    data = text.encode("utf-8", errors="ignore")
    ok = False
    for selection in ("clipboard", "primary"):
        old = _clipboard_owners.pop(selection, None)
        if old is not None:
            try:
                if old.poll() is None:
                    old.terminate()
                # Reap so the previous xclip doesn't linger as a zombie. Over a long
                # session, accumulated zombies starve PIDs/FDs and make subsequent
                # xdotool/xclip calls fail, which manifests as input "freezing".
                old.wait(timeout=0.2)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    old.kill()
                    old.wait(timeout=0.2)
                except (subprocess.TimeoutExpired, OSError):
                    pass
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
    # Some apps (e.g. xterm, certain GTK menus, vim) only populate PRIMARY on
    # right-click → Copy / text-selection, while Ctrl+C always lands in
    # CLIPBOARD. Check both and prefer CLIPBOARD when it has content so
    # right-click copy in the remote desktop also propagates back to the host.
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


def _apply_input_json(raw: str) -> None:
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return
    if not isinstance(obj, dict):
        return
    t = (obj.get("t") or obj.get("type") or "").strip().lower()
    env = {**os.environ, "DISPLAY": _display()}
    try:
        if t in ("move", "mousemove"):
            x = float(obj.get("x", 0))
            y = float(obj.get("y", 0))
            subprocess.run(["xdotool", "mousemove", str(int(x)), str(int(y))], check=False, timeout=2, env=env)
        elif t in ("click",):
            b = int(obj.get("button", 1))
            if "x" in obj and "y" in obj:
                x = float(obj.get("x", 0))
                y = float(obj.get("y", 0))
                subprocess.run(["xdotool", "mousemove", str(int(x)), str(int(y))], check=False, timeout=2, env=env)
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
        clipboard_env = {**os.environ, "DISPLAY": _display()}
        last_clipboard = ""

        async def poll_remote_clipboard() -> None:
            nonlocal last_clipboard
            while pc.connectionState not in ("failed", "closed"):
                try:
                    text = await asyncio.to_thread(_get_x_clipboard, clipboard_env)
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
            try:
                input_queue.put_nowait(message)
            except asyncio.QueueFull:
                # Drop oldest mouse-move-like messages to keep the loop responsive
                # under bursts. We deliberately do not drop clicks or paste/key
                # events when the queue is full; they are rare and latency-sensitive.
                try:
                    input_queue.get_nowait()
                    input_queue.task_done()
                    input_queue.put_nowait(message)
                except asyncio.QueueEmpty:
                    pass

        @channel.on("close")
        def on_close() -> None:
            clipboard_task.cancel()
            input_task.cancel()

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
