"""Low-latency X11 pointer moves via XWarpPointer (avoids xdotool subprocess per move)."""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import threading
from typing import Any

logger = logging.getLogger("axonos.webrtc_agent.x11")

_lock = threading.Lock()
_lib: Any = None
_display_ptr: Any = None
_display_key: str | None = None


def _load_lib() -> Any:
    global _lib
    if _lib is not None:
        return _lib
    path = ctypes.util.find_library("X11") or "libX11.so.6"
    lib = ctypes.CDLL(path)
    lib.XOpenDisplay.restype = ctypes.c_void_p
    lib.XCloseDisplay.argtypes = [ctypes.c_void_p]
    lib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    lib.XDefaultRootWindow.restype = ctypes.c_ulong
    lib.XWarpPointer.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.c_int,
        ctypes.c_int,
    ]
    lib.XFlush.argtypes = [ctypes.c_void_p]
    _lib = lib
    return lib


def _open_display(env: dict[str, str]) -> Any:
    global _display_ptr, _display_key
    display_name = (env.get("DISPLAY") or ":0").strip()
    xauth = (env.get("XAUTHORITY") or "").strip()
    key = f"{display_name}|{xauth}"
    if _display_ptr is not None and _display_key == key:
        return _display_ptr
    lib = _load_lib()
    if _display_ptr is not None:
        lib.XCloseDisplay(_display_ptr)
        _display_ptr = None
    if xauth:
        os.environ["XAUTHORITY"] = xauth
    os.environ["DISPLAY"] = display_name
    _display_ptr = lib.XOpenDisplay(display_name.encode() or None)
    _display_key = key if _display_ptr else None
    return _display_ptr


def warp_pointer(x: int, y: int, env: dict[str, str]) -> bool:
    with _lock:
        try:
            lib = _load_lib()
            dpy = _open_display(env)
            if not dpy:
                return False
            root = lib.XDefaultRootWindow(dpy)
            lib.XWarpPointer(dpy, 0, root, 0, 0, 0, 0, int(x), int(y))
            lib.XFlush(dpy)
            return True
        except Exception as exc:
            logger.debug("XWarpPointer failed: %s", exc)
            global _display_ptr, _display_key
            _display_ptr = None
            _display_key = None
            return False


def close_display() -> None:
    global _display_ptr, _display_key
    with _lock:
        if _display_ptr is None:
            return
        try:
            _load_lib().XCloseDisplay(_display_ptr)
        except Exception:
            pass
        _display_ptr = None
        _display_key = None
