"""WebRTC desktop video capture: MSS (CPU/VP8) or FFmpeg NVENC (H.264)."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Callable

logger = logging.getLogger("axonos.webrtc_agent.capture")


def capture_backend_name() -> str:
    raw = (os.getenv("WEBRTC_CAPTURE_BACKEND") or "auto").strip().lower()
    if raw in ("mss", "cpu", "software"):
        return "mss"
    if raw in ("nvfbc", "fbc", "capture-sdk"):
        return "nvfbc"
    if raw in ("nvenc", "h264", "gpu"):
        return "nvenc"
    return "auto"


def capture_bitrate_bps() -> int:
    raw = (os.getenv("WEBRTC_CAPTURE_BITRATE") or "12000000").strip()
    try:
        n = int(raw)
    except ValueError:
        n = 12_000_000
    return max(1_000_000, min(30_000_000, n))


def capture_nvenc_low_latency() -> bool:
    raw = (os.getenv("WEBRTC_CAPTURE_LOW_LATENCY") or "true").strip().lower()
    return raw in ("1", "true", "yes", "on")


def capture_nvenc_preset() -> str:
    return (os.getenv("WEBRTC_CAPTURE_NVENC_PRESET") or "p1").strip() or "p1"


def capture_nvenc_tune() -> str:
    raw = (os.getenv("WEBRTC_CAPTURE_NVENC_TUNE") or "ll").strip().lower()
    if raw in ("hq", "ll", "ull", "lossless"):
        return raw
    return "ll"


def capture_nvfbc_bin() -> str:
    return (
        os.getenv("WEBRTC_CAPTURE_NVFBC_BIN") or "/usr/local/bin/nvfbc_nvenc_streamer"
    ).strip() or "/usr/local/bin/nvfbc_nvenc_streamer"


def capture_nvfbc_preset() -> str:
    raw = (os.getenv("WEBRTC_CAPTURE_NVFBC_PRESET") or "").strip().lower()
    if raw in ("llhp", "llhq", "ll", "hp", "hq", "default"):
        return raw
    # Map the existing FFmpeg-oriented p1 default to the nearest old SDK preset.
    nvenc = capture_nvenc_preset().lower()
    if nvenc in ("p1", "p2", "hp"):
        return "llhp"
    return "llhq"


def capture_max_width() -> int:
    raw = (os.getenv("WEBRTC_CAPTURE_MAX_WIDTH") or "1920").strip()
    try:
        return max(320, min(3840, int(raw)))
    except ValueError:
        return 1920


def capture_max_stale_frames() -> int:
    raw = (os.getenv("WEBRTC_CAPTURE_MAX_STALE_FRAMES") or "1").strip()
    try:
        return max(0, min(60, int(raw)))
    except ValueError:
        return 1


def capture_fps() -> float:
    raw = (os.getenv("WEBRTC_CAPTURE_FPS") or "30").strip()
    try:
        return float(raw)
    except ValueError:
        return 30.0


def x11grab_input(display: str) -> str:
    d = (display or ":0").strip()
    if not d:
        d = ":0"
    if "+" in d:
        return d
    if "." in d.split(":", 1)[-1]:
        return f"{d}+0,0"
    return f"{d}.0+0,0"


def probe_display_size(env: dict[str, str]) -> tuple[int, int]:
    try:
        import mss

        with mss.mss() as sct:
            mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
            return int(mon["width"]), int(mon["height"])
    except Exception as exc:
        logger.debug("mss display size probe failed: %s", exc)
    try:
        p = subprocess.run(
            ["xdpyinfo"],
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if p.returncode == 0 and p.stdout:
            for line in p.stdout.splitlines():
                if "dimensions:" in line:
                    part = line.split("dimensions:", 1)[1].strip().split()[0]
                    w, _, h = part.partition("x")
                    if w.isdigit() and h.isdigit():
                        return int(w), int(h)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug("xdpyinfo display size probe failed: %s", exc)
    return 1920, 1080


def ffmpeg_lists_h264_nvenc() -> bool:
    try:
        p = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return p.returncode == 0 and "h264_nvenc" in (p.stdout or "")


def nvenc_runtime_ok(env: dict[str, str] | None = None) -> bool:
    if not ffmpeg_lists_h264_nvenc():
        return False
    try:
        p = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "nullsrc=size=640x480:rate=1",
                "-frames:v",
                "1",
                "-c:v",
                "h264_nvenc",
                "-f",
                "null",
                "-",
            ],
            env=env,
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug("nvenc runtime probe failed: %s", exc)
        return False
    if p.returncode != 0:
        err = (p.stderr or b"").decode("utf-8", errors="ignore")[:300]
        logger.debug("nvenc runtime probe exit=%s: %s", p.returncode, err)
    return p.returncode == 0


def nvfbc_runtime_ok(env: dict[str, str] | None = None) -> bool:
    raw = capture_nvfbc_bin()
    path = raw if os.path.sep in raw else shutil.which(raw, path=(env or os.environ).get("PATH"))
    if not path:
        return False
    try:
        p = subprocess.run(
            [path, "--help"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug("nvfbc runtime probe failed: %s", exc)
        return False
    return p.returncode == 0


def resolve_capture_backend(env: dict[str, str]) -> str:
    requested = capture_backend_name()
    if requested == "mss":
        return "mss"
    if requested == "nvfbc":
        if nvfbc_runtime_ok(env):
            return "nvfbc"
        logger.warning("WEBRTC_CAPTURE_BACKEND=nvfbc but NvFBC streamer unavailable; falling back")
        if nvenc_runtime_ok(env):
            return "nvenc"
        return "mss"
    if requested == "nvenc":
        if nvenc_runtime_ok(env):
            return "nvenc"
        logger.warning("WEBRTC_CAPTURE_BACKEND=nvenc but NVENC unavailable; falling back to mss")
        return "mss"
    if nvfbc_runtime_ok(env):
        return "nvfbc"
    if nvenc_runtime_ok(env):
        return "nvenc"
    return "mss"


def _local_cursor_enabled() -> bool:
    """True when the browser draws a local cursor overlay (WEBRTC_LOCAL_CURSOR)."""
    raw = (os.getenv("WEBRTC_LOCAL_CURSOR") or "auto").strip().lower()
    return raw in ("1", "true", "yes", "on")


def build_nvenc_ffmpeg_cmd(
    *,
    display: str,
    env: dict[str, str],
    src_w: int,
    src_h: int,
    out_w: int,
    out_h: int,
    fps: float,
    bitrate_bps: int,
    preset: str,
    draw_mouse: bool = True,
) -> list[str]:
    disp = x11grab_input(display)
    fps_i = max(1, int(round(fps)))
    gop = fps_i
    low_latency = capture_nvenc_low_latency()
    # VBR burst ceiling: 1.5× target lets the encoder spend extra bits on fast
    # motion (window drags, scrolling) without exceeding link capacity on average.
    maxrate_bps = int(bitrate_bps * 1.5)
    max_frame_bits = max(maxrate_bps // fps_i, 100_000)
    # 1-frame VBV in low-latency mode: encoder cannot hold back output waiting
    # for future frames, keeping pipeline latency at a single frame interval.
    buf_frames = 1 if low_latency else 3
    bufsize = str(max(max_frame_bits * buf_frames, 300_000))
    # x11grab thread_queue_size is in frames; 512 @ 30fps ≈ 17s of capture latency.
    queue_size = "4"
    bitrate = str(bitrate_bps)
    maxrate = str(maxrate_bps)
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-f",
        "x11grab",
        "-draw_mouse",
        "1" if draw_mouse else "0",
        "-framerate",
        str(int(round(fps))),
        "-video_size",
        f"{src_w}x{src_h}",
        "-thread_queue_size",
        queue_size,
        "-i",
        disp,
    ]
    if out_w != src_w or out_h != src_h:
        cmd.extend(["-vf", f"scale={out_w}:{out_h}:flags=lanczos"])
    cmd.extend(
        [
            "-c:v",
            "h264_nvenc",
            "-preset",
            preset,
            "-tune",
            capture_nvenc_tune(),
            "-rc",
            "vbr",
            "-b:v",
            bitrate,
            "-maxrate",
            maxrate,
            "-bufsize",
            bufsize,
            "-cq",
            "26",
            "-g",
            str(gop),
            "-bf",
            "0",
            "-rc-lookahead",
            "0",
            "-zerolatency",
            "1",
            "-spatial-aq",
            "1",
            "-temporal-aq",
            "1",
            "-aq-strength",
            "4",
            "-pix_fmt",
            "yuv420p",
            "-profile:v",
            "high",
            "-flush_packets",
            "1",
            "-muxdelay",
            "0",
            "-muxpreload",
            "0",
            "-f",
            "mpegts",
            "pipe:1",
        ]
    )
    return cmd


def build_nvfbc_streamer_cmd(
    *,
    src_w: int,
    src_h: int,
    out_w: int,
    out_h: int,
    fps: float,
    bitrate_bps: int,
    preset: str,
    draw_mouse: bool = True,
) -> list[str]:
    fps_i = max(1, int(round(fps)))
    gop = fps_i
    size_w = out_w if out_w > 0 else src_w
    size_h = out_h if out_h > 0 else src_h
    cmd = [
        capture_nvfbc_bin(),
        "--size",
        f"{size_w}x{size_h}",
        "--fps",
        str(fps_i),
        "--bitrate",
        str(bitrate_bps),
        "--max-bitrate",
        str(bitrate_bps),
        "--vbv-frames",
        "1" if capture_nvenc_low_latency() else "3",
        "--gop",
        str(gop),
        "--timeout-ms",
        str(max(1, int(round(1000 / fps_i)))),
        "--preset",
        preset,
        "--profile",
        "high",
        "--mux",
        "mpegts",
        "--quiet",
    ]
    if not draw_mouse:
        cmd.append("--no-cursor")
    return cmd


class LiveNvencVideoTrack:
    """Drop stale H.264 packets so aiortc never sends seconds-old frames."""

    def __init__(self, inner: Any) -> None:
        from aiortc import MediaStreamTrack
        from aiortc.mediastreams import MediaStreamError

        max_stale = capture_max_stale_frames()

        class _Track(MediaStreamTrack):  # type: ignore[misc, valid-type]
            kind = "video"

            def __init__(self, source: Any) -> None:
                super().__init__()
                self._source = source

            async def recv(self) -> Any:
                if self.readyState != "live":
                    raise MediaStreamError
                self._source._player._start(self._source)
                latest = await self._source._queue.get()
                if max_stale > 0:
                    for _ in range(max_stale):
                        try:
                            latest = self._source._queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                if latest is None:
                    self.stop()
                    raise MediaStreamError
                return latest

            def stop(self) -> None:
                super().stop()
                self._source.stop()

        self._track = _Track(inner)

    @property
    def track(self) -> Any:
        return self._track


def scaled_output_size(src_w: int, src_h: int, max_w: int) -> tuple[int, int, int, int]:
    if src_w <= max_w:
        out_w = src_w if src_w % 2 == 0 else src_w - 1
        out_h = src_h if src_h % 2 == 0 else src_h - 1
        return src_w, src_h, max(2, out_w), max(2, out_h)
    out_w = max_w if max_w % 2 == 0 else max_w - 1
    out_h = max(2, int(round(src_h * (out_w / float(src_w)))))
    out_h = out_h if out_h % 2 == 0 else out_h - 1
    return src_w, src_h, out_w, out_h


@dataclass
class CaptureHandle:
    track: Any
    backend: str
    cleanup: Callable[[], None]


def _terminate_process(proc: subprocess.Popen[bytes] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass


def open_mss_capture(
    *,
    session_id: str,
    target_fps: float,
    max_w: int,
) -> CaptureHandle:
    from aiortc import VideoStreamTrack
    from av import VideoFrame

    import mss
    import numpy as np

    interval = 1.0 / target_fps

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
                    im = im.resize((max_w, nh), Image.Resampling.LANCZOS)
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
                logger.info(
                    "WebRTC mss frame session=%s size=%sx%s frames=%s",
                    session_id[:16],
                    w,
                    h,
                    self._frames,
                )
            return vf

    track = ScreenVideoTrack()
    return CaptureHandle(track=track, backend="mss", cleanup=lambda: None)


def open_nvenc_capture(
    *,
    session_id: str,
    display: str,
    env: dict[str, str],
    target_fps: float,
    max_w: int,
    bitrate_bps: int,
    preset: str,
    local_cursor: bool = False,
) -> CaptureHandle:
    from aiortc.contrib.media import MediaPlayer

    src_w, src_h, out_w, out_h = scaled_output_size(*probe_display_size(env), max_w)
    cmd = build_nvenc_ffmpeg_cmd(
        display=display,
        env=env,
        src_w=src_w,
        src_h=src_h,
        out_w=out_w,
        out_h=out_h,
        fps=target_fps,
        bitrate_bps=bitrate_bps,
        preset=preset,
        draw_mouse=not local_cursor,
    )
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    if proc.stdout is None:
        _terminate_process(proc)
        raise RuntimeError("ffmpeg NVENC capture did not provide stdout pipe")

    player = MediaPlayer(
        proc.stdout,
        format="mpegts",
        options={
            "fflags": "nobuffer+flush_packets+discardcorrupt",
            "flags": "low_delay",
            "probesize": "32",
            "analyzeduration": "0",
            "max_delay": "0",
        },
        decode=False,
    )
    # mpegts pipe is live NVENC output; aiortc otherwise treats it as a file and
    # paces recv() to packet timestamps — queue depth becomes multi-second lag.
    player._throttle_playback = False
    inner = player.video
    if inner is None:
        _terminate_process(proc)
        raise RuntimeError("ffmpeg NVENC capture produced no video track")
    track = LiveNvencVideoTrack(inner).track

    logger.info(
        "WebRTC nvenc capture session=%s src=%sx%s out=%sx%s fps=%s bitrate=%s preset=%s",
        session_id[:16],
        src_w,
        src_h,
        out_w,
        out_h,
        int(round(target_fps)),
        bitrate_bps,
        preset,
    )

    def cleanup() -> None:
        try:
            track.stop()
        except Exception:
            pass
        _terminate_process(proc)

    return CaptureHandle(track=track, backend="nvenc", cleanup=cleanup)


def open_nvfbc_capture(
    *,
    session_id: str,
    env: dict[str, str],
    target_fps: float,
    max_w: int,
    bitrate_bps: int,
    preset: str,
    local_cursor: bool = False,
) -> CaptureHandle:
    from aiortc.contrib.media import MediaPlayer

    src_w, src_h, out_w, out_h = scaled_output_size(*probe_display_size(env), max_w)
    cmd = build_nvfbc_streamer_cmd(
        src_w=src_w,
        src_h=src_h,
        out_w=out_w,
        out_h=out_h,
        fps=target_fps,
        bitrate_bps=bitrate_bps,
        preset=preset,
        draw_mouse=not local_cursor,
    )
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    if proc.stdout is None:
        _terminate_process(proc)
        raise RuntimeError("NvFBC capture did not provide stdout pipe")

    player = MediaPlayer(
        proc.stdout,
        format="mpegts",
        options={
            "fflags": "nobuffer+flush_packets+discardcorrupt",
            "flags": "low_delay",
            "probesize": "32",
            "analyzeduration": "0",
            "max_delay": "0",
        },
        decode=False,
    )
    player._throttle_playback = False
    inner = player.video
    if inner is None:
        _terminate_process(proc)
        raise RuntimeError("NvFBC capture produced no video track")
    track = LiveNvencVideoTrack(inner).track

    logger.info(
        "WebRTC nvfbc capture session=%s src=%sx%s out=%sx%s fps=%s bitrate=%s preset=%s",
        session_id[:16],
        src_w,
        src_h,
        out_w,
        out_h,
        int(round(target_fps)),
        bitrate_bps,
        preset,
    )

    def cleanup() -> None:
        try:
            track.stop()
        except Exception:
            pass
        _terminate_process(proc)

    return CaptureHandle(track=track, backend="nvfbc", cleanup=cleanup)


def _h264_profile_ids_from_offer(offer_sdp: str) -> list[str]:
    profiles: list[str] = []
    seen: set[str] = set()
    for line in offer_sdp.splitlines():
        if "profile-level-id=" not in line.lower():
            continue
        match = re.search(r"profile-level-id=([0-9A-Fa-f]+)", line, re.I)
        if not match:
            continue
        profile = match.group(1).lower()
        if profile not in seen:
            seen.add(profile)
            profiles.append(profile)
    return profiles


def _video_codec_label_from_sdp(sdp: str) -> str:
    in_video = False
    for line in sdp.splitlines():
        if line.startswith("m="):
            in_video = line.startswith("m=video")
            continue
        if not in_video or not line.startswith("a=rtpmap:"):
            continue
        match = re.match(r"a=rtpmap:\d+\s+(\S+)/", line, re.I)
        if match:
            return match.group(1).upper()
    return "unknown"


def prefer_h264_for_pc(
    pc: Any, backend: str, offer_sdp: str | None = None
) -> None:
    """NVENC sends pre-encoded H.264; force H.264 in SDP before setRemoteDescription."""
    if backend not in ("nvenc", "nvfbc"):
        return
    try:
        from aiortc import RTCRtpSender

        caps = RTCRtpSender.getCapabilities("video")
        h264 = [c for c in caps.codecs if c.mimeType.lower() == "video/h264"]
        if not h264:
            logger.warning("NVENC active but aiortc exposes no H264 codec capabilities")
            return
        profiles = _h264_profile_ids_from_offer(offer_sdp or "")
        if profiles:

            def rank(cap: Any) -> tuple[int, str]:
                profile = str(cap.parameters.get("profile-level-id", "")).lower()
                try:
                    return (profiles.index(profile), profile)
                except ValueError:
                    return (len(profiles), profile)

            h264 = sorted(h264, key=rank)
        for transceiver in pc.getTransceivers():
            if transceiver.kind == "video":
                transceiver.setCodecPreferences(h264)
                logger.info(
                    "WebRTC video codec preference set to H264 (%s variants, offer profiles=%s)",
                    len(h264),
                    profiles or ["default"],
                )
    except Exception:
        logger.exception("Failed to set H264 codec preferences for NVENC capture")


def open_capture(
    *,
    session_id: str,
    display: str,
    env: dict[str, str],
    target_fps: float | None = None,
    max_w: int | None = None,
    bitrate_bps: int | None = None,
    preset: str | None = None,
) -> CaptureHandle:
    fps = max(5.0, min(60.0, target_fps if target_fps is not None else capture_fps()))
    bound_w = max_w if max_w is not None else capture_max_width()
    local_cursor = _local_cursor_enabled()
    backend = resolve_capture_backend(env)
    if backend == "nvfbc":
        try:
            return open_nvfbc_capture(
                session_id=session_id,
                env=env,
                target_fps=fps,
                max_w=bound_w,
                bitrate_bps=bitrate_bps if bitrate_bps is not None else capture_bitrate_bps(),
                preset=capture_nvfbc_preset(),
                local_cursor=local_cursor,
            )
        except Exception:
            logger.exception("NvFBC capture setup failed session=%s; falling back", session_id[:16])
            if nvenc_runtime_ok(env):
                backend = "nvenc"
            else:
                backend = "mss"
    if backend == "nvenc":
        try:
            return open_nvenc_capture(
                session_id=session_id,
                display=display,
                env=env,
                target_fps=fps,
                max_w=bound_w,
                bitrate_bps=bitrate_bps if bitrate_bps is not None else capture_bitrate_bps(),
                preset=preset if preset is not None else capture_nvenc_preset(),
                local_cursor=local_cursor,
            )
        except Exception:
            logger.exception("NVENC capture setup failed session=%s; falling back to mss", session_id[:16])
    return open_mss_capture(session_id=session_id, target_fps=fps, max_w=bound_w)
