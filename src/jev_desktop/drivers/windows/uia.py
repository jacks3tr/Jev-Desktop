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
PROP_LEGACY_VALUE = _prop("UIA_LegacyIAccessibleValuePropertyId", 30093)
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
    PROP_LEGACY_VALUE,
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
    "combobox",
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
# Value patterns also appear on navigation trees and lists. Require a text-entry role.
TEXT_ENTRY_ROLES = {"edit", "document", "combobox", "spinner"}

# Collect interactive controls before content rows consume the observation budget.
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
# Scan collections after controls, then retain focus and populated content before empty rows.
ROW_ROLES = {"listitem", "treeitem", "dataitem"}
CONTAINER_ROLES = {"pane", "group", "custom", "table", "tree", "list", "datagrid"}


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
        self._poisoned = False
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
        if self._thread.is_alive():
            self._poisoned = True
            raise DriverError("UI Automation worker is still running; ownership must be retained")
        self._thread = None

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def submit(self, fn: Callable[[Any], Any], timeout: float = 30.0) -> Any:
        if self._thread is None:
            raise DriverError("UI Automation worker is not running")
        if self._poisoned:
            raise DriverError("UI Automation worker timed out; restart the broker before further input")
        future: Future = Future()
        self._queue.put((fn, future))
        try:
            return future.result(timeout)
        except TimeoutError as exc:
            self._poisoned = True
            future.cancel()
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
            if not future.set_running_or_notify_cancel():
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

    def focused_element(self, hwnd: int) -> Any | None:
        if win32.root_window(win32.foreground_window()) != win32.root_window(hwnd):
            return None
        try:
            return self.automation.GetFocusedElementBuildCache(self._cache)
        except Exception:
            return None

    def children(self, element: Any) -> Iterable[Any]:
        """Fetch cached siblings lazily so a wide provider cannot allocate an entire row set."""
        try:
            walker = self.automation.ControlViewWalker
            child = walker.GetFirstChildElementBuildCache(element, self._cache)
            while child:
                yield child
                child = walker.GetNextSiblingElementBuildCache(child, self._cache)
        except Exception as exc:
            raise DriverError(f"cached traversal failed: {exc}") from exc


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
        try:
            return tuple(int(item) for item in value)
        except (TypeError, ValueError):
            return ()
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
    legacy = _cached(element, PROP_LEGACY_VALUE)
    return legacy if isinstance(legacy, str) and legacy else None


def _operations_for(role: str, available: Mapping[str, bool], editable: bool) -> tuple[str, ...]:
    """Return operations supported by both the element and the driver."""
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


