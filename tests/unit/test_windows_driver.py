"""Windows driver edge cases exercised with fakes: no desktop input, COM, or system settings."""

import multiprocessing
import multiprocessing.spawn
import os
import sys
from types import SimpleNamespace as NS

import pytest

from jev_desktop.contracts import (
    Capture,
    ContractError,
    Coverage,
    DriverError,
    EvidenceRef,
    Geometry,
    InputMode,
    Operation,
    Pause,
    Reason,
    Rect,
    ScreenshotPoint,
)
from jev_desktop.drivers import windows as windows_driver
from jev_desktop.drivers.windows import capture, identity, uia, win32
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


@pytest.mark.parametrize("overlap", [False, True], ids=["taskbar-under-invisible-border", "real-cover"])
def test_maximized_window_capture_ignores_invisible_resize_borders(monkeypatch, tmp_path, overlap):
    edge, tray = 1, 2
    geometry = Geometry(1, 0, 0, 2560, 1440, 96, 1.0)
    maximized = Rect(-8, -8, 2568, 1408)  # GetWindowRect of a maximized window
    frames = {edge: Rect(0, 0, 2560, 1400), tray: Rect(0, 1390 if overlap else 1400, 2560, 1440)}
    window = NS(window_ref="win:1", rect=maximized)
    captured = []
    driver = NativeWindowsDriver(evidence_dir=tmp_path)
    driver._started = True
    driver.screen = NS(
        geometry=lambda: geometry,
        capture_to=lambda _path, **kwargs: (
            captured.append(kwargs["region"])
            or (NS(evidence=NS(evidence_id="ev:1"), source_rect=kwargs["region"]), bytes(2560 * 1400 * 4))
        ),
    )
    monkeypatch.setattr(driver, "snapshot", lambda *_: NS(elements=[], geometry=geometry, windows=[window]))
    monkeypatch.setattr(driver, "_observe_windows", lambda _app: [window])
    monkeypatch.setattr(driver, "_scoped_windows", lambda _scope, windows: windows)
    monkeypatch.setattr(driver, "window_handle", lambda _ref: edge)
    monkeypatch.setattr(win32, "root_window", lambda hwnd: hwnd)
    monkeypatch.setattr(win32, "foreground_window", lambda: edge)
    monkeypatch.setattr(win32, "window_rect", lambda hwnd: maximized if hwnd == edge else frames[tray])
    monkeypatch.setattr(win32, "frame_rect", frames.__getitem__, raising=False)
    monkeypatch.setattr(win32, "enum_top_level_windows", lambda: [tray, edge])  # the taskbar is topmost
    monkeypatch.setattr(win32, "is_cloaked", lambda _hwnd: False)
    monkeypatch.setattr(win32.user32, "IsWindowVisible", lambda _hwnd: True)
    scope = NS(app_ref="app:1")
    if overlap:
        with pytest.raises(DriverError, match="covers the approved capture region"):
            driver.capture(scope=scope, snapshot_id="snap:1", run_id="run:1")
    else:
        driver.capture(scope=scope, snapshot_id="snap:1", run_id="run:1")
        assert captured == [frames[edge]]


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


