"""Target membership and empty replacement at the native input boundary."""

from types import SimpleNamespace as NS

from jev_desktop.contracts import InputMode, Operation, Rect
from jev_desktop.drivers.windows import input as native_input
from jev_desktop.drivers.windows import uia


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
    assert result[0] == "right-list"


def test_empty_replacement_sends_delete_but_empty_append_does_not(monkeypatch):
    rect = Rect(0, 0, 20, 20)
    handle = NS(operations=("TYPE_TEXT",), rect=rect)
    state = NS(rect=rect, focused=True, root=1, foreground_root=1)
    monkeypatch.setattr(uia, "resolve_element", lambda *_: handle)
    monkeypatch.setattr(native_input, "live_state", lambda *_: state)
    monkeypatch.setattr(native_input, "require_user_path_ready", lambda *_: (10, 10))
    monkeypatch.setattr(native_input, "_hit_ok", lambda *_: True)
    monkeypatch.setattr(native_input, "_live_value", lambda *_: "")
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
