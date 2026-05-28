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


_reconnect_cooldown = 0.0  # monotonic time after which we may retry _open_display
_RECONNECT_INTERVAL = 2.0  # seconds between retry attempts


def warp_pointer(x: int, y: int, env: dict[str, str]) -> bool:
    global _display_ptr, _display_key, _reconnect_cooldown
    with _lock:
        try:
            lib = _load_lib()
            dpy = _display_ptr
            if not dpy:
                # Respect the cooldown so we don't spam XOpenDisplay on every
                # move when X11 is genuinely down.
                import time
                now = time.monotonic()
                if now < _reconnect_cooldown:
                    return False
                dpy = _open_display(env)
                if not dpy:
                    _reconnect_cooldown = now + _RECONNECT_INTERVAL
                    return False
            root = lib.XDefaultRootWindow(dpy)
            lib.XWarpPointer(dpy, 0, root, 0, 0, 0, 0, int(x), int(y))
            lib.XFlush(dpy)
            return True
        except Exception as exc:
            logger.debug("XWarpPointer failed: %s", exc)
            if _display_ptr is not None:
                try:
                    lib.XCloseDisplay(_display_ptr)
                except Exception:
                    pass
            _display_ptr = None
            _display_key = None
            import time
            _reconnect_cooldown = time.monotonic() + _RECONNECT_INTERVAL
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


_libfixes: Any = None


class XFixesCursorImage(ctypes.Structure):
    _fields_ = [
        ("x", ctypes.c_short),
        ("y", ctypes.c_short),
        ("width", ctypes.c_ushort),
        ("height", ctypes.c_ushort),
        ("xhot", ctypes.c_ushort),
        ("yhot", ctypes.c_ushort),
        ("cursor_serial", ctypes.c_ulong),
        ("pixels", ctypes.POINTER(ctypes.c_ulong)),
        ("atom", ctypes.c_ulong),
        ("name", ctypes.c_char_p)
    ]


def _load_libfixes() -> Any:
    global _libfixes
    if _libfixes is not None:
        return _libfixes
    path = ctypes.util.find_library("Xfixes") or "libXfixes.so.3"
    lib = ctypes.CDLL(path)
    lib.XFixesGetCursorImage.restype = ctypes.POINTER(XFixesCursorImage)
    lib.XFixesGetCursorImage.argtypes = [ctypes.c_void_p]
    _libfixes = lib
    return lib


def get_cursor_info(env: dict[str, str], last_serial: int = 0) -> dict[str, Any] | None:
    global _display_ptr, _display_key, _reconnect_cooldown
    with _lock:
        try:
            lib = _load_lib()
            libfixes = _load_libfixes()
            dpy = _display_ptr
            if not dpy:
                import time
                now = time.monotonic()
                if now < _reconnect_cooldown:
                    return None
                dpy = _open_display(env)
                if not dpy:
                    _reconnect_cooldown = now + _RECONNECT_INTERVAL
                    return None
            img_ptr = libfixes.XFixesGetCursorImage(dpy)
            if not img_ptr:
                return None
            try:
                img = img_ptr.contents
                serial = int(img.cursor_serial)
                if serial == last_serial:
                    return {"serial": serial, "changed": False}

                width = int(img.width)
                height = int(img.height)
                xhot = int(img.xhot)
                yhot = int(img.yhot)
                name = img.name.decode("utf-8", errors="ignore") if img.name else None

                if width <= 0 or height <= 0:
                    return {
                        "serial": serial,
                        "changed": True,
                        "name": name,
                        "width": 0,
                        "height": 0,
                        "xhot": 0,
                        "yhot": 0,
                        "img": ""
                    }

                num_pixels = width * height
                raw_pixels = img.pixels[:num_pixels]

                rgba_data = bytearray(num_pixels * 4)
                for i, val in enumerate(raw_pixels):
                    a = (val >> 24) & 0xff
                    r = (val >> 16) & 0xff
                    g = (val >> 8) & 0xff
                    b = val & 0xff
                    offset = i * 4
                    rgba_data[offset] = r
                    rgba_data[offset+1] = g
                    rgba_data[offset+2] = b
                    rgba_data[offset+3] = a

                from PIL import Image
                import base64
                from io import BytesIO

                pil_img = Image.frombytes("RGBA", (width, height), bytes(rgba_data))
                buffered = BytesIO()
                pil_img.save(buffered, format="PNG")
                img_str = base64.b64encode(buffered.getvalue()).decode("utf-8")

                return {
                    "serial": serial,
                    "changed": True,
                    "name": name,
                    "width": width,
                    "height": height,
                    "xhot": xhot,
                    "yhot": yhot,
                    "img": img_str
                }
            finally:
                lib.XFree(img_ptr)
        except Exception as exc:
            logger.debug("get_cursor_info failed: %s", exc)
            if _display_ptr is not None:
                try:
                    lib.XCloseDisplay(_display_ptr)
                except Exception:
                    pass
            _display_ptr = None
            _display_key = None
            import time
            _reconnect_cooldown = time.monotonic() + _RECONNECT_INTERVAL
            return None


_libxtst: Any = None


