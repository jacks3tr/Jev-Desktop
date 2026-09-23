"""Desktop presence: a quiet edge glow while Jev holds the desktop, and the human's input.

Runs in the broker process on one thread that owns a message loop, the overlay windows, and
(only while active) the low-level mouse and keyboard hooks. The glow is four click-through
strip windows per monitor rather than one full-screen layered window, so it never sits over
the middle of a fullscreen video or game. It is deliberately not excluded from capture: a
person watching through remote desktop or screen sharing sees it too, and so do the
plugin's own screenshots, as a faint edge tint.

Hook callbacks do almost nothing: they classify the event, bump an integer, and hand Esc to
a dispatcher thread. Windows unhooks a low-level hook that exceeds LowLevelHooksTimeout.
"""

from __future__ import annotations

import ctypes
import math
import queue
import sys
import threading
import time
import traceback
from collections.abc import Callable
from ctypes import wintypes
from typing import Any

from ...contracts import DriverError, Rect
from . import win32
from .win32 import JEV_INPUT_TAG, PRESENCE_CLASS

# --------------------------------------------------------------------------------------
# Vignette field (pure)
# --------------------------------------------------------------------------------------

COLOR = (56, 132, 255)  # RGB
PEAK_ALPHA = 0.34
DEPTH_AT_96_DPI = 20
FADE_IN_S = 0.18
FADE_OUT_S = 0.30