def discover_windows(
    registry: Registry, *, app_ref: str, process_ids: Iterable[int], handles: Iterable[int] | None = None
) -> list[WindowInfo]:
    """Top-level windows for the given processes, including owned dialogs.

    Window references are stable across observations of the same HWND: the registry is
    authoritative, discovery only refreshes it. Native handles stay server-side.
    """
    pids = set(process_ids)
    by_hwnd = {handle.hwnd: ref for ref, handle in registry.windows.items()}
    collected: list[tuple[int, int, WindowInfo]] = []
    for hwnd in handles if handles is not None else win32.enum_top_level_windows():
        pid = win32.window_process_id(hwnd)
        if pid not in pids:
            continue
        if not win32.is_top_level(hwnd) or win32.is_owned_popup(hwnd) or win32.is_cloaked(hwnd):
            continue
        if not win32.user32.IsWindowVisible(hwnd):
            continue
        try:
            rect = win32.window_rect(hwnd)
        except DriverError:  # the window closed during enumeration
            continue
        owner = win32.owner_window(hwnd)
        if owner and win32.window_process_id(owner) not in pids:
            owner = 0
        ref = by_hwnd.get(hwnd)
        if ref is None:
            ref = new_id("win")
            registry.windows[ref] = WindowHandle(window_ref=ref, hwnd=hwnd, app_ref=app_ref, process_id=pid)
        by_hwnd[hwnd] = ref
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
                    rect=rect,
                    dpi=win32.dpi_for_window(hwnd),
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
        dpi=window.dpi,
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
        previous = registry.current_snapshot.pop(app_ref, None)
        for element_id in registry.snapshots.pop(previous or "", set()):
            registry.elements.pop(element_id, None)
        snapshot_id = new_id("snap")
        registry.next_index = 1  # element indexes are per-observation, never cumulative
        window_infos: list[WindowInfo] = []
        truncation: list[str] = []
        coverage = Coverage.COMPLETE
        elements: list[ElementInfo] = []
        deferred: list[ElementInfo] = []
        focused_elements: list[ElementInfo] = []
        texts: list[dict[str, Any]] = []
        skipped: list[int] = []
        foreground = win32.foreground_window()

        # Dialogs and the focused window come first: when a budget runs out, it should run out
        # on background content rather than on the window the user is working in.
        ordered_windows = sorted(
            scope_windows,
            key=lambda item: 0 if win32.owner_window(item[1]) else 1 if foreground == item[1] else 2,
        )
        content_cap = max_elements
        content_seen = 0
        traversal = [0]
        rows_dropped: list[int] = []
        for window_ref, hwnd in ordered_windows:
            try:
                rect = win32.window_rect(hwnd)
            except DriverError:
                truncation.append(f"window {window_ref} closed during observation")
                coverage = Coverage.PARTIAL
                continue
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
                    rect=rect,
                    dpi=win32.dpi_for_window(hwnd),
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
            get_focused = getattr(worker, "focused_element", None)
            focused_element = get_focused(hwnd) if get_focused is not None else None
            if focused_element is not None:
                focused_chrome: list[ElementInfo] = []
                focused_content: list[ElementInfo] = []
                _walk(
                    worker,
                    focused_element,
                    registry,
                    snapshot_id,
                    window_ref,
                    hwnd,
                    focused_chrome,
                    focused_content,
                    texts,
                    depth=0,
                    max_depth=0,
                    max_elements=max_elements,
                    include_invisible=include_invisible,
                    text_limit=text_limit,
                    truncation=[],
                    skipped=[],
                    rows_dropped=[],
                    content_cap=max_elements,
                    content_seen=0,
                    traversal=[0],
                )
                focused_elements.extend(focused_chrome + focused_content)
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
                traversal=traversal,
            )
        if not window_infos:
            raise DriverError("every window in scope closed during observation")

        # Actionable elements are all in `elements` by now; content fills what is left, so a
        # dialog's own buttons are never crowded out by a file list that happens to sit earlier
        # in the tree.
        observed_runtime_ids = {registry.elements[item.element_id].runtime_id for item in elements + deferred}
        for item in focused_elements:
            runtime_id = registry.elements[item.element_id].runtime_id
            if not runtime_id or runtime_id not in observed_runtime_ids:
                deferred.insert(0, item)
        deferred.sort(
            key=lambda item: (
                not item.focused,
                not item.state.get("selected", False),
                not bool(item.value),
                item.role not in ROW_ROLES,
            )
        )
        room = max(0, max_elements - len(elements))
        if len(deferred) > room:
            rows_dropped.append(len(deferred) - room)
            coverage = Coverage.TRUNCATED if coverage is Coverage.TRUNCATED else Coverage.PARTIAL
        elements.extend(deferred[:room])

        if rows_dropped:
            coverage = Coverage.TRUNCATED if coverage is Coverage.TRUNCATED else Coverage.PARTIAL
            truncation.append(f"{sum(rows_dropped)} list rows were not observed (collection row budget)")
        if skipped:
            coverage = Coverage.TRUNCATED if coverage is Coverage.TRUNCATED else Coverage.PARTIAL
            truncation.append(f"{len(skipped)} offscreen elements were not observed")

        if len(elements) >= max_elements:
            coverage = Coverage.TRUNCATED
            truncation.append(f"element cap reached ({max_elements})")

        retained = {e.element_id for e in elements}
        texts = [item for item in texts if item["element_id"] in retained]
        if truncation and coverage is Coverage.COMPLETE:
            coverage = Coverage.PARTIAL
        interval_ms = int((time.perf_counter() - started) * 1000)
        fingerprint = hashlib.blake2s(
            canonical_json(
                [
                    [e.window_ref, e.role, e.name, e.value, e.enabled, e.visible, e.focused, e.rect.to_json(), e.state]
                    for e in elements
                ]
                + [[w.window_ref, w.title, w.rect.to_json(), w.enabled, w.modal, w.focused] for w in window_infos]
            ).encode("utf-8"),
            digest_size=16,
        ).hexdigest()

        focus_ids = {registry.elements[item.element_id].runtime_id for item in focused_elements} - {()}
        actual_focus = next(
            (item.element_id for item in elements if registry.elements[item.element_id].runtime_id in focus_ids),
            next((item.element_id for item in elements if item.focused), None),
        )
        snapshot = Snapshot(
            snapshot_id=snapshot_id,
            app_ref=app_ref,
            captured_at=now(),
            interval_ms=interval_ms,
            geometry=geometry,
            fingerprint=fingerprint,
            coverage=coverage,
            truncation=tuple(dict.fromkeys(truncation)),
            windows=tuple(window_infos),
            elements=tuple(elements),
            context={
                "texts": texts,
                "focused_element_id": actual_focus,
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

    def build_and_release(current: UiaWorker) -> Snapshot:
        before = set(registry.elements)
        retained: set[str] = set()
        try:
            result = _build(current)
            retained = {element.element_id for element in result.elements}
            return result
        finally:
            for element_id in set(registry.elements) - before - retained:
                registry.elements.pop(element_id, None)

    return worker.submit(build_and_release)


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
    skipped: list[int],
    rows_dropped: list[int],
    content_cap: int,
    content_seen: int,
    traversal: list[int],
) -> int:
    stack: list[tuple[Any, int, tuple[str, ...]]] = [(iter((element,)), depth, ())]
    collections: list[tuple[Any, int, tuple[str, ...]]] = []
    collection_phase = False
    while stack or collections:
        if not stack:
            stack.append(collections.pop(0))
            collection_phase = True
        if traversal[0] >= max(64, max_elements * 4):
            truncation.append("traversal node budget reached")
            break
        if len(elements) >= max_elements:
            break
        iterator, depth, path = stack[-1]
        try:
            element = next(iterator)
        except StopIteration:
            stack.pop()
            continue
        except DriverError:
            # An element vanished mid-walk (UIA_E_ELEMENTNOTAVAILABLE): drop only its subtree.
            stack.pop()
            truncation.append("a subtree was not observed because its elements became unavailable")
            continue
        traversal[0] += 1
        role = _control_type(element)
        native_handle = _cached(element, PROP_NATIVE_HANDLE)
        if (
            role == "window"
            and isinstance(native_handle, int)
            and native_handle
            and native_handle != hwnd
            and win32.root_window(native_handle) == native_handle
        ):
            continue
        name_value = _cached(element, PROP_NAME)
        name = name_value if isinstance(name_value, str) else ""
        if depth < max_depth:
            children = (iter(worker.children(element)), depth + 1, (*path, name or role))
            if role in {"list", "tree", "datagrid", "table"}:
                collections.append(children)
            else:
                stack.append(children)
        else:
            truncation.append(f"depth cap reached ({max_depth})")
        if role in ROW_ROLES:
            content_seen += 1
            if content_seen > content_cap:
                rows_dropped.append(1)
                if collection_phase:
                    stack.clear()
                    truncation.append("remaining collection content was not observed (collection row budget)")
                elif depth < max_depth:
                    stack.pop()
                continue
        password = _bool(_cached(element, PROP_PASSWORD))
        value = _value_of(element, password)
        enabled = _bool(_cached(element, PROP_ENABLED), True)
        offscreen = _bool(_cached(element, PROP_OFFSCREEN), False)
        rect = _rect_of(_cached(element, PROP_BOUNDS))
        if offscreen and role in ROW_ROLES and not rect.is_empty:
            offscreen = not point_hits_element(worker, element, *rect.center())
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
            skipped.append(1)
            continue
        if role not in CHROME_ROLES and len(pending) >= max_elements + content_cap:
            truncation.append("content element cap reached")
            continue
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
                state={**state, "password": password},
                text=None if password else element_text,
                truncation=None if element_text is None or text is None or len(text) < text_limit else "length",
            )
        )
        registry.next_index += 1
        if element_text and len(texts) < 40:
            texts.append({"element_id": element_id, "role": role, "name": name, "text": element_text})

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


