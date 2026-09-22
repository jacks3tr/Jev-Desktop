"""Windows driver edge cases exercised with fakes: no desktop input, COM, or system settings."""

import multiprocessing
import multiprocessing.spawn
import os
import sys
from types import SimpleNamespace as NS

import pytest

from jev_desktop.contracts import (
    Coverage,
    DriverError,
    Geometry,
    InputMode,
    Operation,
    Pause,
    Reason,
    Rect,
)
from jev_desktop.drivers import windows as windows_driver
from jev_desktop.drivers.windows import identity, uia, win32
from jev_desktop.drivers.windows import input as native_input
from jev_desktop.drivers.windows.native import NativeWindowsDriver


@pytest.fixture
def no_send_input(monkeypatch):
    def refuse(*_args):
        raise AssertionError("SendInput must not be reached")

    monkeypatch.setattr(win32.user32, "SendInput", refuse)


def _click_flags(monkeypatch, *, swapped, button="left"):
    sent = []
    monkeypatch.setattr(win32.user32, "GetSystemMetrics", lambda index: int(swapped) if index == 23 else 0)
    monkeypatch.setattr(win32, "normalize_absolute", lambda x, y: (x, y))
    monkeypatch.setattr(win32, "_send", lambda events: sent.extend(events) or len(events))
    win32.click_at(1, 2, button=button)
    return [event.mi.dwFlags for event in sent[1:]]


def test_click_maps_logical_buttons_through_swapped_mouse_buttons(monkeypatch, no_send_input):
    left = [win32.MOUSEEVENTF_LEFTDOWN, win32.MOUSEEVENTF_LEFTUP]
    right = [win32.MOUSEEVENTF_RIGHTDOWN, win32.MOUSEEVENTF_RIGHTUP]
    assert _click_flags(monkeypatch, swapped=False) == left
    assert _click_flags(monkeypatch, swapped=True) == right
    assert _click_flags(monkeypatch, swapped=True, button="right") == left
    assert _click_flags(monkeypatch, swapped=True, button="middle") == [
        win32.MOUSEEVENTF_MIDDLEDOWN,
        win32.MOUSEEVENTF_MIDDLEUP,
    ]


@pytest.mark.parametrize("flags", [win32.MOUSEEVENTF_LEFTDOWN, win32.MOUSEEVENTF_WHEEL])
def test_held_modifier_blocks_mouse_batches(monkeypatch, no_send_input, flags):
    monkeypatch.setattr(win32, "key_down", lambda vk: vk == 0x11)
    monkeypatch.setattr(win32, "normalize_absolute", lambda x, y: (x, y))
    with pytest.raises(Pause) as paused:
        win32._send_batch([win32._mouse_input(flags, 1, 1)])
    assert paused.value.reason is Reason.USER_TAKEOVER


def test_wheel_positive_notches_scroll_up_and_right(monkeypatch, no_send_input):
    sent = []
    monkeypatch.setattr(win32, "normalize_absolute", lambda x, y: (x, y))
    monkeypatch.setattr(win32, "_send", lambda events: sent.extend(events) or len(events))
    win32.scroll_wheel(0, 0, notches=3)
    win32.scroll_wheel(0, 0, notches=-1, horizontal=True)
    assert (sent[1].mi.dwFlags, sent[1].mi.mouseData) == (win32.MOUSEEVENTF_WHEEL, 360)
    assert (sent[3].mi.dwFlags, sent[3].mi.mouseData) == (win32.MOUSEEVENTF_HWHEEL, (-120) & 0xFFFFFFFF)


def test_destroyed_window_is_not_top_level(monkeypatch):
    monkeypatch.setattr(win32.user32, "GetAncestor", lambda *_: None)
    assert win32.is_top_level(1234) is False


def _stable_windows(monkeypatch, vanished):
    def rect(hwnd):
        if hwnd in vanished:
            raise DriverError("GetWindowRect failed (1400)")
        return Rect(0, 0, 100, 100)

    monkeypatch.setattr(win32, "window_rect", rect)
    monkeypatch.setattr(win32, "window_process_id", lambda _hwnd: 7)
    monkeypatch.setattr(win32, "is_top_level", lambda _hwnd: True)
    monkeypatch.setattr(win32, "is_owned_popup", lambda _hwnd: False)
    monkeypatch.setattr(win32, "is_cloaked", lambda _hwnd: False)
    monkeypatch.setattr(win32, "owner_window", lambda _hwnd: 0)
    monkeypatch.setattr(win32, "foreground_window", lambda: 0)
    monkeypatch.setattr(win32, "window_title", lambda _hwnd: "title")
    monkeypatch.setattr(win32, "window_class", lambda _hwnd: "class")
    monkeypatch.setattr(win32, "dpi_for_window", lambda _hwnd: 96)
    monkeypatch.setattr(win32.user32, "IsWindowVisible", lambda _hwnd: True)
    monkeypatch.setattr(win32.user32, "IsWindowEnabled", lambda _hwnd: True)