def _load_libxtst() -> Any:
    global _libxtst
    if _libxtst is not None:
        return _libxtst
    path = ctypes.util.find_library("Xtst") or "libXtst.so.6"
    lib = ctypes.CDLL(path)
    # int XTestFakeMotionEvent(Display *display, int screen_number, int x, int y, unsigned long delay);
    lib.XTestFakeMotionEvent.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_ulong]
    lib.XTestFakeMotionEvent.restype = ctypes.c_int
    # int XTestFakeButtonEvent(Display *display, unsigned int button, Bool is_press, unsigned long delay);
    lib.XTestFakeButtonEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
    lib.XTestFakeButtonEvent.restype = ctypes.c_int
    _libxtst = lib
    return lib


def xtest_mousemove(x: int, y: int, env: dict[str, str]) -> bool:
    global _display_ptr, _display_key, _reconnect_cooldown
    with _lock:
        try:
            lib = _load_lib()
            libxtst = _load_libxtst()
            dpy = _display_ptr
            if not dpy:
                import time
                now = time.monotonic()
                if now < _reconnect_cooldown:
                    return False
                dpy = _open_display(env)
                if not dpy:
                    _reconnect_cooldown = now + _RECONNECT_INTERVAL
                    return False
            libxtst.XTestFakeMotionEvent(dpy, -1, int(x), int(y), 0)
            lib.XFlush(dpy)
            return True
        except Exception as exc:
            logger.warning("XTestFakeMotionEvent failed: %s", exc)
            if _display_ptr is not None:
                try:
                    lib.XCloseDisplay(_display_ptr)
                except Exception:
                    pass
            _display_ptr = None
            _display_key = None
            import time
            _reconnect_cooldown = time.monotonic() + _RECONNECT_INTERVAL
            return False


def xtest_mousedown(button: int, env: dict[str, str]) -> bool:
    global _display_ptr, _display_key, _reconnect_cooldown
    with _lock:
        try:
            lib = _load_lib()
            libxtst = _load_libxtst()
            dpy = _display_ptr
            if not dpy:
                import time
                now = time.monotonic()
                if now < _reconnect_cooldown:
                    return False
                dpy = _open_display(env)
                if not dpy:
                    _reconnect_cooldown = now + _RECONNECT_INTERVAL
                    return False
            libxtst.XTestFakeButtonEvent(dpy, int(button), 1, 0)
            lib.XFlush(dpy)
            return True
        except Exception as exc:
            logger.warning("XTestFakeButtonEvent (down) failed: %s", exc)
            if _display_ptr is not None:
                try:
                    lib.XCloseDisplay(_display_ptr)
                except Exception:
                    pass
            _display_ptr = None
            _display_key = None
            import time
            _reconnect_cooldown = time.monotonic() + _RECONNECT_INTERVAL
            return False


def xtest_mouseup(button: int, env: dict[str, str]) -> bool:
    global _display_ptr, _display_key, _reconnect_cooldown
    with _lock:
        try:
            lib = _load_lib()
            libxtst = _load_libxtst()
            dpy = _display_ptr
            if not dpy:
                import time
                now = time.monotonic()
                if now < _reconnect_cooldown:
                    return False
                dpy = _open_display(env)
                if not dpy:
                    _reconnect_cooldown = now + _RECONNECT_INTERVAL
                    return False
            libxtst.XTestFakeButtonEvent(dpy, int(button), 0, 0)
            lib.XFlush(dpy)
            return True
        except Exception as exc:
            logger.warning("XTestFakeButtonEvent (up) failed: %s", exc)
            if _display_ptr is not None:
                try:
                    lib.XCloseDisplay(_display_ptr)
                except Exception:
                    pass
            _display_ptr = None
            _display_key = None
            import time
            _reconnect_cooldown = time.monotonic() + _RECONNECT_INTERVAL
            return False


def xtest_click(button: int, repeat: int, env: dict[str, str]) -> bool:
    global _display_ptr, _display_key, _reconnect_cooldown
    with _lock:
        try:
            lib = _load_lib()
            libxtst = _load_libxtst()
            dpy = _display_ptr
            if not dpy:
                import time
                now = time.monotonic()
                if now < _reconnect_cooldown:
                    return False
                dpy = _open_display(env)
                if not dpy:
                    _reconnect_cooldown = now + _RECONNECT_INTERVAL
                    return False
            btn = int(button)
            use_delay = btn in (1, 2, 3)
            for i in range(repeat):
                libxtst.XTestFakeButtonEvent(dpy, btn, 1, 0)
                if use_delay:
                    lib.XFlush(dpy)
                    import time
                    time.sleep(0.04)
                libxtst.XTestFakeButtonEvent(dpy, btn, 0, 0)
                if use_delay:
                    lib.XFlush(dpy)
                    if i < repeat - 1:
                        import time
                        time.sleep(0.04)
            if not use_delay:
                lib.XFlush(dpy)
            return True
        except Exception as exc:
            logger.warning("XTestFakeButtonEvent (click) failed: %s", exc)
            if _display_ptr is not None:
                try:
                    lib.XCloseDisplay(_display_ptr)
                except Exception:
                    pass
            _display_ptr = None
            _display_key = None
            import time
            _reconnect_cooldown = time.monotonic() + _RECONNECT_INTERVAL
            return False