def point_hits_element(worker: UiaWorker, element: Any, x: int, y: int) -> bool:
    """Run on the COM worker. Prove visibility through the actual hit and its ancestors."""
    try:
        hit = worker.element_at_point(x, y)
        walker = worker.automation.RawViewWalker
        for _ in range(64):
            if not hit:
                return False
            if worker.automation.CompareElements(hit, element):
                return True
            hit = walker.GetParentElement(hit)
    except Exception:
        return False
    return False


def hit_is_descendant_or_self(
    worker: UiaWorker, target: ElementHandle, x: int, y: int, attempts: int = 3, settle_s: float = 0.1
) -> bool:
    # Chromium answers a point query from its previous hit or a z-order-blind approximation
    # while the exact hit test runs asynchronously, so the first answer over a modal, menu, or
    # palette can be an ancestor. Sample again before refusing; a real cover never passes.
    for attempt in range(attempts):
        if worker.submit(lambda _: point_hits_element(worker, target.element, x, y), timeout=10.0):
            return True
        if attempt + 1 < attempts:
            time.sleep(settle_s)
    return False


def selection_belongs_to(worker: UiaWorker, option: ElementHandle, container: ElementHandle) -> bool:
    def check(_worker: UiaWorker) -> bool:
        if not _bool(option.element.GetCurrentPropertyValue(AVAILABILITY["selectionitem"])):
            return False
        # Property exposes the owning selection container, including provider-linked popups.
        owner = option.element.GetCurrentPropertyValue(30080)
        if owner:
            try:
                if worker.automation.CompareElements(owner, container.element):
                    return True
            except Exception:
                pass
        current = option.element
        for _ in range(32):
            current = worker.automation.RawViewWalker.GetParentElement(current)
            if not current:
                break
            if worker.automation.CompareElements(current, container.element):
                return True
        return False

    return bool(worker.submit(check, timeout=10.0))
