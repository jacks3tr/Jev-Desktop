"""UI Automation observation on a dedicated MTA worker thread.

Design rules enforced here:

* Every UIA call runs on one COM MTA thread that owns no windows. Nothing else touches
  UIA objects, and no UIA object escapes this module.
* Observation is scoped to approved top-level windows of one process (plus its owned
  dialogs) and uses a cache request so properties and control patterns arrive together.
* Real elements and native handles stay server-side: callers only ever see opaque
  snapshot-bound identifiers.
* A desktop snapshot is never claimed to be atomic: the capture interval is recorded and
  callers must revalidate before input.
"""

from __future__ import annotations

import contextlib
import hashlib
import queue
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any

import comtypes
import comtypes.client

from ...contracts import (
    ContractError,
    Coverage,
    DriverError,
    ElementInfo,
    Geometry,
    Operation,
    Rect,
    Snapshot,
    WindowInfo,
    canonical_json,
    new_id,
    now,
)
from . import win32

_UIA_MODULE = None
_UIA_MODULE_LOCK = threading.Lock()


def uia_module() -> Any:
    """Lazily import the generated UI Automation client module (thread-safe)."""
    global _UIA_MODULE
    with _UIA_MODULE_LOCK:
        if _UIA_MODULE is None:
            comtypes.client.GetModule("UIAutomationCore.dll")
            from comtypes.gen import UIAutomationClient

            _UIA_MODULE = UIAutomationClient
        return _UIA_MODULE


def _prop(name: str, fallback: int) -> int:
    return int(getattr(uia_module(), name, fallback))


def pattern_id(name: str, fallback: int) -> int:
    return int(getattr(uia_module(), name, fallback))


# Property ids (fallbacks are the documented constants).
PROP_NAME = _prop("UIA_NamePropertyId", 30005)
PROP_CONTROL_TYPE = _prop("UIA_ControlTypePropertyId", 30003)
PROP_BOUNDS = _prop("UIA_BoundingRectanglePropertyId", 30001)
PROP_ENABLED = _prop("UIA_IsEnabledPropertyId", 30010)
PROP_OFFSCREEN = _prop("UIA_IsOffscreenPropertyId", 30022)
PROP_FOCUSED = _prop("UIA_HasKeyboardFocusPropertyId", 30008)
PROP_FOCUSABLE = _prop("UIA_IsKeyboardFocusablePropertyId", 30009)
PROP_VALUE = _prop("UIA_ValueValuePropertyId", 30045)
PROP_VALUE_READONLY = _prop("UIA_ValueIsReadOnlyPropertyId", 30046)
PROP_TOGGLE = _prop("UIA_ToggleToggleStatePropertyId", 30086)
PROP_SELECTED = _prop("UIA_SelectionItemIsSelectedPropertyId", 30079)
PROP_EXPANDED = _prop("UIA_ExpandCollapseExpandCollapseStatePropertyId", 30070)
PROP_PASSWORD = _prop("UIA_IsPasswordPropertyId", 30019)
PROP_CLASSNAME = _prop("UIA_ClassNamePropertyId", 30012)
PROP_NATIVE_HANDLE = _prop("UIA_NativeWindowHandlePropertyId", 30020)
PROP_RUNTIME_ID = _prop("UIA_RuntimeIdPropertyId", 30000)
PROP_FRAMEWORK = _prop("UIA_FrameworkIdPropertyId", 30024)
PROP_PROCESS_ID = _prop("UIA_ProcessIdPropertyId", 30002)
PROP_HELP_TEXT = _prop("UIA_HelpTextPropertyId", 30013)
PROP_ITEM_STATUS = _prop("UIA_ItemStatusPropertyId", 30026)
PROP_IS_MODAL = _prop("UIA_WindowIsModalPropertyId", 30029)