def vignette_depth(monitor: Rect, dpi: int) -> int:
    """Glow depth in physical pixels, scaled by the monitor's DPI and never over half the monitor."""
    depth = max(1, round(DEPTH_AT_96_DPI * dpi / 96))
    return max(0, min(depth, monitor.width // 2, monitor.height // 2))


def vignette_alpha(dx: int, dy: int, depth: int) -> float:
    """Alpha of the pixel `dx` columns and `dy` rows in from its nearest monitor edges.

    The glow is a function of how far the pixel center reaches past an inner rectangle
    inset by `depth`. Near a corner that reach is a Euclidean distance, so the iso-lines
    round the corner instead of meeting at a mitred crease, and the corner never exceeds the
    edge peak. A quadratic ease reads as a soft glow rather than a band, and reaches zero
    with zero slope, so there is no visible inner line.
    """
    if depth <= 0:
        return 0.0
    px = max(depth - (dx + 0.5), 0.0)
    py = max(depth - (dy + 0.5), 0.0)
    t = min(math.hypot(px, py) / depth, 1.0)
    return PEAK_ALPHA * t * t


def vignette_strips(monitor: Rect, depth: int) -> list[Rect]:
    """Top and bottom span the monitor; left and right fill between them. No overlap, no gaps."""
    left, top, right, bottom = monitor.left, monitor.top, monitor.right, monitor.bottom
    strips = [
        Rect(left, top, right, top + depth),
        Rect(left, bottom - depth, right, bottom),
        Rect(left, top + depth, left + depth, bottom - depth),
        Rect(right - depth, top + depth, right, bottom - depth),
    ]
    return [strip for strip in strips if not strip.is_empty]


def _pixel(alpha: float) -> bytes:
    """One premultiplied BGRA pixel of the glow color."""
    a = round(alpha * 255)
    red, green, blue = COLOR
    return bytes((round(blue * a / 255), round(green * a / 255), round(red * a / 255), a))


def render_strip(monitor: Rect, strip: Rect, depth: int) -> bytes:
    """Top-down premultiplied BGRA pixels of `strip`, cut from the monitor-wide field."""
    rows: dict[int, bytes] = {}
    out = bytearray()
    start, stop = (strip.left - monitor.left) * 4, (strip.right - monitor.left) * 4
    for y in range(strip.top, strip.bottom):
        dy = min(y - monitor.top, monitor.bottom - 1 - y, depth)
        row = rows.get(dy)
        if row is None:
            edge = [_pixel(vignette_alpha(dx, dy, depth)) for dx in range(depth)]
            inner = _pixel(vignette_alpha(depth, dy, depth))
            row = b"".join(edge) + inner * (monitor.width - 2 * depth) + b"".join(reversed(edge))
            rows[dy] = row
        out += row[start:stop]
    return bytes(out)


# --------------------------------------------------------------------------------------
# Hook event classification (pure)
# --------------------------------------------------------------------------------------

WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0100, 0x0101, 0x0104, 0x0105
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN, WM_RBUTTONDOWN, WM_MBUTTONDOWN = 0x0201, 0x0204, 0x0207
WM_MOUSEWHEEL, WM_XBUTTONDOWN, WM_MOUSEHWHEEL = 0x020A, 0x020B, 0x020E
LLMHF_INJECTED, LLMHF_LOWER_IL_INJECTED = 0x01, 0x02
LLKHF_LOWER_IL_INJECTED, LLKHF_INJECTED = 0x02, 0x10
VK_ESCAPE = 0x1B

_MOUSE_HUMAN = frozenset(
    {WM_LBUTTONDOWN, WM_RBUTTONDOWN, WM_MBUTTONDOWN, WM_XBUTTONDOWN, WM_MOUSEWHEEL, WM_MOUSEHWHEEL}
)
_KEY_DOWN = frozenset({WM_KEYDOWN, WM_SYSKEYDOWN})
_KEY_UP = frozenset({WM_KEYUP, WM_SYSKEYUP})


def classify(message: int, vk: int, flags: int, extra_info: int) -> str:
    """'human', 'escape', or 'ignore' for one low-level hook event.

    Only the plugin's own input is both injected and tagged with `JEV_INPUT_TAG`. Everything
    else is a person, including input injected by a remote-desktop client on their behalf.
    Pointer moves and releases are not a person taking over.
    """
    if message in _MOUSE_HUMAN:
        own = flags & (LLMHF_INJECTED | LLMHF_LOWER_IL_INJECTED) and extra_info == JEV_INPUT_TAG
        return "ignore" if own else "human"
    if message in _KEY_DOWN:
        if flags & (LLKHF_INJECTED | LLKHF_LOWER_IL_INJECTED) and extra_info == JEV_INPUT_TAG:
            return "ignore"
        return "escape" if vk == VK_ESCAPE else "human"
    return "ignore"


# --------------------------------------------------------------------------------------
# Fade timeline (pure)
# --------------------------------------------------------------------------------------


def _smooth(p: float) -> float:
    p = min(max(p, 0.0), 1.0)
    return p * p * (3.0 - 2.0 * p)


class Fade:
    """Overlay opacity over time: fade in when active; fade out once inactive for `linger_s`.

    Re-activation at any point, including mid fade-out, reverses smoothly from the current
    level. Durations scale with the distance still to travel.
    """

    def __init__(self, linger_s: float, fade_in_s: float = FADE_IN_S, fade_out_s: float = FADE_OUT_S) -> None:
        self.linger_s = linger_s
        self.fade_in_s = fade_in_s
        self.fade_out_s = fade_out_s
        self.active = False
        self._from = self._to = 0.0
        self._start = self._duration = 0.0
        self._out_at: float | None = None

    def _segment(self, now: float) -> float:
        if self._duration <= 0:
            return self._to
        return self._from + (self._to - self._from) * _smooth((now - self._start) / self._duration)

    def _out(self) -> tuple[float, float]:
        assert self._out_at is not None
        level = self._segment(self._out_at)
        return level, self.fade_out_s * level

    def opacity(self, now: float) -> float:
        if self._out_at is None or now <= self._out_at:
            return self._segment(now)
        level, duration = self._out()
        if duration <= 0:
            return 0.0
        return level * (1.0 - _smooth((now - self._out_at) / duration))

    def set_active(self, active: bool, now: float) -> None:
        if active == self.active:
            return
        self.active = active
        if active:
            level = self.opacity(now)
            self._from, self._to, self._start = level, 1.0, now
            self._duration = self.fade_in_s * (1.0 - level)
            self._out_at = None
        else:
            self._out_at = now + self.linger_s

    def next_change(self, now: float) -> float | None:
        """Seconds until the opacity next moves (0.0 while animating), or None once settled."""
        fading_in = now < self._start + self._duration
        if self.active or self._out_at is None:
            return 0.0 if fading_in else None
        if now < self._out_at:
            return 0.0 if fading_in else self._out_at - now
        _level, duration = self._out()
        return 0.0 if now < self._out_at + duration else None


# --------------------------------------------------------------------------------------
# Win32 declarations (own DLL instances: argtypes never collide with win32.py's)
# --------------------------------------------------------------------------------------

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_shcore: Any
try:
    _shcore = ctypes.WinDLL("shcore", use_last_error=True)
except OSError:  # pragma: no cover - shcore ships with every supported Windows
    _shcore = None

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
MONITORENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(win32.RECT), wintypes.LPARAM
)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON),
    ]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", win32.POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_ubyte),
        ("BlendFlags", ctypes.c_ubyte),
        ("SourceConstantAlpha", ctypes.c_ubyte),
        ("AlphaFormat", ctypes.c_ubyte),
    ]


