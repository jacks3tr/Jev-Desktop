"""Bounded native-worker client. COM objects remain in a disposable child process."""

from __future__ import annotations

import _winapi
import multiprocessing
import os
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ...contracts import ContractError, DriverError, EmergencyStop, Pause, Reason, UncertainEffect
from .presence import DesktopPresence
from .worker import serve

_VENV_LAUNCHER = "__PYVENV_LAUNCHER__"


def _start_worker_process(process: Any) -> None:
    """Start the worker so that the tracked process is the interpreter running it.

    A venv's Scripts\\pythonw.exe is a redirector that runs the base interpreter as its own
    child: tracking it would leave the real worker an untracked grandchild, so terminate,
    join, lease duplication, and the reported pid would all target the redirector. Start the
    base pythonw.exe directly and name the venv it stands for, as multiprocessing itself does
    for sys.executable (bpo-35797).
    """
    venv = Path(sys.executable).with_name("pythonw.exe")
    base = Path(getattr(sys, "_base_executable", sys.executable)).with_name("pythonw.exe")
    if not base.is_file():
        raise DriverError("pythonw.exe is required to start the native worker without a console")
    multiprocessing.set_executable(str(base))
    if os.path.normcase(venv) == os.path.normcase(base):
        process.start()
        return
    # CreateProcess inherits this environment; restore it as soon as the child exists.
    previous = os.environ.get(_VENV_LAUNCHER)
    os.environ[_VENV_LAUNCHER] = str(venv)
    try:
        process.start()
    finally:
        if previous is None:
            os.environ.pop(_VENV_LAUNCHER, None)
        else:
            os.environ[_VENV_LAUNCHER] = previous