AVAILABILITY = {
    "invoke": _prop("UIA_IsInvokePatternAvailablePropertyId", 30031),
    "value": _prop("UIA_IsValuePatternAvailablePropertyId", 30043),
    "toggle": _prop("UIA_IsTogglePatternAvailablePropertyId", 30085),
    "selectionitem": _prop("UIA_IsSelectionItemPatternAvailablePropertyId", 30078),
    "selection": _prop("UIA_IsSelectionPatternAvailablePropertyId", 30077),
    "scroll": _prop("UIA_IsScrollPatternAvailablePropertyId", 30072),
    "expandcollapse": _prop("UIA_IsExpandCollapsePatternAvailablePropertyId", 30069),
    "text": _prop("UIA_IsTextPatternAvailablePropertyId", 30066),
    "window": _prop("UIA_IsWindowPatternAvailablePropertyId", 30076),
}

CACHE_PROPERTIES = [
    PROP_NAME,
    PROP_CONTROL_TYPE,
    PROP_BOUNDS,
    PROP_ENABLED,
    PROP_OFFSCREEN,
    PROP_FOCUSED,
    PROP_FOCUSABLE,
    PROP_VALUE,
    PROP_VALUE_READONLY,
    PROP_TOGGLE,
    PROP_SELECTED,
    PROP_EXPANDED,
    PROP_PASSWORD,
    PROP_CLASSNAME,
    PROP_NATIVE_HANDLE,
    PROP_RUNTIME_ID,
    PROP_FRAMEWORK,
    PROP_PROCESS_ID,
    PROP_HELP_TEXT,
    PROP_ITEM_STATUS,
    PROP_IS_MODAL,
    *AVAILABILITY.values(),
]

CACHE_PATTERNS = [
    pattern_id("UIA_InvokePatternId", 10000),
    pattern_id("UIA_ValuePatternId", 10002),
    pattern_id("UIA_TogglePatternId", 10015),
    pattern_id("UIA_SelectionItemPatternId", 10010),
    pattern_id("UIA_SelectionPatternId", 10001),
    pattern_id("UIA_ScrollPatternId", 10004),
    pattern_id("UIA_ExpandCollapsePatternId", 10005),
    pattern_id("UIA_TextPatternId", 10014),
    pattern_id("UIA_WindowPatternId", 10009),
]

CONTROL_TYPES: dict[int, str] = {
    50000: "button",
    50001: "calendar",
    50002: "checkbox",
    50003: "combobox",
    50004: "edit",
    50005: "hyperlink",
    50006: "image",
    50007: "listitem",
    50008: "list",
    50009: "menu",
    50010: "menubar",
    50011: "menuitem",
    50012: "progressbar",
    50013: "radiobutton",
    50014: "scrollbar",
    50015: "slider",
    50016: "spinner",
    50017: "statusbar",
    50018: "tab",
    50019: "tabitem",
    50020: "text",
    50021: "toolbar",
    50022: "tooltip",
    50023: "tree",
    50024: "treeitem",
    50025: "custom",
    50026: "group",
    50027: "thumb",
    50028: "datagrid",
    50029: "dataitem",
    50030: "document",
    50031: "splitbutton",
    50032: "window",
    50033: "pane",
    50034: "header",
    50035: "headeritem",
    50036: "table",
    50037: "titlebar",
    50038: "separator",
}

CLICKABLE_ROLES = {
    "button",
    "splitbutton",
    "hyperlink",
    "menuitem",
    "listitem",
    "tabitem",
    "treeitem",
    "checkbox",
    "radiobutton",
    "dataitem",
}
TEXT_ROLES = {"edit", "document", "text", "statusbar", "tooltip"}
# Roles that genuinely accept typed text. Trusting the Value pattern alone is wrong: shell
# navigation trees, file lists, and column headers all expose Value, which turned 151 tree
# items into "text fields" in a real Notepad Save As dialog and buried the file name field.
TEXT_ENTRY_ROLES = {"edit", "document", "combobox", "spinner"}

