#!/usr/bin/env python3
"""In-container WebRTC streaming agent: claims SDP offers from the gate, answers with desktop video.

Environment:
  WEBRTC_ENABLED=true
  WEBRTC_AGENT_INTERNAL_KEY — shared secret with the gate (required)
  WEBRTC_GATE_INTERNAL_URL — gate base URL (default http://127.0.0.1:8889)
  WEBRTC_CAPTURE_DISPLAY — X display (default :0)
  WEBRTC_CAPTURE_MAX_WIDTH — scale bound (default 1280)
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
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("axonos.webrtc_agent")

_AXT = "X-AxonOS-WebRTC-Agent-Key"


def _truthy(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in ("1", "true", "yes", "on")


def _gate_url() -> str:
    return (os.getenv("WEBRTC_GATE_INTERNAL_URL") or "http://127.0.0.1:8889").rstrip("/")


def _agent_key() -> str:
    return (os.getenv("WEBRTC_AGENT_INTERNAL_KEY") or "").strip()


def _display() -> str:
    return (os.getenv("WEBRTC_CAPTURE_DISPLAY") or ":0").strip()


def _max_width() -> int:
    raw = (os.getenv("WEBRTC_CAPTURE_MAX_WIDTH") or "1280").strip()
    try:
        return max(320, min(3840, int(raw)))
    except ValueError:
        return 1280


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
            subprocess.run(["xdotool", "click", str(b)], check=False, timeout=2, env=env)
        elif t in ("keydown", "key"):
            text = str(obj.get("key", ""))[:64]
            if text:
                subprocess.run(["xdotool", "type", "--delay", "5", text], check=False, timeout=5, env=env)
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

        @channel.on("message")
        def on_msg(message) -> None:  # type: ignore[no-untyped-def]
            if isinstance(message, str):
                _apply_input_json(message)

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

        async def recv(self) -> VideoFrame:  # type: ignore[override]
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
            vf.pts = int(time.time() * 90_000)
            vf.time_base = 1 / 90_000
            return vf

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
