"""Real overlay windows on the interactive desktop. No input is injected; hooks only observe."""

from __future__ import annotations

import time

import pytest

from jev_desktop.drivers.windows import presence, win32

pytestmark = pytest.mark.windows


def _wait(condition, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.02)
    return True


def test_overlay_is_click_through_and_cleaned_up():
    watch = presence.DesktopPresence(linger_s=0.1)
    watch.start()
    try:
        watch.set_active(True)
        assert _wait(lambda: watch._strips and watch._alpha == 255)
        assert len(watch._hooks) == 2
        strips = list(watch._strips)
        for hwnd in strips:
            assert win32.window_class(hwnd) == win32.PRESENCE_CLASS
            assert win32.user32.IsWindowVisible(hwnd)
            ex_style = win32.user32.GetWindowLongPtrW(hwnd, win32.GWL_EXSTYLE)
            assert ex_style & presence.OVERLAY_EX_STYLE == presence.OVERLAY_EX_STYLE
            rect = win32.window_rect(hwnd)
            assert win32.root_window(win32.window_from_point(rect.left + 1, rect.top + 1)) not in strips
        assert win32.foreground_window() not in strips
        assert not set(strips) & set(win32.enum_top_level_windows())

        watch.set_active(False)
        assert _wait(lambda: not watch._hooks)
        assert _wait(lambda: not watch._strips)
        assert not any(win32.user32.IsWindow(hwnd) for hwnd in strips)
    finally:
        watch.close()
    assert watch._thread is None