# Controls a test can act on, as opposed to content rows. Measured on a real Notepad Save As
# dialog: the file list holds hundreds of rows, and a depth-first walk spent the entire element
# budget on them, so the dialog's own Save button never appeared in the observation at all.
CHROME_ROLES = {
    "button",
    "splitbutton",
    "edit",
    "combobox",
    "checkbox",
    "radiobutton",
    "menuitem",
    "tabitem",
    "tab",
    "hyperlink",
    "slider",
    "spinner",
    "scrollbar",
    "thumb",
    "titlebar",
    "menubar",
    "toolbar",
}
# Rows are the cheap, numerous thing a big list produces, and the only thing the share below
# applies to. Text, status, and document values stay: they are what assertions read, and they
# are few. Containers are structural: their children matter, the container itself does not.
ROW_ROLES = {"listitem", "treeitem", "dataitem"}
CONTAINER_ROLES = {"pane", "group", "custom", "table", "tree", "list", "datagrid"}
CONTENT_SHARE = 0.25  # of the element budget, with a floor so short lists stay complete


@dataclass
class ElementHandle:
    """Server-side element record. Never leaves the driver."""

    element_id: str
    element: Any
    runtime_id: tuple[int, ...]
    snapshot_id: str
    window_ref: str
    hwnd: int
    role: str
    name: str
    rect: Rect
    enabled: bool
    visible: bool
    operations: tuple[str, ...]
    created_at: float


@dataclass
class WindowHandle:
    window_ref: str
    hwnd: int
    app_ref: str
    process_id: int


@dataclass
class Registry:
    elements: dict[str, ElementHandle] = field(default_factory=dict)
    snapshots: dict[str, set[str]] = field(default_factory=dict)
    windows: dict[str, WindowHandle] = field(default_factory=dict)
    current_snapshot: dict[str, str] = field(default_factory=dict)  # app_ref -> snapshot_id
    next_index: int = 1

    def clear(self) -> None:
        self.elements.clear()
        self.snapshots.clear()
        self.windows.clear()
        self.current_snapshot.clear()
        self.next_index = 1


class UiaWorker:
    """Owns the COM MTA thread and serializes every UI Automation call onto it."""

    def __init__(self) -> None:
        self._queue: queue.Queue[tuple[Callable[[Any], Any] | None, Future]] = queue.Queue()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._automation: Any = None
        self._cache: Any = None
        self._walker: Any = None
        self._true_condition: Any = None
        self._error: BaseException | None = None
        self._hi_epoch = 0
        self._com_threading = False

    # -- lifecycle ----------------------------------------------------------------

    def start(self, timeout: float = 20.0) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="uia-mta", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            raise DriverError("UI Automation worker did not initialize in time")
        if self._error is not None:
            raise DriverError(f"UI Automation worker failed to start: {self._error}")

    def stop(self, timeout: float = 5.0) -> None:
        if self._thread is None:
            return
        self._queue.put((None, Future()))
        self._thread.join(timeout)
        self._thread = None

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def submit(self, fn: Callable[[Any], Any], timeout: float = 30.0) -> Any:
        if self._thread is None:
            raise DriverError("UI Automation worker is not running")
        future: Future = Future()
        self._queue.put((fn, future))
        try:
            return future.result(timeout)
        except TimeoutError as exc:
            raise DriverError("UI Automation call timed out; worker state unknown") from exc

    # -- worker thread ------------------------------------------------------------

    def _run(self) -> None:
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except OSError as exc:  # pragma: no cover - apartment already initialized
            self._error = exc
            self._ready.set()
            return
        try:
            UIA = uia_module()
            self._automation = comtypes.client.CreateObject(UIA.CUIAutomation, interface=UIA.IUIAutomation)
            option = getattr(UIA, "UIAutomationGlobalOption_UseComThreading", None)
            if option is not None:
                try:
                    self._automation.SetGlobalOptions(option)
                    self._com_threading = True
                except Exception:  # optional optimization only
                    self._com_threading = False
            self._cache = self._automation.CreateCacheRequest()
            for prop in CACHE_PROPERTIES:
                self._cache.AddProperty(prop)
            for pattern in CACHE_PATTERNS:
                self._cache.AddPattern(pattern)
            self._true_condition = self._automation.CreateTrueCondition()
            self._hi_epoch = 0
        except BaseException as exc:  # pragma: no cover - environment failure
            self._error = exc
            self._ready.set()
            return
        self._ready.set()
        while True:
            fn, future = self._queue.get()
            if fn is None:
                break
            if future.cancelled():
                continue
            try:
                future.set_result(fn(self))
            except BaseException as exc:
                future.set_exception(exc)
        with contextlib.suppress(Exception):  # pragma: no cover - teardown only
            comtypes.CoUninitialize()

    # -- helpers executed on the worker thread -------------------------------------

    @property
    def automation(self) -> Any:
        if self._automation is None:
            raise DriverError("UI Automation is not initialized")
        return self._automation

    @property
    def has_cached_walker(self) -> bool:
        """Whether scoped traversal through the cache request works on this host."""
        return self._automation is not None

    def element_from_handle(self, hwnd: int) -> Any:
        return self.automation.ElementFromHandle(hwnd)

    def element_at_point(self, x: int, y: int) -> Any | None:
        point = uia_module().tagPOINT(x, y)
        try:
            return self.automation.ElementFromPoint(point)
        except Exception:
            return None

    def children(self, element: Any) -> list[Any]:
        """Direct children with every cached property/pattern already attached.

        One COM call per node instead of one per property: the cache request resolves
        properties and patterns together, as Microsoft's caching guidance describes.
        """
        UIA = uia_module()
        try:
            array = element.FindAllBuildCache(UIA.TreeScope_Children, self._true_condition, self._cache)
        except Exception as exc:
            raise DriverError(f"cached traversal failed: {exc}") from exc
        return [array.GetElement(index) for index in range(array.Length)]


