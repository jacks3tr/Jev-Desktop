"""Desktop presence logic with no windows, hooks, or input: field, classification, timeline."""

import ctypes
import itertools
import math
import threading

import pytest

from jev_desktop.contracts import Rect
from jev_desktop.drivers import windows as windows_driver
from jev_desktop.drivers.windows import presence, win32
from jev_desktop.drivers.windows.presence import (
    FADE_IN_S,
    FADE_OUT_S,
    PEAK_ALPHA,
    VK_ESCAPE,
    WM_KEYDOWN,
    WM_KEYUP,
    WM_LBUTTONDOWN,
    WM_MBUTTONDOWN,
    WM_MOUSEHWHEEL,
    WM_MOUSEMOVE,
    WM_MOUSEWHEEL,
    WM_RBUTTONDOWN,
    WM_SYSKEYDOWN,
    WM_XBUTTONDOWN,
    DesktopPresence,
    Fade,
    classify,
    render_strip,
    vignette_alpha,
    vignette_depth,
    vignette_strips,
)

# -- vignette field -----------------------------------------------------------------------


def _field(monitor: Rect, depth: int, x: int, y: int) -> float:
    dx = min(x - monitor.left, monitor.right - 1 - x)
    dy = min(y - monitor.top, monitor.bottom - 1 - y)
    return vignette_alpha(dx, dy, depth)


def test_glow_peaks_at_the_edge_and_eases_to_nothing():
    depth = 20
    profile = [vignette_alpha(dx, 500, depth) for dx in range(depth + 5)]
    assert 0.30 <= profile[0] <= 0.35
    assert all(a > b for a, b in itertools.pairwise(profile[:depth]))
    assert profile[depth - 1] < 0.002
    assert profile[depth:] == [0.0] * 5


def test_corners_round_off_without_doubling_or_creasing():
    depth = 16
    corner = vignette_alpha(0, 0, depth)
    assert vignette_alpha(0, 500, depth) <= corner <= PEAK_ALPHA
    monitor = Rect(0, 0, 64, 48)
    field = [[_field(monitor, depth, x, y) for x in range(64)] for y in range(48)]
    step = max(
        max(abs(row[x + 1] - row[x]) for row in field for x in range(63)),
        max(abs(field[y + 1][x] - field[y][x]) for y in range(47) for x in range(64)),
    )
    assert step <= PEAK_ALPHA * (2 + 1 / depth) / depth
    # Symmetric about the corner diagonal: no seam where the top and left strips meet.
    assert all(field[y][x] == field[x][y] for x in range(depth + 2) for y in range(depth + 2))


def test_depth_scales_with_monitor_dpi_and_fits_small_monitors():
    screen = Rect(0, 0, 3840, 2160)
    assert [vignette_depth(screen, dpi) for dpi in (96, 120, 144, 192)] == [20, 25, 30, 40]
    assert vignette_depth(Rect(0, 0, 30, 18), 96) == 9


