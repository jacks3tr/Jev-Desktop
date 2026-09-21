"""ctypes Win32 layer for the Windows driver.

Explicit declarations only: every function used by the driver is declared here with
argtypes/restypes and checked return values. No third-party input or automation layer
sits in front of `SendInput`, GDI capture, or window/process queries.
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

from ...contracts import DriverError, Rect

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
SM_CXSCREEN, SM_CYSCREEN = 0, 1

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_ABSOLUTE = 0x8000

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

GA_PARENT, GA_ROOT, GA_ROOTOWNER = 1, 2, 3

DWMWA_CLOAKED = 14
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
DPI_AWARENESS_CONTEXT_SYSTEM_AWARE = ctypes.c_void_p(-2)

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
GW_OWNER = 4
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080

MAPVK_VK_TO_VSC = 0

VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN = 0x10, 0x11, 0x12, 0x5B
SW_RESTORE = 9
SW_SHOW = 5
SPI_GETWORKAREA = 0x0030
HWND_TOP = 0
HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_SHOWWINDOW = 0x0040
WS_EX_TOPMOST = 0x00000008

# ---------------------------------------------------------------------------
# Structures
# ---------------------------------------------------------------------------


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG), ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

    def as_rect(self) -> Rect:
        return Rect(self.left, self.top, self.right, self.bottom)


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", RECT),
        ("rcWork", RECT),
        ("dwFlags", wintypes.DWORD),
    ]


# ---------------------------------------------------------------------------
# Prototypes
# ---------------------------------------------------------------------------

user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.BringWindowToTop.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindow.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.IsWindowEnabled.argtypes = [wintypes.HWND]
user32.IsWindowEnabled.restype = wintypes.BOOL
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
user32.GetClientRect.restype = wintypes.BOOL
user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(POINT)]
user32.ClientToScreen.restype = wintypes.BOOL
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetWindow.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
user32.GetCursorPos.restype = wintypes.BOOL
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.SetCursorPos.restype = wintypes.BOOL
user32.WindowFromPoint.argtypes = [POINT]
user32.WindowFromPoint.restype = wintypes.HWND
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.AttachThreadInput.restype = wintypes.BOOL
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.MonitorFromWindow.restype = wintypes.HANDLE
user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFO)]
user32.GetMonitorInfoW.restype = wintypes.BOOL
user32.GetDpiForWindow.argtypes = [wintypes.HWND]
user32.GetDpiForWindow.restype = wintypes.UINT
user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
user32.SetProcessDPIAware.restype = wintypes.BOOL
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.ReleaseDC.restype = ctypes.c_int
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = wintypes.BOOL
user32.SendMessageTimeoutW.argtypes = [
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
    wintypes.UINT,
    wintypes.UINT,
    ctypes.POINTER(ctypes.c_size_t),
]
user32.SendMessageTimeoutW.restype = wintypes.LPARAM
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.SetWindowPos.argtypes = [
    wintypes.HWND,
    wintypes.HWND,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.UINT,
]
user32.SetWindowPos.restype = wintypes.BOOL
user32.PeekMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG),
    wintypes.HWND,
    wintypes.UINT,
    wintypes.UINT,
    wintypes.UINT,
]
user32.PeekMessageW.restype = wintypes.BOOL
user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
user32.MapVirtualKeyW.restype = wintypes.UINT
user32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT]
user32.SystemParametersInfoW.restype = wintypes.BOOL

kernel32.GetCurrentProcess.restype = wintypes.HANDLE
kernel32.GetCurrentProcessId.restype = wintypes.DWORD
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.GetProcessTimes.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(FILETIME),
    ctypes.POINTER(FILETIME),
    ctypes.POINTER(FILETIME),
    ctypes.POINTER(FILETIME),
]
kernel32.GetProcessTimes.restype = wintypes.BOOL
kernel32.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
kernel32.ProcessIdToSessionId.restype = wintypes.BOOL
kernel32.SetEvent.argtypes = [wintypes.HANDLE]
kernel32.SetEvent.restype = wintypes.BOOL
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateEventW.restype = wintypes.HANDLE
kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.OpenEventW.restype = wintypes.HANDLE
kernel32.ResetEvent.argtypes = [wintypes.HANDLE]
kernel32.ResetEvent.restype = wintypes.BOOL

gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateDIBSection.argtypes = [
    wintypes.HDC,
    ctypes.POINTER(BITMAPINFO),
    wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p),
    wintypes.HANDLE,
    wintypes.DWORD,
]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.BitBlt.argtypes = [
    wintypes.HDC,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HDC,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.DWORD,
]
gdi32.BitBlt.restype = wintypes.BOOL
gdi32.StretchBlt.argtypes = [
    wintypes.HDC,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HDC,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.DWORD,
]
gdi32.StretchBlt.restype = wintypes.BOOL
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteObject.restype = wintypes.BOOL
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.DeleteDC.restype = wintypes.BOOL
gdi32.SetStretchBltMode.argtypes = [wintypes.HDC, ctypes.c_int]
gdi32.SetStretchBltMode.restype = ctypes.c_int

SRCCOPY = 0x00CC0020
HALFTONE = 4
CAPTUREBLT = 0x40000000

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ULONG_PTR = ctypes.POINTER(ctypes.c_ulong)
_NULL_EXTRA = _ULONG_PTR()


def set_dpi_awareness() -> str:
    """Per-monitor DPI awareness, falling back to system awareness. Returns the mode used."""
    try:
        if user32.SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2):
            return "per_monitor_v2"
    except AttributeError:  # pragma: no cover - older Windows
        pass
    if user32.SetProcessDPIAware():
        return "system"
    return "none"


def virtual_screen() -> Rect:
    return Rect(
        user32.GetSystemMetrics(SM_XVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_YVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_XVIRTUALSCREEN) + user32.GetSystemMetrics(SM_CXVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_YVIRTUALSCREEN) + user32.GetSystemMetrics(SM_CYVIRTUALSCREEN),
    )


def normalize_absolute(x: int, y: int) -> tuple[int, int]:
    """Screen coordinates -> 0..65535 absolute mouse coordinates over the virtual desktop."""
    screen = virtual_screen()
    width = max(screen.width - 1, 1)
    height = max(screen.height - 1, 1)
    nx = round((x - screen.left) * 65535 / width)
    ny = round((y - screen.top) * 65535 / height)
    return max(0, min(65535, nx)), max(0, min(65535, ny))


def _send(inputs: list[INPUT]) -> int:
    if not inputs:
        return 0
    array = (INPUT * len(inputs))(*inputs)
    ctypes.set_last_error(0)
    inserted = user32.SendInput(len(inputs), array, ctypes.sizeof(INPUT))
    if inserted != len(inputs):
        raise DriverError(f"SendInput inserted {inserted}/{len(inputs)} events ({ctypes.get_last_error()})")
    return int(inserted)


def _mouse_input(flags: int, x: int = 0, y: int = 0, data: int = 0) -> INPUT:
    nx, ny = normalize_absolute(x, y)
    return INPUT(
        type=INPUT_MOUSE,
        mi=MOUSEINPUT(nx, ny, ctypes.c_ulong(data & 0xFFFFFFFF).value, flags, 0, _NULL_EXTRA),
    )


def _key_input(vk: int, flags: int, scan: int = 0) -> INPUT:
    return INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(vk, scan, flags, 0, _NULL_EXTRA))


def move_mouse(x: int, y: int) -> int:
    return _send([_mouse_input(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, x, y)])


# Measured: on a standard Win32 control both a single-batch press and a split press register
# every trial, so this hold is a hedge for applications that sample the physical button state
# rather than a requirement. Ten milliseconds costs about one percent of an action.
CLICK_PRESS_SECONDS = 0.01
CLICK_BATCHED = False


def click_at(
    x: int,
    y: int,
    *,
    double: bool = False,
    button: str = "left",
    press_seconds: float | None = None,
    batched: bool = False,
) -> int:
    """Move, press, hold briefly, release.

    Press and release go in separate SendInput batches with a short hold between them.
    Measured with `scripts/calibrate_thresholds.py`: the split batching is what makes a
    control register the click, and the hold keeps the physical button state observable to
    applications that inspect it. `batched=True` reproduces the single-batch behaviour so the
    difference stays measurable.
    """
    down, up = {
        "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
        "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
        "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
    }[button]
    hold = CLICK_PRESS_SECONDS if press_seconds is None else max(0.0, press_seconds)
    batched = batched or CLICK_BATCHED
    count = move_mouse(x, y)
    for _ in range(2 if double else 1):
        if batched:
            count += _send([_mouse_input(down, x, y), _mouse_input(up, x, y)])
        else:
            count += _send([_mouse_input(down, x, y)])
            if hold:
                time.sleep(hold)
            count += _send([_mouse_input(up, x, y)])
        if double and hold:
            time.sleep(hold)
    return count


def scroll_wheel(x: int, y: int, *, notches: int, horizontal: bool = False) -> int:
    count = move_mouse(x, y)
    data = (int(notches) * 120) & 0xFFFFFFFF
    count += _send([_mouse_input(MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL, x, y, data)])
    return count


def type_unicode(text: str) -> int:
    """Type text as Unicode key events (surrogate pairs split into two units)."""
    events: list[INPUT] = []
    for char in text:
        unit = ord(char)
        if unit > 0xFFFF:  # split into UTF-16 surrogate pair
            unit -= 0x10000
            units = [0xD800 + (unit >> 10), 0xDC00 + (unit & 0x3FF)]
        else:
            units = [unit]
        for code in units:
            events.append(_key_input(0, KEYEVENTF_UNICODE, code))
            events.append(_key_input(0, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, code))
    return _send(events)


_EXTENDED_VKS = {
    0x21,
    0x22,
    0x23,
    0x24,
    0x25,
    0x26,
    0x27,
    0x28,
    0x2D,
    0x2E,
    0x5B,
    0x5C,
    0x5D,
    0x6F,
}


def key_chord(vk_codes: list[int]) -> int:
    """Press modifiers in order, tap the final key, release in reverse order."""
    if not vk_codes:
        return 0
    events: list[INPUT] = []
    for vk in vk_codes[:-1]:
        events.append(_key_input(vk, KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED_VKS else 0))
    last = vk_codes[-1]
    events.append(_key_input(last, KEYEVENTF_EXTENDEDKEY if last in _EXTENDED_VKS else 0))
    events.append(_key_input(last, KEYEVENTF_KEYUP | (KEYEVENTF_EXTENDEDKEY if last in _EXTENDED_VKS else 0)))
    for vk in reversed(vk_codes[:-1]):
        events.append(_key_input(vk, KEYEVENTF_KEYUP | (KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED_VKS else 0)))
    return _send(events)


def window_title(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(max(length + 1, 2))
    user32.GetWindowTextW(hwnd, buffer, len(buffer))
    return buffer.value


def window_class(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buffer, len(buffer))
    return buffer.value


def window_rect(hwnd: int) -> Rect:
    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise DriverError(f"GetWindowRect failed ({ctypes.get_last_error()})")
    return rect.as_rect()


def is_cloaked(hwnd: int) -> bool:
    """UWP/ghost windows report visible but are cloaked."""
    try:
        dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
    except OSError:  # pragma: no cover
        return False
    value = ctypes.c_int(0)
    dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    result = dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(value), ctypes.sizeof(value))
    return result == 0 and value.value != 0


def owner_window(hwnd: int) -> int:
    return int(user32.GetWindow(hwnd, GW_OWNER) or 0)


def root_window(hwnd: int) -> int:
    return int(user32.GetAncestor(hwnd, GA_ROOT) or hwnd)


def foreground_window() -> int:
    return int(user32.GetForegroundWindow() or 0)


def window_process_id(hwnd: int) -> int:
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def window_thread_id(hwnd: int) -> int:
    return int(user32.GetWindowThreadProcessId(hwnd, None))


def cursor_position() -> tuple[int, int]:
    point = POINT()
    if not user32.GetCursorPos(ctypes.byref(point)):
        raise DriverError(f"GetCursorPos failed ({ctypes.get_last_error()})")
    return point.x, point.y


def set_cursor_position(x: int, y: int) -> None:
    user32.SetCursorPos(x, y)


def window_from_point(x: int, y: int) -> int:
    return int(user32.WindowFromPoint(POINT(x, y)) or 0)


def activate_window(hwnd: int) -> bool:
    """Best-effort activation: restore, attach input queues, then foreground.

    Activation is verified, never assumed, and the caller decides what a failure means.
    """
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    if _is_foreground_root(hwnd):
        return True
    current_thread = kernel32.GetCurrentThreadId()
    # A thread needs an input queue before it can attach to another input queue.
    message = wintypes.MSG()
    user32.PeekMessageW(ctypes.byref(message), 0, 0, 0, 0)
    for attempt in range(4):
        target_thread = window_thread_id(hwnd)
        foreground_thread = window_thread_id(foreground_window()) if foreground_window() else 0
        attached: list[int] = []
        for thread in {target_thread, foreground_thread}:
            if thread and thread != current_thread and user32.AttachThreadInput(current_thread, thread, True):
                attached.append(thread)
        try:
            user32.SetWindowPos(hwnd, HWND_TOP, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        finally:
            for thread in attached:
                user32.AttachThreadInput(current_thread, thread, False)
        if _is_foreground_root(hwnd):
            return True
        time.sleep(0.05 * (attempt + 1))
    return _is_foreground_root(hwnd)


def _is_foreground_root(hwnd: int) -> bool:
    foreground = foreground_window()
    return bool(foreground) and root_window(foreground) == root_window(hwnd)


def dpi_for_window(hwnd: int) -> int:
    dpi = user32.GetDpiForWindow(hwnd)
    return int(dpi) if dpi else 96


def monitor_work_area(hwnd: int) -> Rect:
    monitor = user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    if monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return Rect(info.rcMonitor.left, info.rcMonitor.top, info.rcMonitor.right, info.rcMonitor.bottom)
    return virtual_screen()


def enum_top_level_windows() -> list[int]:
    handles: list[int] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _callback(hwnd: int, _param: int) -> bool:
        handles.append(int(hwnd))
        return True

    callback = callback_type(_callback)
    user32.EnumWindows(callback, 0)
    return handles


def is_top_level(hwnd: int) -> bool:
    return int(user32.GetAncestor(hwnd, GA_ROOT)) == int(hwnd)


def is_owned_popup(hwnd: int) -> bool:
    ex_style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    return bool(ex_style & WS_EX_TOOLWINDOW)


def process_image_path(pid: int) -> str:
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        raise DriverError(f"OpenProcess({pid}) failed ({ctypes.get_last_error()})")
    try:
        size = wintypes.DWORD(4096)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            raise DriverError(f"QueryFullProcessImageNameW({pid}) failed ({ctypes.get_last_error()})")
        return buffer.value
    finally:
        kernel32.CloseHandle(handle)


def process_command_line(pid: int) -> str:
    """Command line of a process, read through WMI-free Toolhelp-free means: the PEB is not
    accessible from outside, so this uses the documented WMI provider interface."""
    import subprocess as _subprocess

    result = _subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            f"(Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}').CommandLine",
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    return result.stdout.strip()


def process_creation_time(pid: int) -> float:
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        raise DriverError(f"OpenProcess({pid}) failed ({ctypes.get_last_error()})")
    try:
        creation, exit_time, kernel_time, user_time = FILETIME(), FILETIME(), FILETIME(), FILETIME()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            raise DriverError(f"GetProcessTimes({pid}) failed ({ctypes.get_last_error()})")
        ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        return ticks / 10_000_000 - 11644473600.0
    finally:
        kernel32.CloseHandle(handle)


def key_down(vk: int) -> bool:
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


# ---------------------------------------------------------------------------
# Process enumeration and packaged-app identity
# ---------------------------------------------------------------------------

TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE_PTR = ctypes.c_void_p(-1).value
ERROR_NO_MORE_FILES = 18


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32FirstW.restype = wintypes.BOOL
kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32NextW.restype = wintypes.BOOL
kernel32.GetPackageFamilyName.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.UINT), wintypes.LPWSTR]
kernel32.GetPackageFamilyName.restype = ctypes.c_long


def iter_process_ids() -> list[int]:
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == INVALID_HANDLE_VALUE_PTR:
        raise DriverError(f"CreateToolhelp32Snapshot failed ({ctypes.get_last_error()})")
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        pids: list[int] = []
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return pids
        while True:
            pids.append(int(entry.th32ProcessID))
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
        return pids
    finally:
        kernel32.CloseHandle(snapshot)


def process_package_family(pid: int) -> str | None:
    """Package family name for a packaged process, or None for a desktop process."""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        length = wintypes.UINT(0)
        kernel32.GetPackageFamilyName(handle, ctypes.byref(length), None)
        if not length.value:
            return None
        buffer = ctypes.create_unicode_buffer(length.value)
        if kernel32.GetPackageFamilyName(handle, ctypes.byref(length), buffer) != 0:
            return None
        return buffer.value or None
    finally:
        kernel32.CloseHandle(handle)