# --------------------------------------------------------------------------------------
# Property extraction
# --------------------------------------------------------------------------------------


def _cached(element: Any, prop: int) -> Any:
    try:
        return element.GetCachedPropertyValue(prop)
    except Exception:
        try:
            return element.GetCurrentPropertyValue(prop)
        except Exception:
            return None


def _rect_of(value: Any) -> Rect:
    """UI Automation reports BoundingRectangle as double[4] = left, top, width, height."""
    if value is None:
        return Rect(0, 0, 0, 0)
    if isinstance(value, (list, tuple)) and len(value) >= 4:
        left, top, width, height = (float(value[0]), float(value[1]), float(value[2]), float(value[3]))
        return Rect(int(left), int(top), int(left + width), int(top + height))
    struct_left = getattr(value, "left", None)
    if struct_left is not None:
        return Rect(int(value.left), int(value.top), int(value.right), int(value.bottom))
    return Rect(0, 0, 0, 0)


def _runtime_id(element: Any) -> tuple[int, ...]:
    value = _cached(element, PROP_RUNTIME_ID)
    if value is None:
        try:
            value = element.GetRuntimeId()
        except Exception:
            return ()
    if isinstance(value, (list, tuple)):
        return tuple(int(item) for item in value)
    return ()


def _bool(value: Any, default: bool = False) -> bool:
    return bool(value) if isinstance(value, (bool, int)) else default


def _state_of(element: Any, available: Mapping[str, bool] | None = None) -> dict[str, Any]:
    """Pattern state, included only when the backing pattern actually exists."""
    available = available or {}
    state: dict[str, Any] = {}
    if available.get("toggle"):
        toggle = _cached(element, PROP_TOGGLE)
        if isinstance(toggle, int):
            state["checked"] = {0: "off", 1: "on", 2: "indeterminate"}.get(toggle, str(toggle))
    if available.get("selectionitem"):
        selected = _cached(element, PROP_SELECTED)
        if isinstance(selected, bool):
            state["selected"] = selected
    if available.get("expandcollapse"):
        expanded = _cached(element, PROP_EXPANDED)
        if isinstance(expanded, int) and expanded != 3:  # 3 = leaf node, not a state
            state["expanded"] = {0: "collapsed", 1: "expanded", 2: "partially"}.get(expanded, str(expanded))
    if available.get("value"):
        readonly = _cached(element, PROP_VALUE_READONLY)
        if isinstance(readonly, bool):
            state["readonly"] = readonly
    status = _cached(element, PROP_ITEM_STATUS)
    if isinstance(status, str) and status:
        state["item_status"] = status[:400]
    help_text = _cached(element, PROP_HELP_TEXT)
    if isinstance(help_text, str) and help_text:
        state["help"] = help_text[:400]
    return state


