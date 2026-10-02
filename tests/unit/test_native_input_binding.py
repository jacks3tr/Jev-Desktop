"""Target membership and empty replacement at the native input boundary."""

from types import SimpleNamespace as NS

import pytest

from jev_desktop.contracts import InputMode, Operation, Rect
from jev_desktop.drivers.windows import input as native_input
from jev_desktop.drivers.windows import uia
from jev_desktop.journal import UncertainEffect


def test_same_label_option_must_belong_to_selected_container(monkeypatch):
    container, unrelated = object(), object()

    def option(owner):
        return NS(GetCurrentPropertyValue=lambda prop: owner if prop == 30080 else True)

    automation = NS(CompareElements=lambda a, b: a is b, RawViewWalker=NS(GetParentElement=lambda _: None))
    worker = NS(automation=automation, submit=lambda fn, **_: fn(worker))
    rect = Rect(0, 0, 20, 20)
    elements = [
        NS(element_id="button", name="Yes", role="button"),
        NS(element_id="wrong-list", name="Yes", role="listitem"),
        NS(element_id="right-list", name="Yes", role="listitem"),
    ]
    handles = {
        e.element_id: NS(
            element=option(container if e.element_id == "right-list" else unrelated), visible=True, rect=rect
        )
        for e in elements
    }
    driver = NS(worker=worker, registry=NS(elements=handles))
    state = NS(rect=rect, enabled=True, offscreen=False)
    monkeypatch.setattr(native_input, "live_state", lambda *_: state)
    monkeypatch.setattr(native_input, "_hit_ok", lambda *_: True)
    result = native_input._observed_option(driver, NS(elements=elements), NS(element=container), "Yes")
    assert result[0] is handles["right-list"]


def test_discarded_target_is_a_stale_observation_not_a_driver_error():
    import comtypes

    from jev_desktop.contracts import Pause, Reason

    def discarded(_prop):
        raise comtypes.COMError(-2147220991, "An event was unable to invoke any of the subscribers", None)

    handle = NS(element=NS(GetCurrentPropertyValue=discarded), element_id="el:1", role="button")
    worker = NS(submit=lambda fn, **_: fn(worker))
    with pytest.raises(Pause) as paused:
        native_input.live_state(worker, handle)
    assert paused.value.reason is Reason.STALE_OBSERVATION


@pytest.mark.parametrize("settles", [True, False], ids=["async-hit-settles", "covered"])
def test_hit_check_resamples_before_calling_a_target_covered(monkeypatch, settles):
    target, text, dialog, backdrop = object(), object(), object(), object()
    parents = {text: target, target: dialog}
    # Chromium's first answer is the stale dialog hit; the exact one lands a moment later.
    hits = [dialog, text if settles else backdrop, text if settles else backdrop]
    automation = NS(CompareElements=lambda a, b: a is b, RawViewWalker=NS(GetParentElement=parents.get))
    worker = NS(automation=automation, submit=lambda fn, **_: fn(worker), element_at_point=lambda *_: hits.pop(0))
    monkeypatch.setattr(uia.time, "sleep", lambda _: None)
    assert uia.hit_is_descendant_or_self(worker, NS(element=target), 10, 10) is settles
    assert len(hits) == (1 if settles else 0), "sampling stops at the first proven hit and gives up after three"


