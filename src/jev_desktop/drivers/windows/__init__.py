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

__all__ = ["WindowsDriver", "capture_module", "identity_module", "input_module", "uia", "win32"]


class WindowsDriver:
    def __init__(self, *, evidence_dir: str | Path | None = None) -> None:
        self.worker = uia.UiaWorker()
        self.registry = uia.Registry()
        self.screen = capture_module.ScreenCapture()
        self.evidence_dir = Path(evidence_dir) if evidence_dir else Path.cwd() / ".artifacts"
        self._apps: dict[str, AppRef] = {}
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
        self._app_by_key.clear()
        self._pid_by_app.clear()
        self._scope.clear()
        self._started = False

    def __enter__(self) -> WindowsDriver:
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
        return pid

    def list_apps(self) -> list[AppRef]:
        self._require_started()
        pids: set[int] = set()
        for hwnd in win32.enum_top_level_windows():
            if not win32.is_top_level(hwnd) or not win32.user32.IsWindowVisible(hwnd):
                continue
            if win32.is_owned_popup(hwnd) or win32.is_cloaked(hwnd):
                continue
            pids.add(win32.window_process_id(hwnd))
        apps: list[AppRef] = []
        for pid in sorted(pids):
            try:
                app_ref = self._register_app(pid)
            except DriverError:
                continue  # protected process: not observable, not selectable
            windows = uia.discover_windows(self.registry, app_ref=app_ref, process_ids={pid})
            app = self._apps[app_ref]
            apps.append(
                AppRef(
                    app_ref=app.app_ref,
                    package_identity=app.package_identity,
                    executable_path=app.executable_path,
                    process_id=app.process_id,
                    process_creation_time=app.process_creation_time,
                    window_refs=tuple(window.window_ref for window in windows),
                )
            )
            for window in windows:
                self._scope[window.window_ref] = (
                    self.registry.windows[window.window_ref].hwnd,
                    app_ref,
                )
        return apps

    def list_windows(self) -> list[WindowInfo]:
        self._require_started()
        windows: list[WindowInfo] = []
        for app in self.list_apps():
            pid = self.process_id_for(app.app_ref)
            windows.extend(uia.discover_windows(self.registry, app_ref=app.app_ref, process_ids={pid}))
        return windows

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
        windows = self._observe_windows(scope.app_ref)
        if scope.window_refs:
            wanted = set(scope.window_refs)
            windows = [window for window in windows if window.window_ref in wanted]
        elif scope.include_dialogs:
            # include every observed window of the process (dialogs already included)
            pass
        else:
            windows = [window for window in windows if window.scope != "dialog"]
        if not windows:
            raise DriverError("no windows in scope for observation")
        pairs = [(window.window_ref, self.registry.windows[window.window_ref].hwnd) for window in windows]
        return uia.observe(
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
        if region is None and scope is not None:
            try:
                windows = self._observe_windows(scope.app_ref)
                rects = [window.rect for window in windows if not window.rect.is_empty]
            except (DriverError, ContractError):
                rects = []
            if rects:
                region = Rect(
                    min(r.left for r in rects),
                    min(r.top for r in rects),
                    max(r.right for r in rects),
                    max(r.bottom for r in rects),
                )
        max_dimension = max(320, int(1600 * max(0.05, min(max_scale, 1.0))))
        directory = self.evidence_dir / run_id.replace(":", "_")
        directory.mkdir(parents=True, exist_ok=True)
        label = (checkpoint or "capture").replace("/", "-")[:40]
        path = directory / f"{label}-{int(time.time() * 1000)}-{new_id('ev').split(':')[1][:8]}.png"
        return self.screen.capture_to(
            str(path),
            run_id=run_id,
            checkpoint=checkpoint,
            description=description,
            region=region,
            snapshot_id=snapshot_id,
            max_dimension=max_dimension,
        )

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
        return input_module.execute(self, request, guard, snapshot)

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