def _value_of(element: Any, password: bool) -> str | None:
    """Empty strings are treated as absent: unavailable properties report '' via cache."""
    if password:
        return None
    value = _cached(element, PROP_VALUE)
    if isinstance(value, str) and value:
        return value
    return None


def _operations_for(role: str, available: Mapping[str, bool], editable: bool) -> tuple[str, ...]:
    """Only operations the element genuinely supports and the driver implements."""
    ops: list[str] = []
    clickable = bool(available.get("invoke")) or role in CLICKABLE_ROLES
    if clickable:
        ops.append(Operation.CLICK.value)
    if editable and role in TEXT_ENTRY_ROLES:
        ops.append(Operation.TYPE_TEXT.value)
    if role in {"combobox", "list", "listitem", "tabitem", "radiobutton"} and (
        available.get("selectionitem") or available.get("expandcollapse") or available.get("selection")
    ):
        ops.append(Operation.SELECT.value)
    if available.get("toggle") or role in {"checkbox", "radiobutton"}:
        ops.append(Operation.TOGGLE.value)
    if available.get("scroll"):
        ops.append(Operation.SCROLL.value)
    if role == "window" or available.get("window"):
        ops.append(Operation.FOCUS_WINDOW.value)
    # de-duplicate, preserve order
    seen: set[str] = set()
    unique: list[str] = []
    for candidate in ops:
        if candidate not in seen:
            seen.add(candidate)
            unique.append(candidate)
    return tuple(unique)


def _control_type(element: Any) -> str:
    value = _cached(element, PROP_CONTROL_TYPE)
    if isinstance(value, int):
        return CONTROL_TYPES.get(value, f"control-{value}")
    return "unknown"


def _available_patterns(element: Any) -> dict[str, bool]:
    return {name: _bool(_cached(element, prop)) for name, prop in AVAILABILITY.items()}


# --------------------------------------------------------------------------------------
# Window discovery (runs on the worker thread)
# --------------------------------------------------------------------------------------


def discover_windows(registry: Registry, *, app_ref: str, process_ids: Iterable[int]) -> list[WindowInfo]:
    """Top-level windows for the given processes, including owned dialogs.

    Window references are stable across observations of the same HWND: the registry is
    authoritative, discovery only refreshes it. Native handles stay server-side.
    """
    pids = set(process_ids)
    by_hwnd = {handle.hwnd: ref for ref, handle in registry.windows.items()}
    collected: list[tuple[int, int, WindowInfo]] = []
    for hwnd in win32.enum_top_level_windows():
        pid = win32.window_process_id(hwnd)
        if pid not in pids:
            continue
        if not win32.is_top_level(hwnd) or win32.is_owned_popup(hwnd) or win32.is_cloaked(hwnd):
            continue
        if not win32.user32.IsWindowVisible(hwnd):
            continue
        owner = win32.owner_window(hwnd)
        if owner and win32.window_process_id(owner) not in pids:
            owner = 0
        ref = by_hwnd.get(hwnd)
        if ref is None:
            ref = new_id("win")
            registry.windows[ref] = WindowHandle(window_ref=ref, hwnd=hwnd, app_ref=app_ref, process_id=pid)
        collected.append(
            (
                hwnd,
                owner,
                WindowInfo(
                    window_ref=ref,
                    app_ref=app_ref,
                    title=win32.window_title(hwnd),
                    class_name=win32.window_class(hwnd),
                    process_id=pid,
                    modal=bool(owner and not win32.user32.IsWindowEnabled(owner)),
                    owner_window_ref=None,
                    focused=win32.foreground_window() == hwnd,
                    visible=True,
                    enabled=bool(win32.user32.IsWindowEnabled(hwnd)),
                    rect=win32.window_rect(hwnd),
                    scope="dialog" if owner else "scoped",
                ),
            )
        )
    windows = [_with_owner(info, by_hwnd[owner]) if owner in by_hwnd else info for _hwnd, owner, info in collected]
    return windows


def _with_owner(window: WindowInfo, owner_ref: str) -> WindowInfo:
    return WindowInfo(
        window_ref=window.window_ref,
        app_ref=window.app_ref,
        title=window.title,
        class_name=window.class_name,
        process_id=window.process_id,
        modal=window.modal,
        owner_window_ref=owner_ref,
        focused=window.focused,
        visible=window.visible,
        enabled=window.enabled,
        rect=window.rect,
        scope=window.scope,
    )