@pytest.mark.parametrize(
    ("monitor", "dpi"),
    [(Rect(0, 0, 90, 60), 96), (Rect(-75, -20, 0, 41), 144), (Rect(100, 200, 131, 219), 96)],
)
def test_strips_tile_the_monitor_band_and_reproduce_the_full_field(monitor, dpi):
    depth = vignette_depth(monitor, dpi)
    covered: dict[tuple[int, int], bytes] = {}
    for strip in vignette_strips(monitor, depth):
        assert strip.intersect(monitor) == strip
        pixels = render_strip(monitor, strip, depth)
        assert len(pixels) == strip.width * strip.height * 4
        for index in range(strip.width * strip.height):
            point = (strip.left + index % strip.width, strip.top + index // strip.width)
            assert point not in covered, f"strips overlap at {point}"
            covered[point] = pixels[index * 4 : index * 4 + 4]
    for y in range(monitor.top, monitor.bottom):
        for x in range(monitor.left, monitor.right):
            alpha = _field(monitor, depth, x, y)
            if (x, y) in covered:
                assert covered[(x, y)] == presence._pixel(alpha)
            else:
                assert alpha == 0.0, f"uncovered pixel {(x, y)} should glow"


def test_pixels_are_premultiplied_glow_blue():
    blue, green, red, alpha = presence._pixel(vignette_alpha(0, 500, 20))
    assert 0.30 * 255 <= alpha <= 0.35 * 255
    assert (red, green, blue) == tuple(round(c * alpha / 255) for c in presence.COLOR)
    assert presence._pixel(0.0) == bytes(4)


# -- hook classification ------------------------------------------------------------------

WM_LBUTTONUP = 0x0202
INJECTED_MOUSE = presence.LLMHF_INJECTED
LOWER_IL_MOUSE = presence.LLMHF_LOWER_IL_INJECTED
INJECTED_KEY = presence.LLKHF_INJECTED
LOWER_IL_KEY = presence.LLKHF_LOWER_IL_INJECTED


TAG = win32.JEV_INPUT_TAG


@pytest.mark.parametrize(
    ("message", "vk", "flags", "extra", "kind"),
    [
        *[(m, 0, 0, 0, "human") for m in (WM_LBUTTONDOWN, WM_RBUTTONDOWN, WM_MBUTTONDOWN, WM_XBUTTONDOWN)],
        (WM_MOUSEWHEEL, 0, 0, 0, "human"),
        (WM_MOUSEHWHEEL, 0, 0, 0, "human"),
        (WM_MOUSEMOVE, 0, 0, 0, "ignore"),
        (WM_MOUSEMOVE, 0, INJECTED_MOUSE, 0, "ignore"),
        (WM_LBUTTONUP, 0, 0, 0, "ignore"),
        (WM_LBUTTONDOWN, 0, INJECTED_MOUSE, TAG, "ignore"),
        (WM_MOUSEWHEEL, 0, LOWER_IL_MOUSE | INJECTED_MOUSE, TAG, "ignore"),
        (WM_KEYDOWN, 0x41, 0, 0, "human"),
        (WM_SYSKEYDOWN, 0x12, 0x20, 0, "human"),  # LLKHF_ALTDOWN is not injection
        (WM_KEYUP, 0x41, 0x80, 0, "ignore"),
        (WM_KEYDOWN, 0x41, INJECTED_KEY, TAG, "ignore"),
        (WM_KEYDOWN, 0x41, LOWER_IL_KEY, TAG, "ignore"),
        (WM_KEYDOWN, VK_ESCAPE, 0, 0, "escape"),
        (WM_SYSKEYDOWN, VK_ESCAPE, 0, 0, "escape"),
        (WM_KEYDOWN, VK_ESCAPE, INJECTED_KEY, TAG, "ignore"),
        (WM_KEYUP, VK_ESCAPE, 0x80, 0, "ignore"),
        # The tag alone is not ours: a physical event carrying it is still a person.
        (WM_LBUTTONDOWN, 0, 0, TAG, "human"),
        (WM_KEYDOWN, 0x41, 0, TAG, "human"),
    ],
)
def test_classify(message, vk, flags, extra, kind):
    assert classify(message, vk, flags, extra) == kind


@pytest.mark.parametrize("extra", [0, 1, TAG + 1])
def test_input_injected_by_another_program_is_a_person(extra):
    """Chrome Remote Desktop, AnyDesk, TeamViewer, and Parsec deliver their user's input via SendInput."""
    assert classify(WM_LBUTTONDOWN, 0, INJECTED_MOUSE, extra) == "human"
    assert classify(WM_MOUSEWHEEL, 0, INJECTED_MOUSE | LOWER_IL_MOUSE, extra) == "human"
    assert classify(WM_KEYDOWN, 0x41, INJECTED_KEY, extra) == "human"
    assert classify(WM_KEYDOWN, VK_ESCAPE, INJECTED_KEY, extra) == "escape"
    assert classify(WM_LBUTTONDOWN, 0, INJECTED_MOUSE, TAG) == "ignore"
    assert classify(WM_KEYDOWN, VK_ESCAPE, INJECTED_KEY, TAG) == "ignore"


class _Clock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_physical_input_bumps_the_epoch_and_resets_idle_time():
    clock = _Clock()
    watch = DesktopPresence()
    watch._clock = clock
    assert (watch.human_epoch, watch.idle_seconds()) == (0, math.inf)
    watch._mouse_event(WM_MOUSEMOVE, 0, 0)
    watch._mouse_event(WM_LBUTTONDOWN, INJECTED_MOUSE, TAG)
    watch._key_event(WM_KEYDOWN, 0x41, INJECTED_KEY, TAG)
    assert (watch.human_epoch, watch.idle_seconds()) == (0, math.inf)
    watch._mouse_event(WM_MOUSEWHEEL, 0, 0)
    clock.now += 4.5
    assert (watch.human_epoch, watch.idle_seconds()) == (1, 4.5)
    watch._key_event(WM_KEYDOWN, 0x41, 0, 0)
    watch._key_event(WM_KEYDOWN, 0x41, 0, 0)  # auto-repeat is still a person typing
    watch._mouse_event(WM_LBUTTONDOWN, INJECTED_MOUSE, 0)  # a remote-desktop click
    assert (watch.human_epoch, watch.idle_seconds()) == (4, 0.0)


def test_escape_fires_once_per_press_and_never_counts_as_takeover():
    watch = DesktopPresence()
    for _ in range(3):  # held key auto-repeats
        watch._key_event(WM_KEYDOWN, VK_ESCAPE, 0, 0)
    watch._key_event(WM_KEYUP, VK_ESCAPE, 0x80, 0)
    watch._key_event(WM_KEYDOWN, VK_ESCAPE, INJECTED_KEY, TAG)  # the plugin's own Esc hotkey
    watch._key_event(WM_KEYUP, VK_ESCAPE, INJECTED_KEY | 0x80, TAG)
    watch._key_event(WM_KEYDOWN, VK_ESCAPE, INJECTED_KEY, 0)  # a remote-desktop user's Esc
    assert watch.human_epoch == 0
    assert [watch._escapes.get_nowait() for _ in range(watch._escapes.qsize())] == [True, True]


def test_escape_callback_runs_on_the_dispatcher_thread_and_survives_errors():
    watch = DesktopPresence()
    calls: list[threading.Thread] = []
    done = threading.Event()

    def on_escape():
        calls.append(threading.current_thread())
        if len(calls) == 1:
            raise RuntimeError("broken handler")
        done.set()

    watch.on_escape = on_escape
    dispatcher = threading.Thread(target=watch._dispatch_escapes, daemon=True)
    dispatcher.start()
    watch._key_event(WM_KEYDOWN, VK_ESCAPE, 0, 0)
    watch._key_event(WM_KEYUP, VK_ESCAPE, 0x80, 0)
    watch._key_event(WM_KEYDOWN, VK_ESCAPE, 0, 0)
    assert done.wait(5)
    assert calls == [dispatcher, dispatcher]
    watch._escapes.put(False)
    dispatcher.join(5)
    assert not dispatcher.is_alive()


def test_hook_callbacks_always_chain(monkeypatch):
    chained = []
    monkeypatch.setattr(presence._user32, "CallNextHookEx", lambda *args: chained.append(args[1:3]) or 7)
    watch = DesktopPresence()
    key = presence.KBDLLHOOKSTRUCT(vkCode=0x41, flags=INJECTED_KEY, dwExtraInfo=TAG)
    mouse = presence.MSLLHOOKSTRUCT(flags=0)
    assert watch._on_key(0, WM_KEYDOWN, ctypes.addressof(key)) == 7
    assert watch._on_mouse(0, WM_LBUTTONDOWN, ctypes.addressof(mouse)) == 7
    assert watch._on_mouse(-1, WM_LBUTTONDOWN, 0) == 7  # nCode < 0: pass straight through
    monkeypatch.setattr(watch, "_key_event", lambda *_: 1 / 0)
    assert watch._on_key(0, WM_KEYDOWN, ctypes.addressof(key)) == 7  # a failing handler never swallows input
    assert chained == [(0, WM_KEYDOWN), (0, WM_LBUTTONDOWN), (-1, WM_LBUTTONDOWN), (0, WM_KEYDOWN)]
    assert watch.human_epoch == 1


def test_unstarted_presence_is_inert():
    watch = DesktopPresence()
    watch.set_active(True)
    watch.set_active(False)
    watch.close()
    watch.close()
    assert watch.human_epoch == 0


# -- fade timeline ------------------------------------------------------------------------


def test_fade_in_hold_linger_and_fade_out():
    fade = Fade(linger_s=2.0)
    assert (fade.opacity(0.0), fade.next_change(0.0)) == (0.0, None)
    fade.set_active(True, 10.0)
    assert fade.opacity(10.0) == 0.0 and fade.next_change(10.0) == 0.0
    assert 0.0 < fade.opacity(10.0 + FADE_IN_S / 2) < 1.0
    assert fade.opacity(10.0 + FADE_IN_S) == 1.0 and fade.next_change(10.0 + FADE_IN_S) is None
    fade.set_active(False, 20.0)
    assert fade.opacity(21.9) == 1.0
    assert fade.next_change(21.0) == pytest.approx(1.0)
    assert 0.0 < fade.opacity(22.0 + FADE_OUT_S / 2) < 1.0
    assert fade.next_change(22.0 + FADE_OUT_S / 2) == 0.0
    assert fade.opacity(22.0 + FADE_OUT_S) == 0.0 and fade.next_change(22.0 + FADE_OUT_S) is None


def test_reactivation_during_linger_cancels_the_fade():
    fade = Fade(linger_s=2.0)
    fade.set_active(True, 0.0)
    fade.set_active(False, 5.0)
    fade.set_active(True, 6.0)
    assert [fade.opacity(t) for t in (6.0, 7.5, 50.0)] == [1.0, 1.0, 1.0]
    assert fade.next_change(6.0) is None


def test_reactivation_mid_fade_out_reverses_smoothly():
    fade = Fade(linger_s=0.5)
    fade.set_active(True, 0.0)
    fade.set_active(False, 1.0)
    midway = 1.5 + FADE_OUT_S / 2
    level = fade.opacity(midway)
    assert 0.0 < level < 1.0
    fade.set_active(True, midway)
    assert fade.opacity(midway) == pytest.approx(level)
    assert level < fade.opacity(midway + 0.01) < 1.0
    assert fade.opacity(midway + FADE_IN_S * (1 - level)) == 1.0


def test_deactivation_mid_fade_in_finishes_rising_before_the_linger_ends():
    fade = Fade(linger_s=1.0)
    fade.set_active(True, 0.0)
    fade.set_active(False, FADE_IN_S / 4)
    assert fade.next_change(FADE_IN_S / 4) == 0.0
    assert fade.opacity(0.9) == 1.0
    assert fade.opacity(FADE_IN_S / 4 + 1.0 + FADE_OUT_S) == 0.0


# -- driver integration -------------------------------------------------------------------


def test_enumeration_leaves_out_the_presence_overlay(monkeypatch):
    classes = {1: "Notepad", 2: win32.PRESENCE_CLASS, 3: "Shell_TrayWnd", 4: win32.PRESENCE_CLASS}
    monkeypatch.setattr(win32.user32, "EnumWindows", lambda callback, param: all(callback(h, param) for h in classes))
    monkeypatch.setattr(win32, "window_class", classes.__getitem__)
    assert win32.enum_top_level_windows() == [1, 3]


def test_windows_driver_owns_an_unstarted_presence():
    driver = windows_driver.WindowsDriver()
    assert isinstance(driver.presence, DesktopPresence)
    assert driver.presence._thread is None
    driver.close()