def test_text_replacement_waits_for_readback_without_retyping(monkeypatch):
    rect = Rect(0, 0, 20, 20)
    handle = NS(operations=("TYPE_TEXT",), rect=rect)
    state = NS(rect=rect, focused=True, root=1, foreground_root=1)
    monkeypatch.setattr(uia, "resolve_element", lambda *_: handle)
    monkeypatch.setattr(native_input, "live_state", lambda *_: state)
    monkeypatch.setattr(native_input, "require_user_path_ready", lambda *_: (10, 10))
    monkeypatch.setattr(native_input, "_hit_ok", lambda *_: True)
    monkeypatch.setattr(native_input, "_typing_value", lambda *_: "")
    keys = []
    monkeypatch.setattr(native_input.win32, "cursor_position", lambda: (0, 0))
    monkeypatch.setattr(native_input.win32, "set_cursor_position", lambda *_: None)
    monkeypatch.setattr(native_input.win32, "click_at", lambda *_: 2)
    monkeypatch.setattr(native_input.win32, "key_chord", lambda chord: keys.append(chord) or 2)
    monkeypatch.setattr(native_input.win32, "type_unicode", lambda _: 0)
    request = NS(
        operation=Operation.TYPE_TEXT,
        point=None,
        element_id="field",
        snapshot_id="snapshot",
        mode=InputMode.USER_PATH,
        text="",
        replace_existing=True,
        deadline_s=1,
        action_id="action",
        window_ref="window",
    )
    driver = NS(worker=None, registry=None)
    native_input.execute(driver, request, lambda: None, NS(app_ref="app"))
    assert keys == [[17, 36], [17, 16, 35], [46]]
    keys.clear()
    request.replace_existing = False
    native_input.execute(driver, request, lambda: None, NS(app_ref="app"))
    assert keys == []

    request.replace_existing = True
    request.text = "Read-only"
    sent = []
    monkeypatch.setattr(native_input.win32, "type_unicode", lambda text: sent.append(text) or 18)
    values = iter(["", "ead-only", "Read-only"])
    monkeypatch.setattr(native_input, "_typing_value", lambda *_: next(values))
    monkeypatch.setattr(native_input.time, "sleep", lambda _: None)
    native_input.execute(driver, request, lambda: None, NS(app_ref="app"))
    assert sent == ["Read-only"]

    request.deadline_s = 0
    monkeypatch.setattr(native_input, "_typing_value", lambda *_: "")
    with pytest.raises(UncertainEffect):
        native_input.execute(driver, request, lambda: None, NS(app_ref="app"))
    assert sent == ["Read-only", "Read-only"]

    # Multi-line Win32 edits read typed "\n" back as CRLF.
    request.text = "first\nsecond\rthird"
    monkeypatch.setattr(native_input, "_typing_value", lambda *_: "first\r\nsecond\r\nthird")
    receipt = native_input.execute(driver, request, lambda: None, NS(app_ref="app"))
    assert "observed value differs from dispatched text" not in receipt.notes

    # A short input dispatch remains uncertain; a full-value reader never replays it.
    def partial(text):
        sent.append(text)
        raise UncertainEffect("short SendInput count")

    monkeypatch.setattr(native_input.win32, "type_unicode", partial)
    before = len(sent)
    with pytest.raises(UncertainEffect, match="short SendInput count"):
        native_input.execute(driver, request, lambda: None, NS(app_ref="app"))
    assert len(sent) == before + 1


def _semantic_scroll(monkeypatch, scroll):
    calls = []
    pattern = NS(Scroll=lambda horizontal, vertical: calls.append((horizontal, vertical)))
    constants = NS(ScrollAmount_SmallIncrement="inc", ScrollAmount_SmallDecrement="dec", ScrollAmount_NoAmount="-")
    monkeypatch.setattr(native_input, "_pattern", lambda *_: pattern)
    monkeypatch.setattr(uia, "uia_module", lambda: constants)
    request = NS(operation=Operation.SCROLL, scroll=scroll)
    assert native_input.invoke_semantic(NS(element=object()), request)[0] == "scroll"
    return calls


def _typing_reader_fixture(monkeypatch, *, password=False, text_pattern=None):
    full = "x" * 5000
    props = {
        uia.PROP_PASSWORD: password,
        uia.PROP_NATIVE_HANDLE: 2,
        uia.PROP_PROCESS_ID: 42,
        uia.PROP_VALUE: full[:4096],
    }
    element = NS(GetCurrentPropertyValue=props.get)
    handle = NS(element=element, hwnd=1)
    window = NS(hwnd=1, process_id=42)
    worker = NS(submit=lambda fn, **_: fn(worker))
    monkeypatch.setattr(native_input, "_pattern", lambda *_: text_pattern)
    monkeypatch.setattr(native_input.win32.user32, "IsWindow", lambda _: True)
    monkeypatch.setattr(native_input.win32, "root_window", lambda _: 1)
    monkeypatch.setattr(native_input.win32, "window_process_id", lambda _: 42)
    monkeypatch.setattr(native_input.win32, "window_class", lambda _: "WindowsForms10.EDIT.app.0.fixture")
    return full, props, worker, handle, window