def test_discovery_skips_a_window_that_closes_mid_enumeration(monkeypatch):
    _stable_windows(monkeypatch, vanished={2})
    registry = uia.Registry()
    windows = uia.discover_windows(registry, app_ref="app", process_ids={7}, handles=[1, 2])
    assert [registry.windows[window.window_ref].hwnd for window in windows] == [1]


class _Element:
    def __init__(self, name):
        self.values = {uia.PROP_CONTROL_TYPE: 50000, uia.PROP_NAME: name, uia.PROP_BOUNDS: (0, 0, 10, 10)}

    def GetCachedPropertyValue(self, prop):
        return self.values.get(prop)


def test_vanished_subtree_and_window_leave_a_partial_snapshot(monkeypatch):
    _stable_windows(monkeypatch, vanished={2})
    root, child = _Element("root"), _Element("child")

    def children(element):
        if element is root:
            yield child
            raise DriverError("cached traversal failed: UIA_E_ELEMENTNOTAVAILABLE")

    worker = NS(
        children=children,
        element_from_handle=lambda _hwnd: root,
        has_cached_walker=True,
        focused_element=lambda _hwnd: None,
    )
    worker.submit = lambda fn, **_: fn(worker)
    snapshot = uia.observe(
        worker,
        uia.Registry(),
        app_ref="app",
        process_id=7,
        scope_windows=[("win:1", 1), ("win:2", 2)],
        max_elements=50,
        max_depth=4,
        text_limit=100,
        include_invisible=False,
        geometry=Geometry(1, 0, 0, 100, 100, 96, 1.0),
    )
    assert snapshot.coverage is Coverage.PARTIAL
    assert [window.window_ref for window in snapshot.windows] == ["win:1"]
    assert [element.name for element in snapshot.elements] == ["root", "child"]
    assert any("closed during observation" in note for note in snapshot.truncation)
    assert any("became unavailable" in note for note in snapshot.truncation)


@pytest.mark.parametrize(
    ("mode", "operation"),
    [(InputMode.USER_PATH, Operation.CLICK), (InputMode.SEMANTIC, Operation.HOTKEY)],
)
def test_live_target_checks_follow_authorization_immediately_before_input(monkeypatch, mode, operation):
    events = []
    driver = NativeWindowsDriver()
    driver._started = True
    monkeypatch.setattr(driver, "process_id_for", lambda _app: events.append("process") or 7)
    monkeypatch.setattr(driver, "window_handle", lambda _ref: 1)
    monkeypatch.setattr(win32, "window_process_id", lambda _hwnd: 7)
    monkeypatch.setattr(native_input, "_guard_foreground_app", lambda *_: events.append("foreground"))

    def execute(_driver, _request, guard, _snapshot):
        guard()
        events.append("|")
        win32._dispatch_guard()
        return "receipt"

    monkeypatch.setattr(native_input, "execute", execute)
    request = NS(mode=mode, operation=operation, window_ref="win:1")
    assert driver.execute(request, lambda: events.append("authorize"), NS(app_ref="app")) == "receipt"
    explicit = ["authorize", "process", "foreground"] if mode is InputMode.USER_PATH else ["authorize", "process"]
    assert events == [*explicit, "|", "authorize", "process", "foreground"]


def test_worker_is_the_tracked_process_inside_a_venv(monkeypatch, tmp_path):
    base = tmp_path / "base"
    scripts = tmp_path / "venv" / "Scripts"
    base.mkdir()
    (base / "pythonw.exe").write_bytes(b"")
    monkeypatch.setattr(multiprocessing.spawn, "_python_exe", multiprocessing.spawn.get_executable())
    monkeypatch.setattr(sys, "executable", str(scripts / "python.exe"))
    monkeypatch.setattr(sys, "_base_executable", str(base / "python.exe"), raising=False)
    monkeypatch.delenv("__PYVENV_LAUNCHER__", raising=False)
    started = []
    process = NS(
        start=lambda: started.append((multiprocessing.spawn.get_executable(), os.environ.get("__PYVENV_LAUNCHER__")))
    )
    windows_driver._start_worker_process(process)
    assert started == [(str(base / "pythonw.exe"), str(scripts / "pythonw.exe"))]
    assert "__PYVENV_LAUNCHER__" not in os.environ

    started.clear()
    monkeypatch.setattr(sys, "executable", str(base / "python.exe"))
    windows_driver._start_worker_process(process)
    assert started == [(str(base / "pythonw.exe"), None)]


def test_exe_hash_is_not_served_from_a_stale_cache(tmp_path):
    exe = tmp_path / "app.exe"
    exe.write_bytes(b"build-1")
    stamp = os.stat(exe).st_mtime_ns
    first = identity.sha256_file(str(exe))
    exe.write_bytes(b"build-2")
    os.utime(exe, ns=(stamp, stamp))
    assert identity.sha256_file(str(exe)) != first