def test_coordinate_input_tolerates_change_away_from_the_point(monkeypatch):
    geometry = Geometry(1, 0, 0, 1920, 1080, 96, 1.0)
    source = Rect(0, 0, 320, 160)
    evidence = EvidenceRef(
        "ev:" + "1" * 24,
        "run:" + "2" * 24,
        "screenshot",
        "unused.png",
        "image/png",
        "unused",
        0,
        1.0,
        snapshot_id="snap:" + "3" * 24,
        geometry=geometry,
        source_rect=source,
        scale=1.0,
        image_width=320,
        image_height=160,
    )
    frame = bytearray(source.width * source.height * 4)

    def grab_bgra(rect):
        return b"".join(
            frame[(y * 320 + rect.left) * 4 : (y * 320 + rect.right) * 4] for y in range(rect.top, rect.bottom)
        )

    driver = NativeWindowsDriver()
    driver._started = True
    driver.screen = NS(geometry=lambda: geometry, grab_bgra=grab_bgra)
    driver._captures[evidence.evidence_id] = Capture(evidence, geometry, source, 1.0)
    driver._tiles[evidence.evidence_id] = capture.tile_checksums(320, 160, bytes(frame))
    monkeypatch.setattr(driver, "window_handle", lambda _ref: 1)
    monkeypatch.setattr(win32, "window_rect", lambda _hwnd: source)
    monkeypatch.setattr(win32, "dpi_for_window", lambda _hwnd: 96)
    monkeypatch.setattr(win32, "root_window", lambda hwnd: hwnd)
    monkeypatch.setattr(win32, "window_from_point", lambda _x, _y: 1)
    monkeypatch.setattr(win32, "enum_top_level_windows", lambda: [1])
    point = ScreenshotPoint(evidence.evidence_id, 20, 20, source, 1.0, 320, 160, 1)
    request = NS(point=point, run_id=evidence.run_id, snapshot_id=evidence.snapshot_id, window_ref="win:1")
    snapshot = NS(windows=[NS(window_ref="win:1", rect=source, dpi=96)])

    frame[(150 * 320 + 300) * 4] = 255  # a spinner in the far corner
    assert driver.resolve_point(request, snapshot) == (20, 20)
    frame[(25 * 320 + 25) * 4] = 255  # under the point
    with pytest.raises(ContractError, match="content changed"):
        driver.resolve_point(request, snapshot)


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


def _user_path_click(monkeypatch, cursor_after):
    rect = Rect(0, 0, 20, 20)
    handle = NS(operations=("CLICK",), rect=rect)
    state = NS(rect=rect)
    monkeypatch.setattr(uia, "resolve_element", lambda *_: handle)
    monkeypatch.setattr(native_input, "live_state", lambda *_: state)
    monkeypatch.setattr(native_input, "require_user_path_ready", lambda *_: (10, 10))
    monkeypatch.setattr(native_input, "_hit_ok", lambda *_: True)
    monkeypatch.setattr(win32, "click_at", lambda *_: 3)
    positions = iter([(500, 400), cursor_after])
    monkeypatch.setattr(win32, "cursor_position", lambda: next(positions))
    restored = []
    monkeypatch.setattr(win32, "set_cursor_position", lambda *point: restored.append(point))
    request = NS(
        operation=Operation.CLICK,
        point=None,
        element_id="button",
        snapshot_id="snapshot",
        mode=InputMode.USER_PATH,
        action_id="action",
        window_ref="window",
    )
    native_input.execute(NS(worker=None, registry=None), request, lambda: None, NS(app_ref="app"))
    return restored


def test_cursor_returns_only_if_still_where_the_click_left_it(monkeypatch, no_send_input):
    assert _user_path_click(monkeypatch, (10, 10)) == [(500, 400)]
    # The person moved the mouse during the action: their position wins.
    assert _user_path_click(monkeypatch, (730, 90)) == []


def test_unreadable_cursor_is_left_alone(monkeypatch):
    def unreadable():
        raise DriverError("GetCursorPos failed (5)")

    monkeypatch.setattr(win32, "cursor_position", unreadable)
    monkeypatch.setattr(win32, "set_cursor_position", lambda *_: pytest.fail("cursor must not move"))
    native_input._restore_cursor((1, 2), (3, 4))