def observe(
    worker: UiaWorker,
    registry: Registry,
    *,
    app_ref: str,
    process_id: int,
    scope_windows: list[tuple[str, int]],
    max_elements: int,
    max_depth: int,
    text_limit: int,
    include_invisible: bool,
    geometry: Geometry,
) -> Snapshot:
    """Scoped, cached, indexed observation of the approved application."""

    def _build(_worker: UiaWorker) -> Snapshot:
        started = time.perf_counter()
        snapshot_id = new_id("snap")
        registry.next_index = 1  # element indexes are per-observation, never cumulative
        window_infos: list[WindowInfo] = []
        truncation: list[str] = []
        coverage = Coverage.COMPLETE
        elements: list[ElementInfo] = []
        deferred: list[ElementInfo] = []
        texts: list[dict[str, Any]] = []
        skipped: list[int] = []
        foreground = win32.foreground_window()

        # Dialogs and the focused window come first: when a budget runs out, it should run out
        # on background content rather than on the window the user is working in.
        ordered_windows = sorted(
            scope_windows,
            key=lambda item: 0 if win32.owner_window(item[1]) else 2 if win32.foreground_window() == item[1] else 1,
        )
        content_cap = max(20, int(max_elements * CONTENT_SHARE))
        content_seen = 0
        rows_dropped: list[int] = []
        for window_ref, hwnd in ordered_windows:
            owner_hwnd = win32.owner_window(hwnd)
            window_infos.append(
                WindowInfo(
                    window_ref=window_ref,
                    app_ref=app_ref,
                    title=win32.window_title(hwnd),
                    class_name=win32.window_class(hwnd),
                    process_id=win32.window_process_id(hwnd),
                    modal=bool(owner_hwnd and not win32.user32.IsWindowEnabled(owner_hwnd)),
                    owner_window_ref=None,
                    focused=foreground == hwnd,
                    visible=bool(win32.user32.IsWindowVisible(hwnd)),
                    enabled=bool(win32.user32.IsWindowEnabled(hwnd)),
                    rect=win32.window_rect(hwnd),
                    scope="dialog" if owner_hwnd else "scoped",
                )
            )
            try:
                root = worker.element_from_handle(hwnd)
            except Exception as exc:  # pragma: no cover - provider failure
                truncation.append(f"window {window_ref}: {exc}")
                coverage = Coverage.PARTIAL
                continue
            if not worker.has_cached_walker:  # pragma: no cover - fallback path
                truncation.append("cached tree walker unavailable; using flat search")
                coverage = Coverage.PARTIAL
            content_seen = _walk(
                worker,
                root,
                registry,
                snapshot_id,
                window_ref,
                hwnd,
                elements,
                deferred,
                texts,
                depth=0,
                max_depth=max_depth,
                max_elements=max_elements,
                include_invisible=include_invisible,
                text_limit=text_limit,
                truncation=truncation,
                skipped=skipped,
                rows_dropped=rows_dropped,
                content_cap=content_cap,
                content_seen=content_seen,
            )

        # Actionable elements are all in `elements` by now; content fills what is left, so a
        # dialog's own buttons are never crowded out by a file list that happens to sit earlier
        # in the tree.
        room = max(0, max_elements - len(elements))
        if len(deferred) > room:
            rows_dropped.append(len(deferred) - room)
            coverage = Coverage.TRUNCATED if coverage is Coverage.TRUNCATED else Coverage.PARTIAL
        elements.extend(deferred[:room])

        if rows_dropped:
            coverage = Coverage.TRUNCATED if coverage is Coverage.TRUNCATED else Coverage.PARTIAL
            truncation.append(f"{len(rows_dropped)} list rows were not observed (row share of the element budget)")
        if skipped:
            coverage = Coverage.TRUNCATED if coverage is Coverage.TRUNCATED else Coverage.PARTIAL
            truncation.append(f"{len(skipped)} offscreen elements were not observed")

        if len(elements) >= max_elements:
            coverage = Coverage.TRUNCATED
            truncation.append(f"element cap reached ({max_elements})")

        interval_ms = int((time.perf_counter() - started) * 1000)
        fingerprint = hashlib.blake2s(
            canonical_json(
                [[e.role, e.name, e.value, e.enabled, e.visible, e.rect.to_json(), e.state] for e in elements]
                + [[w.title, w.rect.to_json(), w.enabled, w.modal] for w in window_infos]
            ).encode("utf-8"),
            digest_size=16,
        ).hexdigest()

        snapshot = Snapshot(
            snapshot_id=snapshot_id,
            app_ref=app_ref,
            captured_at=now(),
            interval_ms=interval_ms,
            geometry=geometry,
            fingerprint=fingerprint,
            coverage=coverage,
            truncation=tuple(truncation),
            windows=tuple(window_infos),
            elements=tuple(elements),
            context={
                "texts": texts,
                "focused_element_id": next((e.element_id for e in elements if e.focused), None),
                "modal_windows": [w.window_ref for w in window_infos if w.modal],
                "foreground_window_ref": next((w.window_ref for w in window_infos if w.focused), None),
            },
            notes=(),
        )
        registry.snapshots[snapshot_id] = {e.element_id for e in elements}
        registry.current_snapshot[app_ref] = snapshot_id
        return snapshot

    if not scope_windows:
        raise DriverError("no windows in scope for observation")
    return worker.submit(_build)