_user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
_user32.RegisterClassExW.restype = wintypes.ATOM
_user32.CreateWindowExW.argtypes = [
    wintypes.DWORD,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HWND,
    wintypes.HMENU,
    wintypes.HINSTANCE,
    wintypes.LPVOID,
]
_user32.CreateWindowExW.restype = wintypes.HWND
_user32.DestroyWindow.argtypes = [wintypes.HWND]
_user32.DestroyWindow.restype = wintypes.BOOL
_user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.DefWindowProcW.restype = LRESULT
_user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
_user32.GetMessageW.restype = wintypes.BOOL
_user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
_user32.TranslateMessage.restype = wintypes.BOOL
_user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
_user32.DispatchMessageW.restype = LRESULT
_user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.PostMessageW.restype = wintypes.BOOL
_user32.PostQuitMessage.argtypes = [ctypes.c_int]
_user32.PostQuitMessage.restype = None
_user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
_user32.SetWindowsHookExW.restype = wintypes.HHOOK
_user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
_user32.UnhookWindowsHookEx.restype = wintypes.BOOL
_user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
_user32.CallNextHookEx.restype = LRESULT
_user32.UpdateLayeredWindow.argtypes = [
    wintypes.HWND,
    wintypes.HDC,
    ctypes.POINTER(win32.POINT),
    ctypes.POINTER(wintypes.SIZE),
    wintypes.HDC,
    ctypes.POINTER(win32.POINT),
    wintypes.COLORREF,
    ctypes.POINTER(BLENDFUNCTION),
    wintypes.DWORD,
]
_user32.UpdateLayeredWindow.restype = wintypes.BOOL
_user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
_user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
_user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.POINTER(win32.RECT), MONITORENUMPROC, wintypes.LPARAM]
_user32.EnumDisplayMonitors.restype = wintypes.BOOL
_user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(win32.MONITORINFO)]
_user32.GetMonitorInfoW.restype = wintypes.BOOL
_user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
_user32.SetTimer.restype = ctypes.c_size_t
_user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
_user32.KillTimer.restype = wintypes.BOOL
_user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.ShowWindow.restype = wintypes.BOOL
_user32.SetWindowPos.argtypes = [
    wintypes.HWND,
    wintypes.HWND,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.UINT,
]
_user32.SetWindowPos.restype = wintypes.BOOL
_user32.GetDC.argtypes = [wintypes.HWND]
_user32.GetDC.restype = wintypes.HDC
_user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
_user32.ReleaseDC.restype = ctypes.c_int
_gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
_gdi32.CreateCompatibleDC.restype = wintypes.HDC
_gdi32.CreateDIBSection.argtypes = [
    wintypes.HDC,
    ctypes.POINTER(win32.BITMAPINFO),
    wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p),
    wintypes.HANDLE,
    wintypes.DWORD,
]
_gdi32.CreateDIBSection.restype = wintypes.HBITMAP
_gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
_gdi32.SelectObject.restype = wintypes.HGDIOBJ
_gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
_gdi32.DeleteObject.restype = wintypes.BOOL
_gdi32.DeleteDC.argtypes = [wintypes.HDC]
_gdi32.DeleteDC.restype = wintypes.BOOL
_kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
_kernel32.GetModuleHandleW.restype = wintypes.HMODULE
if _shcore is not None:
    _shcore.GetDpiForMonitor.argtypes = [
        wintypes.HMONITOR,
        ctypes.c_int,
        ctypes.POINTER(wintypes.UINT),
        ctypes.POINTER(wintypes.UINT),
    ]
    _shcore.GetDpiForMonitor.restype = ctypes.c_long