def test_refused_activation_pauses_resumably_naming_the_foreground_app(monkeypatch, no_send_input):
    target, other = 101, 202
    monkeypatch.setattr(win32.user32, "IsWindow", lambda _h: True)
    monkeypatch.setattr(win32.user32, "IsWindowEnabled", lambda _h: True)
    monkeypatch.setattr(win32, "activate_window", lambda _h: False)
    monkeypatch.setattr(win32, "foreground_window", lambda: other)
    monkeypatch.setattr(win32, "window_process_id", lambda hwnd: {other: 4242}[hwnd])
    monkeypatch.setattr(win32, "process_image_path", lambda pid: {4242: r"C:\Windows\ApplicationFrameHost.exe"}[pid])
    driver = NS(worker=None, registry=NS(windows={"win:1": NS(hwnd=target)}))
    request = NS(operation=Operation.FOCUS_WINDOW, window_ref="win:1", action_id="act", element_id=None)
    guarded = []

    with pytest.raises(Pause) as paused:
        native_input.execute(driver, request, lambda: guarded.append(True), NS(app_ref="app"))

    assert guarded == [True]
    assert paused.value.reason is Reason.USER_TAKEOVER
    assert paused.value.detail == {
        "reason": "Windows kept another window in the foreground; no input was sent",
        "foreground_process": "ApplicationFrameHost.exe",
        "resumable": True,
    }


def test_ctrl_w_is_a_supported_chord():
    assert native_input.parse_chord(["Ctrl", "W"]) == [0x11, 0x57]


def test_every_input_the_plugin_sends_carries_the_jev_tag(monkeypatch):
    sent = []

    def record(count, events, _size):
        sent.extend((events[i].type, events[i].mi.dwExtraInfo, events[i].ki.dwExtraInfo) for i in range(count))
        return count

    monkeypatch.setattr(win32.user32, "SendInput", record)  # never the real SendInput
    monkeypatch.setattr(win32, "key_down", lambda _vk: False)
    monkeypatch.setattr(win32, "_dispatch_guard", None)
    context = multiprocessing.get_context("spawn")
    buffer, count = context.RawArray("B", 8192), context.RawValue("i", 0)
    monkeypatch.setattr(win32, "_pending_inputs", buffer)
    monkeypatch.setattr(win32, "_pending_count", count)
    win32.move_mouse(5, 5)
    win32.click_at(5, 5, double=True, button="right")
    win32.scroll_wheel(5, 5, notches=-2, horizontal=True)
    win32.type_unicode("a\U0001f600")
    win32.key_chord([0x11, 0x10, 0x53])
    produced = len(sent)
    # The cleanup batch replays recorded releases, which were built by the same helpers.
    pending = [win32._key_input(0x11, win32.KEYEVENTF_KEYUP), win32._mouse_input(win32.MOUSEEVENTF_LEFTUP, 1, 1)]
    payload = bytes((win32.INPUT * 2)(*pending))
    buffer[: len(payload)] = payload
    count.value = 2
    win32.release_pending(buffer, count)
    assert produced == 1 + 5 + 2 + 6 + 6 and len(sent) == produced + 2
    for kind, mouse_extra, key_extra in sent:
        extra = mouse_extra if kind == win32.INPUT_MOUSE else key_extra
        assert extra == win32.JEV_INPUT_TAG


# --------------------------------------------------------------------------------------
# Element dispatch against fake UI Automation trees
# --------------------------------------------------------------------------------------

HWND = 786718


class _Uia:
    """A live UIA element: compared by identity, with a parent link and current properties."""

    def __init__(self, name, parent=None, rect=None, focused=lambda: False, props=None):
        self.name, self.parent, self._focused, self.props = name, parent, focused, props or {}
        self.rect = rect or Rect(0, 0, 0, 0)

    def GetCurrentPropertyValue(self, prop):
        if prop == uia.PROP_BOUNDS:
            return [self.rect.left, self.rect.top, self.rect.width, self.rect.height]
        if prop == uia.PROP_ENABLED:
            return True
        if prop == uia.PROP_OFFSCREEN:
            return False
        if prop == uia.PROP_FOCUSED:
            return self._focused()
        return self.props.get(prop)


def _uia_worker(element_at_point, **automation):
    worker = NS(
        automation=NS(
            CompareElements=lambda a, b: a is b,
            RawViewWalker=NS(GetParentElement=lambda element: element.parent),
            **automation,
        ),
        element_at_point=element_at_point,
    )
    worker.submit = lambda fn, **_: fn(worker)
    return worker