def _walk(
    worker: UiaWorker,
    element: Any,
    registry: Registry,
    snapshot_id: str,
    window_ref: str,
    hwnd: int,
    elements: list[ElementInfo],
    pending: list[ElementInfo],
    texts: list[dict[str, Any]],
    *,
    depth: int,
    max_depth: int,
    max_elements: int,
    include_invisible: bool,
    text_limit: int,
    truncation: list[str],
    path: tuple[str, ...] = (),
    skipped: list[int] | None = None,
    rows_dropped: list[int] | None = None,
    content_cap: int = 0,
    content_seen: int = 0,
) -> int:
    if len(elements) >= max_elements or depth > max_depth:
        if depth > max_depth:
            truncation.append(f"depth cap reached ({max_depth})")
        return content_seen
    role = _control_type(element)
    if role in ROW_ROLES:
        content_seen += 1
    name_value = _cached(element, PROP_NAME)
    name = name_value if isinstance(name_value, str) else ""
    if role in CONTAINER_ROLES and len(elements) >= max_elements:
        # A structural element past the budget: its children still matter, the container does
        # not, so descend without recording it.
        for child in worker.children(element):
            content_seen = _walk(
                worker,
                child,
                registry,
                snapshot_id,
                window_ref,
                hwnd,
                elements,
                pending,
                texts,
                depth=depth + 1,
                max_depth=max_depth,
                max_elements=max_elements,
                include_invisible=include_invisible,
                text_limit=text_limit,
                truncation=truncation,
                path=(*path, name or role),
                skipped=skipped,
                rows_dropped=rows_dropped,
                content_cap=content_cap,
                content_seen=content_seen,
            )
        return content_seen
    password = _bool(_cached(element, PROP_PASSWORD))
    value = _value_of(element, password)
    enabled = _bool(_cached(element, PROP_ENABLED), True)
    offscreen = _bool(_cached(element, PROP_OFFSCREEN), False)
    rect = _rect_of(_cached(element, PROP_BOUNDS))
    focusable = _bool(_cached(element, PROP_FOCUSABLE))
    focused = _bool(_cached(element, PROP_FOCUSED))
    available = _available_patterns(element)
    state = _state_of(element, available)
    readonly = bool(state.get("readonly"))
    editable = (available.get("value") and not readonly and role in TEXT_ENTRY_ROLES) or (
        role == "edit" and not readonly
    )
    visible = (not offscreen) and not rect.is_empty
    if not visible and not include_invisible:
        # Offscreen content is skipped, so the observation is explicitly partial: absence of an
        # element in a partial observation is never proof of absence.
        if skipped is not None:
            skipped.append(1)
        for child in worker.children(element):
            content_seen = _walk(
                worker,
                child,
                registry,
                snapshot_id,
                window_ref,
                hwnd,
                elements,
                pending,
                texts,
                depth=depth + 1,
                max_depth=max_depth,
                max_elements=max_elements,
                include_invisible=include_invisible,
                text_limit=text_limit,
                truncation=truncation,
                path=(*path, name or role),
                skipped=skipped,
                content_cap=content_cap,
                content_seen=content_seen,
            )
        return content_seen

    element_id = new_id("el")
    native_handle = _cached(element, PROP_NATIVE_HANDLE)
    element_hwnd = int(native_handle) if isinstance(native_handle, int) and native_handle else hwnd
    operations = _operations_for(role, available, editable)
    registry.elements[element_id] = ElementHandle(
        element_id=element_id,
        element=element,
        runtime_id=_runtime_id(element),
        snapshot_id=snapshot_id,
        window_ref=window_ref,
        hwnd=element_hwnd,
        role=role,
        name=name,
        rect=rect,
        enabled=enabled,
        visible=visible,
        operations=operations,
        created_at=now(),
    )
    text: str | None = None
    if role in TEXT_ROLES or value:
        raw = value or name
        text = raw[:text_limit] if text_limit else raw[:0]
        if raw and len(raw) > len(text):
            truncation.append(f"text truncated for {role}")
    element_text = (text or None) if (role in TEXT_ROLES or value) else None
    (elements if role in CHROME_ROLES else pending).append(
        ElementInfo(
            element_id=element_id,
            window_ref=window_ref,
            role=role,
            name=name,
            value=None if password else value,
            enabled=enabled,
            visible=visible,
            editable=bool(editable),
            focusable=focusable,
            focused=focused,
            operations=operations,
            rect=rect,
            index=registry.next_index,
            path=path[-4:],
            state=state,
            text=element_text,
            truncation=None if element_text is None or text is None or len(text) < text_limit else "length",
        )
    )
    registry.next_index += 1
    if element_text and len(texts) < 40:
        texts.append({"element_id": element_id, "role": role, "name": name, "text": element_text})

    for child in worker.children(element):
        content_seen = _walk(
            worker,
            child,
            registry,
            snapshot_id,
            window_ref,
            hwnd,
            elements,
            pending,
            texts,
            depth=depth + 1,
            max_depth=max_depth,
            max_elements=max_elements,
            include_invisible=include_invisible,
            text_limit=text_limit,
            truncation=truncation,
            path=(*path, name or role),
            skipped=skipped,
            rows_dropped=rows_dropped,
            content_cap=content_cap,
            content_seen=content_seen,
        )
    return content_seen