WH_KEYBOARD_LL, WH_MOUSE_LL = 13, 14
WS_POPUP = 0x80000000
WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_NOACTIVATE = 0x00080000, 0x00000020, 0x08000000
OVERLAY_EX_STYLE = WS_EX_LAYERED | WS_EX_TRANSPARENT | win32.WS_EX_TOPMOST | win32.WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
SW_SHOWNOACTIVATE = 4
HWND_TOPMOST = -1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010
ULW_ALPHA = 0x02
AC_SRC_OVER, AC_SRC_ALPHA = 0x00, 0x01
HWND_MESSAGE = -3
MDT_EFFECTIVE_DPI = 0
ERROR_CLASS_ALREADY_EXISTS = 1410
WM_DESTROY, WM_TIMER, WM_DISPLAYCHANGE = 0x0002, 0x0113, 0x007E
WM_NCHITTEST, WM_MOUSEACTIVATE, WM_DPICHANGED = 0x0084, 0x0021, 0x02E0
HTTRANSPARENT, MA_NOACTIVATE = -1, 3
WM_APP = 0x8000
_WM_STATE, _WM_REBUILD, _WM_CLOSE = WM_APP + 1, WM_APP + 2, WM_APP + 3
_TIMER = 1
_FRAME_MS = 10  # USER_TIMER_MINIMUM: fires on every system timer tick

# One window class per process; each window routes to the presence that owns it.
_routes: dict[int, Callable[[int, int, int, int], int | None]] = {}
_class_lock = threading.Lock()
_class_registered = False


def _window_proc(hwnd: int, message: int, wparam: int, lparam: int) -> int:
    route = _routes.get(hwnd or 0)
    if route is not None:
        try:
            result = route(hwnd, message, wparam, lparam)
        except Exception:
            traceback.print_exc()
            result = None
        if result is not None:
            return result
    return int(_user32.DefWindowProcW(hwnd, message, wparam, lparam))


_WNDPROC = WNDPROC(_window_proc)


def _register_class() -> None:
    global _class_registered
    with _class_lock:
        if _class_registered:
            return
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = _WNDPROC
        wc.hInstance = _kernel32.GetModuleHandleW(None)
        wc.lpszClassName = PRESENCE_CLASS
        if not _user32.RegisterClassExW(ctypes.byref(wc)) and ctypes.get_last_error() != ERROR_CLASS_ALREADY_EXISTS:
            raise DriverError(f"RegisterClassExW failed ({ctypes.get_last_error()})")
        _class_registered = True


def _monitors() -> list[tuple[Rect, int]]:
    """(rcMonitor, effective DPI) per monitor, in physical pixels on a per-monitor-aware thread."""
    found: list[tuple[Rect, int]] = []

    def visit(monitor: int, _dc: int, _rect: Any, _data: int) -> bool:
        info = win32.MONITORINFO()
        info.cbSize = ctypes.sizeof(win32.MONITORINFO)
        if _user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            dpi_x, dpi_y = wintypes.UINT(96), wintypes.UINT(96)
            if _shcore is not None:
                _shcore.GetDpiForMonitor(monitor, MDT_EFFECTIVE_DPI, ctypes.byref(dpi_x), ctypes.byref(dpi_y))
            found.append((info.rcMonitor.as_rect(), int(dpi_x.value) or 96))
        return True

    _user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(visit), 0)
    return found