def test_typing_reads_full_text_pattern_before_capped_value(monkeypatch):
    pattern = NS(DocumentRange=NS(GetText=lambda count: "x" * 5000 if count == -1 else None))
    full, _, worker, handle, window = _typing_reader_fixture(monkeypatch, text_pattern=pattern)
    assert native_input._typing_value(worker, handle, window) == full


@pytest.mark.parametrize("failure", [None, "stale", "root", "process", "class", "password", "timeout", "changed"])
def test_typing_native_edit_readback_is_full_and_bound_to_approved_window(monkeypatch, failure):
    full, props, worker, handle, window = _typing_reader_fixture(monkeypatch, password=failure == "password")
    reads = []

    def read(hwnd):
        reads.append(hwnd)
        if failure == "changed":
            props[uia.PROP_NATIVE_HANDLE] = 3
        return None if failure == "timeout" else full

    monkeypatch.setattr(native_input.win32, "edit_text", read, raising=False)
    if failure == "stale":
        monkeypatch.setattr(native_input.win32.user32, "IsWindow", lambda _: False)
    if failure == "root":
        monkeypatch.setattr(native_input.win32, "root_window", lambda _: 3)
    if failure == "process":
        monkeypatch.setattr(native_input.win32, "window_process_id", lambda _: 43)
    if failure == "class":
        monkeypatch.setattr(native_input.win32, "window_class", lambda _: "UnknownEdit")
    observed = native_input._typing_value(worker, handle, window)
    assert observed == (full if failure is None else None if failure == "password" else full[:4096])
    assert reads == ([2] if failure in {None, "timeout", "changed"} else [])


def test_selection_keeps_value_readback_when_document_text_differs(monkeypatch):
    _, props, worker, handle, _ = _typing_reader_fixture(monkeypatch)
    props[uia.PROP_VALUE] = "Chosen"
    native_input._verify_selection(
        worker, handle, NS(option_label="Chosen", deadline_s=0), lambda: None, native_input.time.time(), NS()
    )
    assert native_input._live_value(worker, handle) == "Chosen"


@pytest.mark.parametrize("outcome", ["full", "timeout", "capacity"])
def test_native_edit_text_query_is_bounded_read_only(monkeypatch, outcome):
    import ctypes

    def send(hwnd, message, capacity, pointer, flags, timeout, result):
        assert (hwnd, message, flags, timeout) == (2, 0x000D, 2, 250)
        assert capacity == 65537
        if outcome == "timeout":
            return 0
        result._obj.value = 65536 if outcome == "capacity" else 5000
        buffer = (ctypes.c_wchar * capacity).from_address(pointer)
        buffer.value = "x" * result._obj.value
        return 1

    monkeypatch.setattr(native_input.win32.user32, "SendMessageTimeoutW", send)
    assert native_input.win32.edit_text(2) == ("x" * 5000 if outcome == "full" else None)


def test_semantic_scroll_follows_the_wheel_convention_and_magnitude(monkeypatch):
    assert _semantic_scroll(monkeypatch, {"notches": 3}) == [("-", "dec")] * 3
    assert _semantic_scroll(monkeypatch, {"notches": -2}) == [("-", "inc")] * 2
    assert _semantic_scroll(monkeypatch, {"notches": 2, "horizontal": True}) == [("inc", "-")] * 2
    assert _semantic_scroll(monkeypatch, {"notches": -1, "horizontal": True}) == [("dec", "-")]