def resolve_element(registry: Registry, element_id: str, snapshot_id: str | None, app_ref: str) -> ElementHandle:
    handle = registry.elements.get(element_id)
    if handle is None:
        raise ContractError(f"unknown element reference {element_id}")
    current = registry.current_snapshot.get(app_ref)
    if snapshot_id != current or handle.snapshot_id != current:
        raise DriverError("stale observation: re-observe before using this element reference")
    return handle


def live_rect(worker: UiaWorker, handle: ElementHandle) -> Rect:
    def _read(_worker: UiaWorker) -> Rect:
        return _rect_of(handle.element.GetCurrentPropertyValue(PROP_BOUNDS))

    return worker.submit(_read, timeout=10.0)


def hit_test(worker: UiaWorker, x: int, y: int) -> tuple[int, ...]:
    def _hit(_worker: UiaWorker) -> tuple[int, ...]:
        element = worker.element_at_point(x, y)
        if element is None:
            return ()
        try:
            return _runtime_id(element)
        except Exception:
            return ()

    return worker.submit(_hit, timeout=10.0)


def run_on_worker(worker: UiaWorker, fn: Callable[[UiaWorker], Any], timeout: float = 20.0) -> Any:
    return worker.submit(fn, timeout=timeout)


def is_descendant_or_self(hit: tuple[int, ...], target: tuple[int, ...]) -> bool:
    """Runtime ids are hierarchical paths: a prefix match means descendant-or-self."""
    if not hit or not target:
        return False
    return tuple(hit[: len(target)]) == tuple(target)