def _blend(alpha: int) -> BLENDFUNCTION:
    return BLENDFUNCTION(AC_SRC_OVER, 0, alpha, AC_SRC_ALPHA)


# --------------------------------------------------------------------------------------
# DesktopPresence
# --------------------------------------------------------------------------------------


class DesktopPresence:
    """Edge glow and physical-input watch for the desktop the broker is driving.

    `set_active(True)` installs the hooks and fades the glow in; `set_active(False)` removes
    the hooks at once and fades out after `linger_s`, unless re-activated first. Physical
    button presses, wheel turns, and key presses other than Esc bump `human_epoch`; a
    physical Esc press calls `on_escape` on a dispatcher thread.
    """

    def __init__(self, *, linger_s: float = 2.0) -> None:
        self.on_escape: Callable[[], None] | None = None
        self._linger_s = linger_s
        self._clock: Callable[[], float] = time.monotonic
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._dispatcher: threading.Thread | None = None
        self._escapes: queue.SimpleQueue[bool] = queue.SimpleQueue()
        self._error: BaseException | None = None
        self._control = 0  # posted to from any thread; cleared by close()
        self._hwnd = 0  # the same window, as the presence thread knows it
        self._want_active = False
        self._epoch = 0
        self._last_human: float | None = None
        self._escape_held = False
        # Presence-thread state.
        self._fade = Fade(linger_s)
        self._hooks: list[int] = []
        self._strips: list[int] = []
        self._built = False
        self._alpha = -1
        self._rebuild_pending = False
        self._mouse_proc = HOOKPROC(self._on_mouse)
        self._key_proc = HOOKPROC(self._on_key)

    # -- public ----------------------------------------------------------------------

    @property
    def human_epoch(self) -> int:
        return self._epoch

    def idle_seconds(self) -> float:
        last = self._last_human
        return math.inf if last is None else max(0.0, self._clock() - last)

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            ready = threading.Event()
            self._error = None
            self._dispatcher = threading.Thread(target=self._dispatch_escapes, name="jev-presence-esc", daemon=True)
            self._thread = threading.Thread(target=self._run, args=(ready,), name="jev-presence", daemon=True)
            self._dispatcher.start()
            self._thread.start()
        ready.wait(10.0)
        if self._error is not None or not self._control:
            error = self._error
            self.close()
            raise DriverError(f"desktop presence failed to start: {error or 'timed out'}")

    def close(self) -> None:
        with self._lock:
            thread, dispatcher = self._thread, self._dispatcher
            self._thread = self._dispatcher = None
            control, self._control = self._control, 0
        if thread is None:
            return
        if control:
            _user32.PostMessageW(control, _WM_CLOSE, 0, 0)
        if thread is not threading.current_thread():
            thread.join(5.0)
        self._escapes.put(False)
        if dispatcher is not None and dispatcher is not threading.current_thread():
            dispatcher.join(5.0)

    def set_active(self, active: bool) -> None:
        self._want_active = bool(active)
        control = self._control
        if control:
            _user32.PostMessageW(control, _WM_STATE, 0, 0)

    # -- hooks (hook thread: minimal work, always chain) --------------------------------

    def _human(self) -> None:
        self._last_human = self._clock()
        self._epoch += 1

    def _mouse_event(self, message: int, flags: int, extra_info: int) -> None:
        if classify(message, 0, flags, extra_info) == "human":
            self._human()

    def _key_event(self, message: int, vk: int, flags: int, extra_info: int) -> None:
        if vk == VK_ESCAPE and message in _KEY_UP:
            self._escape_held = False
            return
        kind = classify(message, vk, flags, extra_info)
        if kind == "human":
            self._human()
        elif kind == "escape" and not self._escape_held:
            self._escape_held = True
            self._escapes.put(True)

    def _on_mouse(self, code: int, wparam: int, lparam: int) -> int:
        if code >= 0 and wparam != WM_MOUSEMOVE:
            try:
                info = MSLLHOOKSTRUCT.from_address(lparam)
                self._mouse_event(wparam, info.flags, info.dwExtraInfo)
            except Exception:
                traceback.print_exc()
        return int(_user32.CallNextHookEx(None, code, wparam, lparam))

    def _on_key(self, code: int, wparam: int, lparam: int) -> int:
        if code >= 0:
            try:
                info = KBDLLHOOKSTRUCT.from_address(lparam)
                self._key_event(wparam, info.vkCode, info.flags, info.dwExtraInfo)
            except Exception:
                traceback.print_exc()
        return int(_user32.CallNextHookEx(None, code, wparam, lparam))

    def _dispatch_escapes(self) -> None:
        while self._escapes.get():
            callback = self.on_escape
            if callback is None:
                continue
            try:
                callback()
            except Exception:
                traceback.print_exc()

    # -- presence thread -------------------------------------------------------------

    def _run(self, ready: threading.Event) -> None:
        try:
            # Thread-scoped: monitor rectangles and window placement in physical pixels
            # without changing the broker process's own DPI awareness.
            _user32.SetThreadDpiAwarenessContext(win32.DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
            _register_class()
            control = _user32.CreateWindowExW(
                0, PRESENCE_CLASS, None, 0, 0, 0, 0, 0, HWND_MESSAGE, None, _kernel32.GetModuleHandleW(None), None
            )
            if not control:
                raise DriverError(f"CreateWindowExW(message) failed ({ctypes.get_last_error()})")
            _routes[control] = self._control_message
            self._hwnd = self._control = control
        except BaseException as exc:
            self._error = exc
            ready.set()
            return
        ready.set()
        try:
            self._apply_state()
            message = wintypes.MSG()
            while _user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                _user32.TranslateMessage(ctypes.byref(message))
                _user32.DispatchMessageW(ctypes.byref(message))
        finally:
            self._remove_hooks()
            self._destroy_strips()
            _routes.pop(control, None)
            self._hwnd = 0
            self._fade = Fade(self._linger_s)

    def _control_message(self, hwnd: int, message: int, _wparam: int, _lparam: int) -> int | None:
        if message == _WM_STATE:
            self._apply_state()
        elif message == WM_TIMER:
            self._pump()
        elif message == _WM_REBUILD:
            self._rebuild_pending = False
            if self._built:
                self._destroy_strips()
                self._pump()
        elif message == _WM_CLOSE:
            _user32.KillTimer(hwnd, _TIMER)
            self._remove_hooks()
            self._destroy_strips()
            _user32.DestroyWindow(hwnd)
        elif message == WM_DESTROY:
            _user32.PostQuitMessage(0)
        else:
            return None
        return 0

    def _strip_message(self, _hwnd: int, message: int, _wparam: int, _lparam: int) -> int | None:
        if message == WM_NCHITTEST:
            return HTTRANSPARENT
        if message == WM_MOUSEACTIVATE:
            return MA_NOACTIVATE
        if message in (WM_DISPLAYCHANGE, WM_DPICHANGED):
            if not self._rebuild_pending and self._hwnd:
                self._rebuild_pending = True
                _user32.PostMessageW(self._hwnd, _WM_REBUILD, 0, 0)
            return 0
        return None

    def _apply_state(self) -> None:
        active = self._want_active
        if active == self._fade.active:
            return
        if not active:
            self._remove_hooks()
        elif self._built:
            # A shell window (the taskbar) may have risen above the glow since it was built.
            for hwnd in self._strips:
                _user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        self._fade.set_active(active, self._clock())
        self._pump()
        if active:
            # After the build: hook callbacks run on this thread, so input would wait on it.
            self._install_hooks()

    def _pump(self) -> None:
        now = self._clock()
        level = self._fade.opacity(now)
        delay = self._fade.next_change(now)
        alpha = round(level * 255)
        if alpha > 0 or self._fade.active or delay is not None:
            if not self._built:
                self._build(alpha)
            self._set_alpha(alpha)
        else:
            self._destroy_strips()
        if delay is None:
            _user32.KillTimer(self._hwnd, _TIMER)
        else:
            _user32.SetTimer(self._hwnd, _TIMER, max(_FRAME_MS, math.ceil(delay * 1000)), None)

    def _install_hooks(self) -> None:
        if self._hooks:
            return
        self._escape_held = False
        module = _kernel32.GetModuleHandleW(None)
        for kind, proc in ((WH_MOUSE_LL, self._mouse_proc), (WH_KEYBOARD_LL, self._key_proc)):
            hook = _user32.SetWindowsHookExW(kind, proc, module, 0)
            if hook:
                self._hooks.append(hook)
            else:
                print(f"jev presence: SetWindowsHookExW({kind}) failed ({ctypes.get_last_error()})", file=sys.stderr)

    def _remove_hooks(self) -> None:
        while self._hooks:
            _user32.UnhookWindowsHookEx(self._hooks.pop())

    # -- overlay windows ---------------------------------------------------------------

    def _build(self, alpha: int) -> None:
        self._built = True
        self._alpha = alpha
        module = _kernel32.GetModuleHandleW(None)
        screen = _user32.GetDC(None)
        try:
            for monitor, dpi in _monitors():
                depth = vignette_depth(monitor, dpi)
                for strip in vignette_strips(monitor, depth):
                    hwnd = _user32.CreateWindowExW(
                        OVERLAY_EX_STYLE,
                        PRESENCE_CLASS,
                        None,
                        WS_POPUP,
                        strip.left,
                        strip.top,
                        strip.width,
                        strip.height,
                        None,
                        None,
                        module,
                        None,
                    )
                    if not hwnd:
                        continue
                    _routes[hwnd] = self._strip_message
                    self._strips.append(hwnd)
                    self._paint(screen, hwnd, strip, render_strip(monitor, strip, depth), alpha)
                    _user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
        finally:
            _user32.ReleaseDC(None, screen)

    def _paint(self, screen: int, hwnd: int, strip: Rect, pixels: bytes, alpha: int) -> None:
        info = win32.BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(win32.BITMAPINFOHEADER)
        info.bmiHeader.biWidth = strip.width
        info.bmiHeader.biHeight = -strip.height  # top-down
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        memory = _gdi32.CreateCompatibleDC(screen)
        bits = ctypes.c_void_p()
        bitmap = _gdi32.CreateDIBSection(memory, ctypes.byref(info), 0, ctypes.byref(bits), None, 0)
        old = None
        try:
            if not bitmap:
                return
            ctypes.memmove(bits, pixels, len(pixels))
            old = _gdi32.SelectObject(memory, bitmap)
            blend = _blend(alpha)
            _user32.UpdateLayeredWindow(
                hwnd,
                screen,
                ctypes.byref(win32.POINT(strip.left, strip.top)),
                ctypes.byref(wintypes.SIZE(strip.width, strip.height)),
                memory,
                ctypes.byref(win32.POINT(0, 0)),
                0,
                ctypes.byref(blend),
                ULW_ALPHA,
            )
        finally:
            if old:
                _gdi32.SelectObject(memory, old)
            if bitmap:
                _gdi32.DeleteObject(bitmap)
            _gdi32.DeleteDC(memory)

    def _set_alpha(self, alpha: int) -> None:
        if alpha == self._alpha:
            return
        self._alpha = alpha
        blend = _blend(alpha)
        for hwnd in self._strips:
            _user32.UpdateLayeredWindow(hwnd, None, None, None, None, None, 0, ctypes.byref(blend), ULW_ALPHA)

    def _destroy_strips(self) -> None:
        while self._strips:
            hwnd = self._strips.pop()
            _routes.pop(hwnd, None)
            _user32.DestroyWindow(hwnd)
        self._built = False
        self._alpha = -1