def _element_handle(element, element_id, role, operations):
    return NS(
        element=element,
        element_id=element_id,
        role=role,
        operations=operations,
        rect=element.rect,
        hwnd=HWND,
        name=element.name,
        visible=True,
        snapshot_id="snap",
    )


def _element_request(operation, element_id, **extra):
    return NS(
        operation=operation,
        point=None,
        element_id=element_id,
        snapshot_id="snap",
        mode=InputMode.USER_PATH,
        text=None,
        option_label=None,
        replace_existing=True,
        deadline_s=1.0,
        action_id="act",
        window_ref="win",
        **extra,
    )


@pytest.fixture
def desktop(monkeypatch, no_send_input):
    """An enabled foreground window. Returns the points clicked; typing fails the test."""
    monkeypatch.setattr(win32.user32, "IsWindow", lambda _h: True)
    monkeypatch.setattr(win32.user32, "IsWindowEnabled", lambda _h: True)
    monkeypatch.setattr(win32, "root_window", lambda hwnd: hwnd)
    monkeypatch.setattr(win32, "foreground_window", lambda: HWND)
    monkeypatch.setattr(win32, "virtual_screen", lambda: Rect(0, 0, 2560, 1440))
    monkeypatch.setattr(win32, "cursor_position", lambda: (0, 0))
    monkeypatch.setattr(win32, "set_cursor_position", lambda *_: None)
    clicks = []
    monkeypatch.setattr(win32, "click_at", lambda x, y: clicks.append((x, y)) or 3)
    monkeypatch.setattr(win32, "type_unicode", lambda _text: pytest.fail("text was typed"))
    monkeypatch.setattr(win32, "key_chord", lambda _codes: 2)
    monkeypatch.setattr(native_input.time, "sleep", lambda _s: None)
    return clicks


@pytest.mark.parametrize(("nc_hit", "clicked"), [(20, True), (1, False)], ids=["htclose", "htclient"])
def test_caption_button_click_is_proven_by_the_window_hit_test(monkeypatch, desktop, nc_hit, clicked):
    # Electron titleBarOverlay: the caption buttons are native views, and a UIA point query over
    # them resolves into the web document beneath the overlay.
    window = _Uia("window")
    frame = _Uia("WinFrameView", window)
    buttons = _Uia("WinCaptionButtonContainer", frame)
    close = _Uia("Close", buttons, rect=Rect(1928, 1, 1986, 45), props={uia.PROP_CLASSNAME: "WinCaptionButton"})
    page = _Uia("header group", _Uia("Document", _Uia("Chrome Legacy Window", frame)))
    render_widget = 900
    monkeypatch.setattr(win32, "root_window", lambda hwnd: HWND if hwnd == render_widget else hwnd)
    monkeypatch.setattr(win32, "window_from_point", lambda x, y: render_widget)
    queried = []

    def send_message_timeout(hwnd, message, wparam, lparam, flags, timeout_ms, result):
        queried.append((hwnd, message, lparam))
        result._obj.value = nc_hit
        return 1

    monkeypatch.setattr(win32.user32, "SendMessageTimeoutW", send_message_timeout)
    monkeypatch.setattr(uia, "resolve_element", lambda *_: _element_handle(close, "el:close", "button", ("CLICK",)))
    driver = NS(worker=_uia_worker(lambda _x, _y: page), registry=None)
    request = _element_request(Operation.CLICK, "el:close")

    if clicked:
        receipt = native_input.execute(driver, request, lambda: None, NS(app_ref="app"))
        assert receipt.notes == ("point=1957,23",)
    else:
        with pytest.raises(Pause) as paused:
            native_input.execute(driver, request, lambda: None, NS(app_ref="app"))
        assert paused.value.reason is Reason.STALE_OBSERVATION
    assert queried == [(HWND, 0x84, (23 << 16) | 1957)]
    assert desktop == ([(1957, 23)] if clicked else [])
