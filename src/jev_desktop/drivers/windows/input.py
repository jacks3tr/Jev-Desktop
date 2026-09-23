"""Guarded native input and control-pattern operations.

`user_path` (default) locates the control through accessibility data and then drives the
required mouse/keyboard path; `semantic` uses UI Automation control patterns and records
that mechanism. Semantic invocation is never evidence that a mouse-driven path works.

Every dispatch re-validates live state immediately before the native call: element
identity, enabled/visible flags, geometry stability, foreground ownership, modal state,
and the hit target under the intended screen point. Failures before the boundary raise
:class:`DriverError` or :class:`Pause`; failures after it raise
:class:`UncertainEffect` because the effect is unknown.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from ...contracts import (
    ActionRequest,
    DispatchMechanism,
    DispatchState,
    DriverError,
    InputMode,
    Operation,
    Pause,
    Reason,
    Receipt,
    Rect,
    Snapshot,
    UncertainEffect,
)
from . import uia, win32

# --------------------------------------------------------------------------------------
# Key chords: only combinations the driver implements and validates
# --------------------------------------------------------------------------------------

VK_BY_NAME: dict[str, int] = {
    "ctrl": 0x11,
    "control": 0x11,
    "shift": 0x10,
    "alt": 0x12,
    "win": 0x5B,
    "enter": 0x0D,
    "return": 0x0D,
    "tab": 0x09,
    "esc": 0x1B,
    "escape": 0x1B,
    "space": 0x20,
    "backspace": 0x08,
    "delete": 0x2E,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    "f1": 0x70,
    "f2": 0x71,
    "f3": 0x72,
    "f4": 0x73,
    "f5": 0x74,
    "f6": 0x75,
    "f7": 0x76,
    "f8": 0x77,
    "f9": 0x78,
    "f10": 0x79,
    "f11": 0x7A,
    "f12": 0x7B,
}
for _letter in "abcdefghijklmnopqrstuvwxyz":
    VK_BY_NAME[_letter] = ord(_letter.upper())
for _digit in "0123456789":
    VK_BY_NAME[_digit] = ord(_digit)

SUPPORTED_CHORDS = {
    "ctrl+s",
    "ctrl+a",
    "ctrl+c",
    "ctrl+v",
    "ctrl+x",
    "ctrl+z",
    "ctrl+n",
    "ctrl+o",
    "ctrl+f",
    "ctrl+l",
    "ctrl+shift+s",
    "ctrl+p",
    "alt+f4",
    "alt+tab",
    "shift+tab",
    "enter",
    "esc",
    "tab",
    "space",
    "f5",
    "up",
    "down",
    "left",
    "right",
    "home",
    "end",
    "pageup",
    "pagedown",
    "delete",
    "backspace",
}


def parse_chord(parts: tuple[str, ...] | list[str]) -> list[int]:
    if not parts:
        raise DriverError("hotkey requires at least one key")
    names = [str(part).strip().lower() for part in parts]
    chord = " ".join(names)
    single = "+".join(names)
    if single not in SUPPORTED_CHORDS and chord not in SUPPORTED_CHORDS:
        raise Pause(Reason.UNSUPPORTED_CONTROL, {"hotkey": single, "supported": sorted(SUPPORTED_CHORDS)})
    codes: list[int] = []
    for name in names:
        code = VK_BY_NAME.get(name)
        if code is None:
            raise Pause(Reason.UNSUPPORTED_CONTROL, {"hotkey": name})
        codes.append(code)
    if len(codes) > 1 and codes[-1] in {VK_BY_NAME["ctrl"], VK_BY_NAME["shift"], VK_BY_NAME["alt"]}:
        raise DriverError("modifier cannot be the final key of a chord")
    return codes


# --------------------------------------------------------------------------------------
# Live validation
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LiveState:
    rect: Rect
    enabled: bool
    offscreen: bool
    focused: bool
    window_hwnd: int
    window_enabled: bool
    foreground_root: int

    @property
    def root(self) -> int:
        return win32.root_window(self.window_hwnd)


def live_state(worker: uia.UiaWorker, handle: uia.ElementHandle) -> LiveState:
    def _read(_worker: uia.UiaWorker) -> LiveState:
        element = handle.element
        rect = uia._rect_of(element.GetCurrentPropertyValue(uia.PROP_BOUNDS))
        enabled = bool(element.GetCurrentPropertyValue(uia.PROP_ENABLED))
        offscreen = bool(element.GetCurrentPropertyValue(uia.PROP_OFFSCREEN))
        if offscreen and handle.role in uia.ROW_ROLES and not rect.is_empty:
            offscreen = not uia.point_hits_element(worker, element, *rect.center())
        focused = bool(element.GetCurrentPropertyValue(uia.PROP_FOCUSED))
        hwnd = handle.hwnd if win32.user32.IsWindow(handle.hwnd) else 0
        # A modal dialog disables the owner window: the child control keeps its own
        # WS_DISABLED clear, so the top-level window's state is what decides.
        root = win32.root_window(hwnd) if hwnd else 0
        root_enabled = bool(root) and bool(win32.user32.IsWindowEnabled(root))
        return LiveState(
            rect=rect,
            enabled=enabled and bool(hwnd) and bool(win32.user32.IsWindowEnabled(hwnd)) and root_enabled,
            offscreen=offscreen,
            focused=focused,
            window_hwnd=hwnd,
            window_enabled=root_enabled,
            foreground_root=win32.root_window(win32.foreground_window()) if win32.foreground_window() else 0,
        )

    return worker.submit(_read, timeout=10.0)


def _geometry_ok(cached: Rect, live: Rect, tolerance: int = 4) -> bool:
    return (
        not live.is_empty
        and abs(live.left - cached.left) <= tolerance
        and abs(live.top - cached.top) <= tolerance
        and abs(live.right - cached.right) <= tolerance
        and abs(live.bottom - cached.bottom) <= tolerance
    )


def _hit_ok(worker: uia.UiaWorker, handle: uia.ElementHandle, x: int, y: int) -> bool:
    return uia.hit_is_descendant_or_self(worker, handle, x, y)


def require_user_path_ready(
    handle: uia.ElementHandle, state: LiveState, *, needs_keyboard: bool = False
) -> tuple[int, int]:
    """Read-only pre-dispatch guards for the user path.

    Mouse routing is proven by the hit test under the intended point (see `_hit_ok`): the
    input can only reach the intended control if that control, or its descendant, is what
    the desktop resolves at those coordinates. Keyboard routing is a different mechanism, since
    keystrokes follow the focus, so keyboard input additionally requires the target's top-level
    window to be the foreground window.
    """
    if not handle.operations:
        raise Pause(Reason.UNSUPPORTED_CONTROL, {"role": handle.role, "element": handle.element_id})
    if not state.window_hwnd:
        raise Pause(Reason.STALE_OBSERVATION, {"reason": "window is gone"})
    if not state.window_enabled:
        raise Pause(
            Reason.PERMISSION_BOUNDARY,
            {"reason": "target window is disabled (a modal dialog is likely active)"},
        )
    if not state.enabled or state.offscreen:
        raise Pause(Reason.UNSUPPORTED_CONTROL, {"reason": "target control is disabled or offscreen"})
    if needs_keyboard and state.root != state.foreground_root:
        raise Pause(
            Reason.USER_TAKEOVER,
            {"reason": "target window does not hold the keyboard focus", "foreground_root": state.foreground_root},
        )
    if not state.rect.width or not state.rect.height:
        raise Pause(Reason.STALE_OBSERVATION, {"reason": "target has no geometry"})
    screen = win32.virtual_screen()
    if not screen.contains(*state.rect.center()):
        raise Pause(Reason.STALE_OBSERVATION, {"reason": "target is off the virtual desktop"})
    return state.rect.center()


# --------------------------------------------------------------------------------------
# Patterns (semantic mode)
# --------------------------------------------------------------------------------------


PATTERN_INTERFACES: dict[str, str] = {
    "UIA_InvokePatternId": "IUIAutomationInvokePattern",
    "UIA_ValuePatternId": "IUIAutomationValuePattern",
    "UIA_TogglePatternId": "IUIAutomationTogglePattern",
    "UIA_SelectionItemPatternId": "IUIAutomationSelectionItemPattern",
    "UIA_SelectionPatternId": "IUIAutomationSelectionPattern",
    "UIA_ScrollPatternId": "IUIAutomationScrollPattern",
    "UIA_ExpandCollapsePatternId": "IUIAutomationExpandCollapsePattern",
    "UIA_TextPatternId": "IUIAutomationTextPattern",
    "UIA_WindowPatternId": "IUIAutomationWindowPattern",
}


def _pattern(element: Any, name: str, fallback: int) -> Any | None:
    """Fetch a typed control pattern; the untyped COM result has no usable methods."""
    UIA = uia.uia_module()
    pattern_id = uia.pattern_id(name, fallback)
    try:
        raw = element.GetCurrentPattern(pattern_id)
    except Exception:
        return None
    if raw is None:
        return None
    interface = getattr(UIA, PATTERN_INTERFACES[name], None)
    if interface is None:
        return raw
    try:
        return raw.QueryInterface(interface)
    except Exception:
        return None


def invoke_semantic(handle: uia.ElementHandle, request: ActionRequest) -> tuple[Any, str]:
    """Dispatch through control patterns only. Returns (pattern_name, element)."""
    element = handle.element
    if request.operation is Operation.CLICK:
        pattern = _pattern(element, "UIA_InvokePatternId", 10000)
        if pattern is not None:
            pattern.Invoke()
            return "invoke", element
        if handle.role in {"checkbox", "radiobutton"}:
            toggle = _pattern(element, "UIA_TogglePatternId", 10015)
            if toggle is not None:
                toggle.Toggle()
                return "toggle", element
        selection = _pattern(element, "UIA_SelectionItemPatternId", 10010)
        if selection is not None:
            selection.Select()
            return "selection_item", element
        raise Pause(Reason.UNSUPPORTED_CONTROL, {"reason": "no invoke-compatible pattern"})

    if request.operation is Operation.TOGGLE:
        toggle = _pattern(element, "UIA_TogglePatternId", 10015)
        if toggle is not None:
            toggle.Toggle()
            return "toggle", element
        selection = _pattern(element, "UIA_SelectionItemPatternId", 10010)
        if selection is not None:
            selection.Select()
            return "selection_item", element
        raise Pause(Reason.UNSUPPORTED_CONTROL, {"reason": "no toggle-compatible pattern"})

    if request.operation is Operation.SELECT:
        if not request.option_label:
            raise DriverError("SELECT requires an observed option label")
        selection = _pattern(element, "UIA_SelectionItemPatternId", 10010)
        if selection is not None and handle.name.strip().lower() == request.option_label.strip().lower():
            selection.Select()
            return "selection_item", element
        raise Pause(Reason.UNSUPPORTED_CONTROL, {"reason": "no selection-compatible pattern"})

    if request.operation is Operation.TYPE_TEXT:
        if request.text is None:
            raise DriverError("TYPE_TEXT requires fixture text")
        value = _pattern(element, "UIA_ValuePatternId", 10002)
        if value is None:
            raise Pause(Reason.UNSUPPORTED_CONTROL, {"reason": "no value pattern for text entry"})
        readonly = uia._cached(element, uia.PROP_VALUE_READONLY)
        if readonly:
            raise Pause(Reason.UNSUPPORTED_CONTROL, {"reason": "control is read-only"})
        text = request.text
        if not request.replace_existing:
            previous = element.GetCurrentPropertyValue(uia.PROP_VALUE)
            if not isinstance(previous, str):
                raise Pause(Reason.UNSUPPORTED_CONTROL, {"reason": "cannot append without an observed value"})
            text = previous + text
        value.SetValue(text)
        return "value", element

    if request.operation is Operation.SCROLL:
        scroll = _pattern(element, "UIA_ScrollPatternId", 10004)
        if scroll is None:
            raise Pause(Reason.UNSUPPORTED_CONTROL, {"reason": "no scroll pattern"})
        notches = _scroll_notches(request)
        horizontal = bool(request.scroll.get("horizontal"))
        UIA = uia.uia_module()
        # Same convention as the wheel: positive is up or right. A UIA increment moves down
        # (vertical) or right (horizontal), so only the vertical axis flips.
        forward = notches > 0 if horizontal else notches < 0
        step = UIA.ScrollAmount_SmallIncrement if forward else UIA.ScrollAmount_SmallDecrement
        none = UIA.ScrollAmount_NoAmount
        for _ in range(abs(notches)):
            if horizontal:
                scroll.Scroll(step, none)
            else:
                scroll.Scroll(none, step)
        return "scroll", element

    raise Pause(Reason.UNSUPPORTED_CONTROL, {"operation": request.operation.value, "mode": "semantic"})


def _scroll_notches(request: ActionRequest) -> int:
    """Wheel notches: positive scrolls up (vertical) or right (horizontal)."""
    return int(request.scroll.get("notches", request.scroll.get("amount", 3)) or 3)


# --------------------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------------------


def execute(
    driver: Any,
    request: ActionRequest,
    guard: Any,
    snapshot: Snapshot | None,
) -> Receipt:
    """Dispatch exactly one bounded action. See the module docstring for failure semantics."""
    if request.operation in {Operation.WAIT, Operation.DONE, Operation.ESCALATE}:
        raise DriverError(f"{request.operation.value} is not a native action")
    if request.operation is Operation.LAUNCH_APP:
        raise DriverError("LAUNCH_APP is executed by the broker, not the input driver")

    started = time.time()
    worker = driver.worker
    registry = driver.registry

    if request.operation is Operation.HOTKEY:
        _guard_foreground_app(driver, request, snapshot)
        codes = parse_chord(request.hotkey)
        guard()
        inserted = win32.key_chord(codes)
        return _receipt(request, DispatchMechanism.SEND_INPUT_KEYBOARD, inserted, started)

    if request.operation is Operation.FOCUS_WINDOW:
        return _focus_window(driver, request, guard, started)

    if request.point is not None:
        if snapshot is None or request.mode is not InputMode.USER_PATH or request.element_id is not None:
            raise DriverError("coordinate input requires a snapshot, user_path mode, and no element_id")
        if request.operation not in {Operation.CLICK, Operation.TOGGLE, Operation.SCROLL}:
            raise Pause(Reason.UNSUPPORTED_CONTROL, {"reason": "coordinates support click, toggle, and scroll"})
        _guard_foreground_app(driver, request, snapshot)
        x, y = driver.resolve_point(request, snapshot)
        guard()
        previous = win32.cursor_position()
        try:
            if request.operation is Operation.SCROLL:
                inserted = win32.scroll_wheel(
                    x,
                    y,
                    notches=_scroll_notches(request),
                    horizontal=bool(request.scroll.get("horizontal", False)),
                )
            else:
                inserted = win32.click_at(x, y)
        finally:
            _restore_cursor(previous, (x, y))
        return _receipt(
            request,
            DispatchMechanism.SEND_INPUT_MOUSE,
            inserted,
            started,
            notes=(f"evidence={request.point.evidence_id}", f"point={x},{y}"),
        )

    if snapshot is None:
        raise DriverError("element operations require the snapshot they were observed in")
    if not request.element_id:
        raise DriverError(f"{request.operation.value} requires an observed element")

    handle = uia.resolve_element(registry, request.element_id, request.snapshot_id, snapshot.app_ref)
    if request.operation.value not in handle.operations:
        raise Pause(
            Reason.UNSUPPORTED_CONTROL,
            {"operation": request.operation.value, "role": handle.role, "supported": list(handle.operations)},
        )

    state = live_state(worker, handle)
    if not _geometry_ok(handle.rect, state.rect):
        raise Pause(
            Reason.STALE_OBSERVATION,
            {"element": handle.element_id, "cached": handle.rect.to_json(), "live": state.rect.to_json()},
        )

    if request.mode is InputMode.SEMANTIC:
        if not state.enabled or state.offscreen or not state.window_enabled:
            raise Pause(Reason.PERMISSION_BOUNDARY, {"reason": "semantic target is disabled or offscreen"})

        def dispatch_pattern(_worker):
            guard()
            return invoke_semantic(handle, request)[0]

        try:
            pattern_name = worker.submit(dispatch_pattern, timeout=request.deadline_s)
        except Pause:
            raise
        except Exception as exc:
            raise UncertainEffect(
                f"control pattern failed after dispatch boundary: {exc}", mechanism=DispatchMechanism.UIA_PATTERN
            ) from exc
        return _receipt(
            request,
            DispatchMechanism.UIA_PATTERN,
            1,
            started,
            notes=(f"pattern={pattern_name}", f"role={handle.role}"),
        )

    x, y = require_user_path_ready(handle, state)
    if not _hit_ok(worker, handle, x, y):
        raise Pause(
            Reason.STALE_OBSERVATION,
            {"reason": "the point under the target is covered by a different control", "point": [x, y]},
        )

    if request.operation in {Operation.CLICK, Operation.TOGGLE}:
        guard()
        previous = win32.cursor_position()
        try:
            inserted = win32.click_at(x, y)
        except DriverError as exc:
            raise UncertainEffect(
                f"mouse dispatch failed: {exc}", mechanism=DispatchMechanism.SEND_INPUT_MOUSE
            ) from exc
        finally:
            _restore_cursor(previous, (x, y))
        return _receipt(request, DispatchMechanism.SEND_INPUT_MOUSE, inserted, started, notes=(f"point={x},{y}",))

    if request.operation is Operation.SCROLL:
        notches = _scroll_notches(request)
        horizontal = bool(request.scroll.get("horizontal"))
        guard()
        previous = win32.cursor_position()
        try:
            inserted = win32.scroll_wheel(x, y, notches=notches, horizontal=horizontal)
        except DriverError as exc:
            raise UncertainEffect(
                f"wheel dispatch failed: {exc}", mechanism=DispatchMechanism.SEND_INPUT_MOUSE
            ) from exc
        finally:
            _restore_cursor(previous, (x, y))
        return _receipt(
            request,
            DispatchMechanism.SEND_INPUT_MOUSE,
            inserted,
            started,
            notes=(f"point={x},{y}", f"notches={notches}"),
        )

    if request.operation is Operation.SELECT:
        if not request.option_label:
            raise DriverError("SELECT requires an observed option label")
        option = _observed_option(driver, snapshot, handle, request.option_label)
        if option is None:
            raise Pause(Reason.UNSUPPORTED_CONTROL, {"reason": "SELECT requires a visible observed option"})
        select_notes: tuple[str, ...]
        option_state = option[1]
        ox, oy = option_state.rect.center()
        guard()
        previous = win32.cursor_position()
        try:
            if not option_state.enabled or option_state.offscreen:
                raise Pause(Reason.UNSUPPORTED_CONTROL, {"reason": "option is not selectable"})
            inserted = win32.click_at(ox, oy)
            select_notes = ("observed option selected", f"point={ox},{oy}")
        except DriverError as exc:
            raise UncertainEffect(
                f"selection dispatch failed: {exc}", mechanism=DispatchMechanism.SEND_INPUT_MOUSE
            ) from exc
        finally:
            _restore_cursor(previous, (ox, oy))
        _verify_selection(worker, handle, request.option_label)
        return _receipt(request, DispatchMechanism.SEND_INPUT_MOUSE, inserted, started, notes=select_notes)

    if request.operation is Operation.TYPE_TEXT:
        if request.text is None:
            raise DriverError("TYPE_TEXT requires fixture text")
        guard()
        previous = win32.cursor_position()
        try:
            inserted = win32.click_at(x, y)
        except DriverError as exc:
            raise UncertainEffect(f"focus click failed: {exc}", mechanism=DispatchMechanism.SEND_INPUT_MOUSE) from exc
        try:
            focus = live_state(worker, handle)
            focus_deadline = time.monotonic() + min(0.5, max(0.0, request.deadline_s))
            while not focus.focused and time.monotonic() < focus_deadline:
                guard()
                time.sleep(0.025)
                focus = live_state(worker, handle)
            if not focus.focused:
                raise UncertainEffect(
                    "click was dispatched but the control did not take focus; no text was typed",
                    mechanism=DispatchMechanism.SEND_INPUT_MOUSE,
                )
            ready = live_state(worker, handle)
            if not ready.focused or ready.root != ready.foreground_root:
                raise UncertainEffect(
                    "focus changed after the click; no text was typed",
                    mechanism=DispatchMechanism.SEND_INPUT_MOUSE,
                )
            guard()
            if request.replace_existing:
                # Real fields arrive prefilled or with placeholder text selected. Typing over
                # the selection is what a person does; appending silently produces values like
                # "*.txtC:\\path\\file.txt" that no dialog accepts.
                inserted += win32.key_chord([VK_BY_NAME["ctrl"], VK_BY_NAME["home"]])
                inserted += win32.key_chord([VK_BY_NAME["ctrl"], VK_BY_NAME["shift"], VK_BY_NAME["end"]])
            guard()
            if request.replace_existing and not request.text:
                inserted += win32.key_chord([VK_BY_NAME["delete"]])
            else:
                inserted += win32.type_unicode(request.text)
        except UncertainEffect:
            raise
        except BaseException as exc:
            raise UncertainEffect(
                f"keyboard dispatch failed: {exc}", mechanism=DispatchMechanism.SEND_INPUT_KEYBOARD
            ) from exc
        finally:
            _restore_cursor(previous, (x, y))
        type_notes = [f"chars={len(request.text)}"]
        # Multi-line Win32 edits read back CRLF for every typed "\n".
        expected = _newlines(request.text)
        try:
            observed = _live_value(worker, handle)
            while request.replace_existing and observed is not None and _newlines(observed) != expected:
                if time.time() - started >= request.deadline_s:
                    raise UncertainEffect(
                        "text was dispatched but the field has not confirmed it; inspect before retrying",
                        mechanism=DispatchMechanism.SEND_INPUT_KEYBOARD,
                    )
                guard()
                time.sleep(0.01)
                observed = _live_value(worker, handle)
        except UncertainEffect:
            raise
        except BaseException as exc:
            raise UncertainEffect(
                f"text was dispatched but readback failed: {exc}",
                mechanism=DispatchMechanism.SEND_INPUT_KEYBOARD,
            ) from exc
        if observed is not None and _newlines(observed) != expected:
            type_notes.append("observed value differs from dispatched text")
        return _receipt(request, DispatchMechanism.SEND_INPUT_KEYBOARD, inserted, started, notes=tuple(type_notes))

    raise Pause(Reason.UNSUPPORTED_CONTROL, {"operation": request.operation.value})


def _restore_cursor(previous: tuple[int, int], placed: tuple[int, int]) -> None:
    """Put the pointer back only if it is still where the injected input left it.

    A person who moved the mouse meanwhile keeps the pointer where they put it.
    """
    try:
        if win32.cursor_position() == placed:
            win32.set_cursor_position(*previous)
    except DriverError:  # no readable cursor (another desktop has the input): leave it
        pass


def _newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _receipt(
    request: ActionRequest,
    mechanism: DispatchMechanism,
    inserted: int,
    started: float,
    notes: tuple[str, ...] = (),
) -> Receipt:
    return Receipt(
        action_id=request.action_id,
        dispatch_state=DispatchState.DISPATCHED,
        mechanism=mechanism,
        inserted_events=int(inserted),
        started_at=started,
        finished_at=time.time(),
        target={"element_id": request.element_id, "window_ref": request.window_ref},
        notes=notes,
    )


def _guard_foreground_app(driver: Any, request: ActionRequest, snapshot: Snapshot | None) -> None:
    """A chord must never leak into an application outside the approved scope."""
    foreground = win32.foreground_window()
    if not foreground:
        raise Pause(Reason.USER_TAKEOVER, {"reason": "no foreground window"})
    root = win32.root_window(foreground)
    handle = driver.registry.windows.get(request.window_ref or "")
    if (
        handle is None
        or root != win32.root_window(handle.hwnd)
        or snapshot is None
        or handle.app_ref != snapshot.app_ref
        or request.window_ref not in {window.window_ref for window in snapshot.windows}
    ):
        raise Pause(
            Reason.USER_TAKEOVER,
            {"reason": "foreground window is outside the approved application", "root": root},
        )


def _focus_window(driver: Any, request: ActionRequest, guard: Any, started: float) -> Receipt:
    handle = driver.registry.windows.get(request.window_ref or "")
    if handle is None:
        raise Pause(Reason.STALE_OBSERVATION, {"reason": "unknown window reference"})
    if not win32.user32.IsWindow(handle.hwnd):
        raise Pause(Reason.STALE_OBSERVATION, {"reason": "window no longer exists"})
    if not win32.user32.IsWindowEnabled(handle.hwnd):
        raise Pause(Reason.PERMISSION_BOUNDARY, {"reason": "window is disabled"})
    guard()
    activated = win32.activate_window(handle.hwnd)
    if not activated:
        # Activation may already have restored, raised, or reordered the window.
        raise UncertainEffect("window could not be activated; it may have been restored or raised")
    return _receipt(
        request,
        DispatchMechanism.NONE,
        int(activated),
        started,
        notes=("window activation, no synthetic input",),
    )


def _observed_option(
    driver: Any, snapshot: Snapshot, handle: uia.ElementHandle, label: str
) -> tuple[str, LiveState] | None:
    """Select only an unambiguous option belonging to the requested container."""
    wanted = label.strip().lower()
    best: tuple[str, LiveState] | None = None
    for element in snapshot.elements:
        if not element.name:
            continue
        if element.name.strip().lower() != wanted:
            continue
        candidate = driver.registry.elements.get(element.element_id)
        if (
            candidate is None
            or not candidate.visible
            or element.role not in {"listitem", "treeitem", "tabitem", "dataitem", "radiobutton"}
        ):
            continue
        try:
            if not uia.selection_belongs_to(driver.worker, candidate, handle):
                continue
            state = live_state(driver.worker, candidate)
        except Exception:
            continue
        if state.rect.is_empty or not state.enabled or state.offscreen:
            continue
        if not _geometry_ok(candidate.rect, state.rect) or not _hit_ok(driver.worker, candidate, *state.rect.center()):
            continue
        if best is not None:
            raise Pause(Reason.NO_APPROPRIATE_TARGET, {"detail": "ambiguous options in selection container"})
        best = (element.element_id, state)
    return best


def _verify_selection(worker: uia.UiaWorker, handle: uia.ElementHandle, label: str) -> None:
    value = _live_value(worker, handle)
    if value is None:
        return
    if label.strip().lower() != value.strip().lower():
        raise UncertainEffect(
            "selection was dispatched but the observed value does not match",
            mechanism=DispatchMechanism.SEND_INPUT_MOUSE,
        )


def _live_value(worker: uia.UiaWorker, handle: uia.ElementHandle) -> str | None:
    def _read(_worker: uia.UiaWorker) -> str | None:
        try:
            value = handle.element.GetCurrentPropertyValue(uia.PROP_VALUE)
            if not isinstance(value, str) or not value:
                value = handle.element.GetCurrentPropertyValue(uia.PROP_LEGACY_VALUE)
        except Exception:
            return None
        return value if isinstance(value, str) else None

    try:
        return worker.submit(_read, timeout=10.0)
    except Exception:
        return None