class WindowsDriver:
    def __init__(self, *, evidence_dir: str | Path | None = None) -> None:
        self.evidence_dir = str(evidence_dir or Path.cwd() / ".artifacts")
        self._lock = threading.RLock()
        self._lifecycle = threading.RLock()
        self._process: Any = None
        self._connection: Any = None
        self._busy = False
        self._lease_handle: int | None = None
        self._guard: Callable[[], None] | None = None
        self._deadline = float("inf")
        context = multiprocessing.get_context("spawn")
        self._pending_inputs = context.RawArray("B", 8192)
        self._pending_count = context.RawValue("i", 0)
        # Broker-side: the edge glow and physical-input watch live here, not in the worker.
        self.presence = DesktopPresence()

    def start(self) -> None:
        with self._lock, self._lifecycle:
            if self._process is not None:
                if not self._process.is_alive():
                    raise DriverError("native worker stopped; restart the broker and rebind the application")
                return
            context = multiprocessing.get_context("spawn")
            parent, child = context.Pipe()
            process = context.Process(
                target=serve, args=(child, self.evidence_dir, self._pending_inputs, self._pending_count), daemon=True
            )
            _start_worker_process(process)
            self._process = process
            self._connection = parent
            child.close()
        self._call("start")
        self.presence.start()

    def close(self) -> None:
        self.abort()
        self.presence.close()
        if self._connection is not None:
            self._connection.close()

    def abort(self) -> None:
        """Return only after the native process cannot issue more input."""
        with self._lifecycle:
            process = self._process
            if process is not None and process.is_alive():
                process.terminate()
                process.join(3.0)
                if process.is_alive():
                    raise DriverError("native worker did not stop; desktop lease must remain held")
            self._lease_handle = None
            if self._pending_count.value:
                from . import win32

                win32.release_pending(self._pending_inputs, self._pending_count)

    def quiesce(self) -> None:
        with self._lifecycle:
            if self._busy:
                self.abort()
            elif self._lease_handle is not None:
                if self._process is not None and self._process.is_alive():
                    duplicate = _winapi.DuplicateHandle(
                        self._process.sentinel,
                        self._lease_handle,
                        _winapi.GetCurrentProcess(),
                        0,
                        False,
                        _winapi.DUPLICATE_SAME_ACCESS | _winapi.DUPLICATE_CLOSE_SOURCE,
                    )
                    _winapi.CloseHandle(duplicate)
                self._lease_handle = None

    def retain_lease(self, handle: int) -> None:
        """Keep the OS lock alive until this worker exits even if its parent crashes."""
        with self._lifecycle:
            if self._process is None or not self._process.is_alive():
                raise DriverError("cannot lease the desktop without a live native worker")
            if self._lease_handle is not None:
                raise DriverError("native worker already holds a lease handle")
            self._lease_handle = _winapi.DuplicateHandle(
                _winapi.GetCurrentProcess(), handle, self._process.sentinel, 0, False, _winapi.DUPLICATE_SAME_ACCESS
            )

    def emergency_stop(self) -> None:
        from ...ownership import emergency_signal

        emergency_signal()
        self.quiesce()

    def set_boundary(self, guard: Callable[[], None] | None, timeout: float = float("inf")) -> None:
        self._guard = guard
        self._deadline = time.monotonic() + max(0.0, timeout)

    def health(self) -> dict[str, Any]:
        return {
            "started": self._process is not None and self._process.is_alive(),
            "worker_pid": self._process.pid if self._process else None,
            "busy": self._busy,
        }

    def _call(self, method: str, *args: Any, guard: Callable[[], None] | None = None, **kwargs: Any) -> Any:
        with self._lock:
            boundary = guard or self._guard
            deadline = min(self._deadline, time.monotonic() + 30.0)
            if method == "execute":
                deadline = min(deadline, time.monotonic() + args[0].deadline_s)
            if boundary:
                boundary()
            with self._lifecycle:
                if self._process is None or not self._process.is_alive():
                    raise DriverError("native worker unavailable; restart broker and inspect again")
                self._busy = True
            approved_dispatch = False
            received_response = False
            poisoned = False
            try:
                self._connection.send((method, args, kwargs))
                while True:
                    if boundary:
                        boundary()
                    if time.monotonic() >= deadline:
                        raise Pause(Reason.BUDGET_EXHAUSTED, {"budget": "native_deadline"})
                    if not self._connection.poll(0.025):
                        if not self._process.is_alive():
                            raise DriverError("native worker exited without a result")
                        continue
                    kind, payload = self._connection.recv()
                    if kind == "guard":
                        if boundary is None:
                            raise ContractError("native input requires a live authorization guard")
                        boundary()
                        approved_dispatch = True
                        self._connection.send(True)
                        continue
                    received_response = True
                    if kind == "result":
                        return payload
                    error, message, detail = payload
                    poisoned = bool(detail.get("poisoned"))
                    if error == "Pause":
                        raise Pause(detail["reason"], detail["detail"])
                    if error == "UncertainEffect":
                        raise UncertainEffect(message)
                    if error == "ContractError":
                        raise ContractError(message)
                    if error == "EmergencyStop":
                        raise EmergencyStop(message)
                    raise DriverError(message)
            except BaseException as exc:
                if not received_response or poisoned or self._pending_count.value:
                    self.abort()
                if approved_dispatch and not received_response and not isinstance(exc, UncertainEffect):
                    raise UncertainEffect(f"native action interrupted after dispatch authorization: {exc}") from exc
                raise
            finally:
                with self._lifecycle:
                    self._busy = False

    def execute(self, request, guard, snapshot=None):
        return self._call("execute", request, snapshot, guard=guard)

    def discover(self, app_ref: str | None = None):
        return self._call("discover", app_ref)

    def list_apps(self):
        return self._call("list_apps")

    def list_windows(self):
        return self._call("list_windows")

    def observe(self, scope, query=""):
        return self._call("observe", scope, query=query)

    def snapshot(self, snapshot_id, scope):
        return self._call("snapshot", snapshot_id, scope)

    def capture(self, **kwargs):
        return self._call("capture", **kwargs)

    def identity(self, app_ref, expected):
        return self._call("identity", app_ref, expected)

    def bind_process(self, pid, creation_time):
        return self._call("bind_process", pid, creation_time)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_exc):
        self.close()
