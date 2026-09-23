"""Windows driver facade: application binding, observation, input, capture, identity.

One instance owns exactly one UI Automation MTA worker and one element/window registry.
Native references never leave this package; callers receive opaque, snapshot-bound
identifiers and must re-observe after any state change.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ...contracts import (
    AppRef,
    Capture,
    ContractError,
    DriverError,
    EmergencyStop,
    ExpectedIdentity,
    IdentityReport,
    InputMode,
    Operation,
    Receipt,
    Rect,
    ScopeSpec,
    Snapshot,
    WindowInfo,
    new_id,
)
from . import capture as capture_module
from . import identity as identity_module
from . import input as input_module
from . import uia, win32


class NativeWindowsDriver:
    def __init__(self, *, evidence_dir: str | Path | None = None) -> None:
        self.worker = uia.UiaWorker()
        self.registry = uia.Registry()
        self.screen = capture_module.ScreenCapture()
        self.evidence_dir = Path(evidence_dir) if evidence_dir else Path.cwd() / ".artifacts"
        self._apps: dict[str, AppRef] = {}
        self._snapshots: dict[str, Snapshot] = {}
        self._captures: dict[str, Capture] = {}
        self._tiles: dict[str, dict[tuple[int, int], int]] = {}  # evidence_id -> source_rect tile checksums
        self._app_by_key: dict[tuple[int, float], str] = {}
        self._pid_by_app: dict[str, int] = {}
        self._scope: dict[str, tuple[int, str]] = {}  # window_ref -> (hwnd, app_ref)
        self._emergency = False
        self._started = False
        self._dpi_mode = "unknown"

    # -- lifecycle -----------------------------------------------------------------

    def start(self) -> None:
        if self._started:
            return
        self._dpi_mode = win32.set_dpi_awareness()
        self.worker.start()
        self._started = True

    def close(self) -> None:
        self.worker.stop()
        self.registry.clear()
        self._apps.clear()
        self._snapshots.clear()
        self._app_by_key.clear()
        self._pid_by_app.clear()
        self._scope.clear()
        self._started = False

    def __enter__(self) -> NativeWindowsDriver:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    def emergency_active(self) -> bool:
        return self._emergency

    def emergency_stop(self) -> None:
        """Local stop: refuse all further input on this driver immediately."""
        self._emergency = True

    def clear_emergency(self) -> None:
        self._emergency = False

    # -- application binding -------------------------------------------------------

    def _register_app(self, pid: int) -> str:
        try:
            creation = win32.process_creation_time(pid)
            executable = win32.process_image_path(pid)
        except DriverError:
            raise
        key = (pid, round(creation, 3))
        existing = self._app_by_key.get(key)
        if existing is not None:
            return existing
        app_ref = new_id("app")
        self._apps[app_ref] = AppRef(
            app_ref=app_ref,
            package_identity=None,
            executable_path=executable,
            process_id=pid,
            process_creation_time=creation,
        )
        self._app_by_key[key] = app_ref
        self._pid_by_app[app_ref] = pid
        return app_ref

    def process_id_for(self, app_ref: str) -> int:
        pid = self._pid_by_app.get(app_ref)
        if pid is None:
            raise ContractError(f"unknown or expired app reference {app_ref}")
        if abs(win32.process_creation_time(pid) - self._apps[app_ref].process_creation_time) > 0.001:
            raise ContractError("application process was replaced; inspect and bind the new process")
        return pid

    def bind_process(self, pid: int, creation_time: float) -> str:
        app_ref = self._register_app(pid)
        if abs(self._apps[app_ref].process_creation_time - creation_time) > 0.001:
            raise ContractError("launched process identity no longer matches")
        return app_ref

    def discover(self, app_ref: str | None = None) -> tuple[list[AppRef], list[WindowInfo]]:
        self._require_started()
        handles = win32.enum_top_level_windows()
        pids = (
            {self.process_id_for(app_ref)}
            if app_ref
            else {
                win32.window_process_id(hwnd)
                for hwnd in handles
                if win32.is_top_level(hwnd)
                and win32.user32.IsWindowVisible(hwnd)
                and not win32.is_owned_popup(hwnd)
                and not win32.is_cloaked(hwnd)
            }
        )
        apps: list[AppRef] = []
        windows: list[WindowInfo] = []
        for pid in sorted(pids):
            try:
                ref = self._register_app(pid)
            except DriverError:
                continue
            discovered = uia.discover_windows(self.registry, app_ref=ref, process_ids={pid}, handles=handles)
            app = self._apps[ref]
            apps.append(
                AppRef(
                    app_ref=ref,
                    package_identity=app.package_identity,
                    executable_path=app.executable_path,
                    process_id=pid,
                    process_creation_time=app.process_creation_time,
                    window_refs=tuple(window.window_ref for window in discovered),
                )
            )
            windows.extend(discovered)
            for window in discovered:
                self._scope[window.window_ref] = (self.registry.windows[window.window_ref].hwnd, ref)
        return apps, windows

    def list_apps(self) -> list[AppRef]:
        return self.discover()[0]

    def list_windows(self) -> list[WindowInfo]:
        return self.discover()[1]

    def _scoped_windows(self, scope: ScopeSpec, windows: list[WindowInfo]) -> list[WindowInfo]:
        if not scope.window_refs:
            return [w for w in windows if scope.include_dialogs or w.scope != "dialog"]
        allowed = set(scope.window_refs)
        if scope.include_dialogs:
            for _ in range(len(windows)):
                added = {w.window_ref for w in windows if w.app_ref == scope.app_ref and w.owner_window_ref in allowed}
                if added <= allowed:
                    break
                allowed.update(added)
        return [w for w in windows if w.app_ref == scope.app_ref and w.window_ref in allowed]

    def scoped_window_handles(self) -> set[int]:
        return {hwnd for hwnd, _app in self._scope.values()}

    def window_handle(self, window_ref: str) -> int:
        handle = self.registry.windows.get(window_ref)
        if handle is None:
            raise ContractError(f"unknown window reference {window_ref}")
        return handle.hwnd

    # -- observation ---------------------------------------------------------------

    def _observe_windows(self, app_ref: str) -> list[WindowInfo]:
        pid = self.process_id_for(app_ref)
        windows = uia.discover_windows(self.registry, app_ref=app_ref, process_ids={pid})
        for window in windows:
            self._scope[window.window_ref] = (self.registry.windows[window.window_ref].hwnd, app_ref)
        return windows

    def observe(self, scope: ScopeSpec) -> Snapshot:
        self._require_started()
        app = self._apps.get(scope.app_ref)
        if app and Path(app.executable_path).name.lower() == "applicationframehost.exe" and not scope.window_refs:
            raise ContractError(
                "shared application host requires explicit window_refs; process scope includes unrelated apps"
            )
        for _ in range(2):
            foreground_before = win32.foreground_window()
            windows = self._observe_windows(scope.app_ref)
            windows = self._scoped_windows(scope, windows)
            if not windows:
                raise DriverError("no windows in scope for observation")
            pairs = [(window.window_ref, self.registry.windows[window.window_ref].hwnd) for window in windows]
            snapshot = uia.observe(
                self.worker,
                self.registry,
                app_ref=scope.app_ref,
                process_id=self.process_id_for(scope.app_ref),
                scope_windows=pairs,
                max_elements=scope.max_elements,
                max_depth=scope.max_depth,
                text_limit=scope.text_limit,
                include_invisible=scope.include_invisible,
                geometry=self.screen.geometry(),
            )
            if win32.foreground_window() == foreground_before:
                break
        self._snapshots[scope.app_ref] = snapshot
        self._captures.clear()
        self._tiles.clear()
        return snapshot

    def snapshot(self, snapshot_id: str | None, scope: ScopeSpec) -> Snapshot:
        snapshot = self._snapshots.get(scope.app_ref)
        if snapshot is None or snapshot.snapshot_id != snapshot_id:
            raise ContractError("snapshot is unknown or stale; inspect the application again")
        allowed = {w.window_ref for w in self._scoped_windows(scope, self._observe_windows(scope.app_ref))}
        if any(window.window_ref not in allowed for window in snapshot.windows):
            raise ContractError("snapshot includes windows outside the run scope")
        return snapshot

    # -- capture -------------------------------------------------------------------

    def capture(
        self,
        *,
        scope: ScopeSpec | None,
        snapshot_id: str | None,
        region: Rect | None = None,
        max_scale: float = 1.0,
        run_id: str,
        checkpoint: str | None = None,
        description: str = "",
    ) -> Capture:
        self._require_started()
        if scope is None or snapshot_id is None:
            raise ContractError("capture requires an approved scope and snapshot")
        snapshot = self.snapshot(snapshot_id, scope)
        if any(element.visible and element.state.get("password") for element in snapshot.elements):
            raise DriverError("screenshot withheld because a password control is visible")
        if snapshot.geometry != self.screen.geometry():
            raise ContractError("display geometry changed; inspect again")
        if scope is not None:
            try:
                windows = self._observe_windows(scope.app_ref)
                windows = self._scoped_windows(scope, windows)
                foreground = win32.root_window(win32.foreground_window())
                windows = [
                    window
                    for window in windows
                    if win32.root_window(self.window_handle(window.window_ref)) == foreground
                ]
                rects = [window.rect for window in windows if not window.rect.is_empty]
            except (DriverError, ContractError):
                rects = []
            if rects:
                approved = Rect(
                    min(r.left for r in rects),
                    min(r.top for r in rects),
                    max(r.right for r in rects),
                    max(r.bottom for r in rects),
                )
                if region is not None and region.intersect(approved) != region:
                    raise ContractError("capture crop is outside the approved foreground window")
                region = region or approved.intersect(win32.frame_rect(foreground))
                observed = next(
                    (w for w in snapshot.windows if win32.root_window(self.window_handle(w.window_ref)) == foreground),
                    None,
                )
                if observed is None or observed.rect != approved:
                    raise ContractError("window moved since observation; inspect again")
                self._require_uncovered(foreground, region)
            else:
                raise DriverError("approved application is not foreground; refusing unrelated screen capture")
        max_dimension = max(320, int(1600 * max(0.05, min(max_scale, 1.0))))
        directory = self.evidence_dir / run_id.replace(":", "_")
        directory.mkdir(parents=True, exist_ok=True)
        label = (checkpoint or "capture").replace("/", "-")[:40]
        path = directory / f"{label}-{int(time.time() * 1000)}-{new_id('ev').split(':')[1][:8]}.png"
        capture, pixels = self.screen.capture_to(
            str(path),
            run_id=run_id,
            checkpoint=checkpoint,
            description=description,
            region=region,
            snapshot_id=snapshot_id,
            max_dimension=max_dimension,
        )
        if (
            win32.root_window(win32.foreground_window()) != foreground
            or win32.window_rect(foreground) != approved
            or self.screen.geometry() != snapshot.geometry
        ):
            path.unlink(missing_ok=True)
            raise ContractError("window changed during capture; inspect again")
        try:
            self._require_uncovered(foreground, region)
        except DriverError:
            path.unlink(missing_ok=True)
            raise
        self._captures[capture.evidence.evidence_id] = capture
        source = capture.source_rect
        self._tiles[capture.evidence.evidence_id] = capture_module.tile_checksums(source.width, source.height, pixels)
        return capture

    def _require_uncovered(self, hwnd: int, region: Rect) -> None:
        for candidate in win32.enum_top_level_windows():
            if candidate == hwnd:
                return
            if not win32.user32.IsWindowVisible(candidate) or win32.is_cloaked(candidate):
                continue
            try:
                rect = win32.frame_rect(candidate)
            except DriverError:  # closed since enumeration; it covers nothing
                continue
            if not rect.intersect(region).is_empty:
                raise DriverError("another window covers the approved capture region")
        raise DriverError("approved window is no longer visible")

    def resolve_point(self, request, snapshot: Snapshot) -> tuple[int, int]:
        point = request.point
        capture = self._captures.get(point.evidence_id)
        if capture is None:
            raise ContractError("screenshot is unknown or expired; inspect again")
        x, y = point.resolve(capture.evidence, request.run_id, request.snapshot_id)
        window = next((w for w in snapshot.windows if w.window_ref == request.window_ref), None)
        hwnd = self.window_handle(request.window_ref)
        if (
            window is None
            or self.screen.geometry() != capture.geometry
            or win32.window_rect(hwnd) != window.rect
            or not window.rect.contains(x, y)
            or win32.dpi_for_window(hwnd) != window.dpi
        ):
            raise ContractError("coordinate geometry is stale or outside the approved window")
        self._require_uncovered(win32.root_window(hwnd), capture.source_rect)
        if win32.root_window(win32.window_from_point(x, y)) != win32.root_window(hwnd):
            raise ContractError("coordinate is covered by another window")
        # Only the point's tile and its neighbours must match: a spinner elsewhere in the
        # window should not invalidate the screenshot, but a change under the point must.
        source, tile = capture.source_rect, capture_module.TILE
        column, row = max((x - source.left) // tile - 1, 0), max((y - source.top) // tile - 1, 0)
        near = Rect(
            source.left + column * tile,
            source.top + row * tile,
            min(source.left + (column + 3) * tile, source.right),
            min(source.top + (row + 3) * tile, source.bottom),
        )
        fresh = capture_module.tile_checksums(
            near.width, near.height, self.screen.grab_bgra(near), origin=(column, row)
        )
        expected = self._tiles.get(point.evidence_id, {})
        if any(expected.get(key) != checksum for key, checksum in fresh.items()):
            raise ContractError("screenshot content changed; inspect again before coordinate input")
        return x, y

    # -- identity ------------------------------------------------------------------

    def identity(self, app_ref: str, expected: ExpectedIdentity) -> IdentityReport:
        self._require_started()
        pid = self.process_id_for(app_ref)
        windows = self._observe_windows(app_ref)
        if not windows:
            raise DriverError("application has no observable windows")
        main = next((window for window in windows if window.scope != "dialog"), windows[0])
        return identity_module.verify_identity(
            app_ref,
            pid=pid,
            hwnd=self.registry.windows[main.window_ref].hwnd,
            expected=expected,
            title=main.title,
            class_name=main.class_name,
        )

    # -- execution -----------------------------------------------------------------

    def execute(
        self,
        request,
        guard: Callable[[], None],
        snapshot: Snapshot | None = None,
    ) -> Receipt:
        self._require_started()
        if self._emergency:
            raise EmergencyStop("local emergency stop is set; no input dispatched")

        def checked_guard(*, sends_input: bool = False) -> None:
            if snapshot is None:
                raise ContractError("native input requires a bound snapshot")
            # Authorization first: its IPC round-trip and ownership checkpoint are the slow
            # part, so the live target checks run after it, immediately before dispatch.
            guard()
            pid = self.process_id_for(snapshot.app_ref)
            hwnd = self.window_handle(request.window_ref)
            if win32.window_process_id(hwnd) != pid:
                raise ContractError("target window no longer belongs to the bound process")
            # Synthetic input follows the foreground window whatever the requested mode, so a
            # semantic HOTKEY is checked here too.
            if sends_input or (request.mode is InputMode.USER_PATH and request.operation is not Operation.FOCUS_WINDOW):
                input_module._guard_foreground_app(self, request, snapshot)

        previous_guard = win32._dispatch_guard
        win32._dispatch_guard = lambda: checked_guard(sends_input=True)
        try:
            return input_module.execute(self, request, checked_guard, snapshot)
        finally:
            win32._dispatch_guard = previous_guard

    # -- diagnostics ---------------------------------------------------------------

    def health(self) -> Mapping[str, Any]:
        geometry = self.screen.geometry()
        return {
            "started": self._started,
            "uia_alive": self.worker.alive,
            "dpi_awareness": self._dpi_mode,
            "geometry": geometry.to_json(),
            "tracked_apps": len(self._apps),
            "tracked_windows": len(self.registry.windows),
            "emergency": self._emergency,
        }

    def _require_started(self) -> None:
        if not self._started:
            raise DriverError("driver is not started")
        if self.worker._poisoned:
            raise DriverError("native worker timed out; restart the broker before further operations")
