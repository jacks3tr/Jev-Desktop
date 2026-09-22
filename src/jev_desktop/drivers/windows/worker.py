"""Private parent/child protocol; no native references cross the process boundary."""

from __future__ import annotations

import multiprocessing
import os
import threading
from typing import Any

from ...contracts import Pause

METHODS = {
    "discover",
    "start",
    "list_apps",
    "list_windows",
    "observe",
    "snapshot",
    "capture",
    "identity",
    "bind_process",
}


def serve(connection, evidence_dir: str, pending_inputs, pending_count) -> None:
    from . import win32
    from .native import NativeWindowsDriver

    driver = NativeWindowsDriver(evidence_dir=evidence_dir)
    win32._pending_inputs = pending_inputs
    win32._pending_count = pending_count

    def watch_parent() -> None:
        parent = multiprocessing.parent_process()
        if parent is not None:
            parent.join()
            try:
                win32.stop_input_and_release(pending_inputs, pending_count)
            finally:
                os._exit(1)

    threading.Thread(target=watch_parent, name="jev-parent-watch", daemon=True).start()

    def guard() -> None:
        connection.send(("guard", None))
        if connection.recv() is not True:
            raise RuntimeError("parent refused dispatch")

    try:
        while True:
            method, args, kwargs = connection.recv()
            try:
                if method == "execute":
                    request, snapshot = args
                    win32._dispatch_guard = guard
                    try:
                        result = driver.execute(request, guard, snapshot)
                    finally:
                        win32._dispatch_guard = None
                elif method in METHODS:
                    result = getattr(driver, method)(*args, **kwargs)
                else:
                    raise ValueError("unknown native-worker method")
                connection.send(("result", result))
            except Exception as exc:
                detail: dict[str, Any] = (
                    {"reason": exc.reason_value, "detail": exc.detail} if isinstance(exc, Pause) else {}
                )
                detail["poisoned"] = driver.worker._poisoned
                connection.send(("error", (type(exc).__name__, str(exc), detail)))
    except (EOFError, BrokenPipeError, OSError):
        pass
    finally:
        connection.close()
        driver.close()
