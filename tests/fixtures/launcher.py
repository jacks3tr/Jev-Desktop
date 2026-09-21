"""Launcher for the controlled fixture application (see FIXTURE_CONTRACT.md)."""

from __future__ import annotations

import atexit
import contextlib
import ctypes
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindow.restype = wintypes.BOOL
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.EnumChildWindows.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.LPARAM]
user32.EnumChildWindows.restype = wintypes.BOOL
user32.GetDlgCtrlID.argtypes = [wintypes.HWND]
user32.GetDlgCtrlID.restype = ctypes.c_int

WM_CLOSE = 0x0010
FIXTURE_SCRIPT = Path(__file__).with_name("jev_fixture_app.py")
_LIVE: set[FixtureProcess] = set()


@dataclass(eq=False)
class FixtureProcess:
    scenario: str
    build_id: str
    state_dir: Path
    process: subprocess.Popen
    pid: int
    run_id: str | None = None
    hwnd: int = 0
    ready: dict = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)
    stdout_lines: list[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # -- lifecycle -----------------------------------------------------------------

    def wait_ready(self, timeout: float = 20.0) -> dict:
        deadline = time.monotonic() + timeout
        marker = self.state_dir / "ready.json"
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(
                    f"fixture exited early (code {self.process.returncode}):\n" + "\n".join(self.stdout_lines[-10:])
                )
            if marker.is_file():
                try:
                    payload = json.loads(marker.read_text(encoding="utf-8"))
                except ValueError:
                    payload = None
                if payload and payload.get("ok"):
                    self.ready = payload
                    self.hwnd = (
                        int(str(payload.get("hwnd", "0")), 16)
                        if isinstance(payload.get("hwnd"), str)
                        else int(payload.get("hwnd") or 0)
                    )
                    if self.hwnd and user32.IsWindow(self.hwnd):
                        return payload
            time.sleep(0.05)
        raise TimeoutError(f"fixture {self.scenario} did not become ready:\n" + "\n".join(self.stdout_lines[-10:]))

    def stop(self, timeout: float = 5.0) -> None:
        if self.process.poll() is not None:
            _LIVE.discard(self)
            return
        if self.hwnd and user32.IsWindow(self.hwnd):
            user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:  # pragma: no cover - last resort
                self.process.kill()
        _LIVE.discard(self)

    def kill(self) -> None:  # pragma: no cover - used only for crash scenarios
        if self.process.poll() is None:
            self.process.kill()
        _LIVE.discard(self)

    # -- observations --------------------------------------------------------------

    def read_state(self) -> dict | None:
        return self._read("state.json")

    def read_export(self) -> dict | None:
        return self._read("export.json")

    def read_marker(self) -> dict | None:
        return self._read("build_marker.json")

    def _read(self, name: str) -> dict | None:
        path = self.state_dir / name
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def status(self, timeout: float = 5.0) -> str | None:
        """Wait briefly for the most recent state event and return its status."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                states = [event for event in self.events if event.get("event") == "state"]
            if states:
                return str(states[-1].get("status")) if states[-1].get("status") else None
            if self.process.poll() is not None:
                break
            time.sleep(0.05)
        return None

    # -- driving without real input (contract self-test only) ----------------------

    def _child(self, control_id: int) -> int:
        found = {"hwnd": 0}

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def callback(hwnd, _param):
            if user32.GetDlgCtrlID(hwnd) == control_id:
                found["hwnd"] = int(hwnd)
                return False
            return True

        user32.EnumChildWindows(self.hwnd, ctypes.cast(callback, ctypes.c_void_p), 0)
        return found["hwnd"]

    def send_mouse_click(self, control_id: int = 105) -> None:
        """Real mouse messages, without injecting input: WM_LBUTTONDOWN/UP to the control."""
        hwnd = self._child(control_id)
        lparam = (1 << 16) | 1
        user32.PostMessageW(hwnd, 0x0201, 0x0001, lparam)  # WM_LBUTTONDOWN, MK_LBUTTON
        time.sleep(0.05)
        user32.PostMessageW(hwnd, 0x0202, 0, lparam)  # WM_LBUTTONUP
        time.sleep(0.2)

    def type_text(self, control_id: int, text: str) -> None:
        """Synthetic typing: WM_CHAR per character (no real input, cross-process safe).

        Cross-process `SetWindowTextW` points the target at a buffer in *our* address space,
        which is why contract tests type character by character instead.
        """
        hwnd = self._child(control_id)
        for char in text:
            user32.SendMessageW(hwnd, 0x0102, ord(char), 0)  # WM_CHAR
        time.sleep(0.1)

    def send_ctrl_s(self) -> None:
        """Accelerator through the message loop (the fixture handles Ctrl+S pre-translate)."""
        user32.PostMessageW(self.hwnd, 0x0100, 0x53, 1)  # WM_KEYDOWN VK_S; Ctrl state is read live

    def last_state_event(self) -> dict:
        """The fixture's own view of its state (stdout), not a cross-process window read."""
        with self._lock:
            states = [event for event in self.events if event.get("event") == "state"]
        return dict(states[-1]) if states else {}

    def status_now(self) -> str | None:
        with self._lock:
            states = [event for event in self.events if event.get("event") == "state"]
        return str(states[-1].get("status")) if states and states[-1].get("status") else None

    def status_text(self) -> str:
        """Window text of the status control, read directly through Win32 (not UIA)."""
        if not self.hwnd or not user32.IsWindow(self.hwnd):
            return ""
        results: list[str] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def callback(hwnd: int, _param: int) -> bool:
            length = user32.GetWindowTextLengthW(hwnd)
            if length:
                text = ctypes.create_unicode_buffer(length + 2)
                user32.GetWindowTextW(hwnd, text, length + 2)
                if text.value.startswith("Status:"):
                    results.append(text.value)
            return True

        user32.EnumChildWindows(self.hwnd, ctypes.cast(callback, ctypes.c_void_p), 0)
        return results[0] if results else ""

    # -- stdout pump ---------------------------------------------------------------

    def _pump(self) -> None:
        assert self.process.stdout is not None
        for raw in self.process.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            with self._lock:
                self.stdout_lines.append(line)
            try:
                event = json.loads(line)
            except ValueError:
                continue
            with self._lock:
                self.events.append(event)


def start_fixture(
    scenario: str,
    *,
    build_id: str = "fixture-1",
    run_id: str | None = None,
    state_dir: str | Path | None = None,
    position: str | None = None,
    size: str = "720x520",
    auto_close_after: float | None = 120.0,
    topmost: bool = False,
    hidden: bool = False,
    allow_desktop: bool | None = None,
    wait_ready_timeout: float = 20.0,
) -> FixtureProcess:
    """Start the fixture. It creates windows, so desktop access must be granted explicitly."""
    live = os.environ.get("JEV_DESKTOP_LIVE") == "1" if allow_desktop is None else allow_desktop
    if not live:
        raise RuntimeError(
            "start_fixture refused: this starts a windowed application; "
            "set JEV_DESKTOP_LIVE=1 or pass allow_desktop=True"
        )
    directory = Path(state_dir) if state_dir else Path(tempfile.mkdtemp(prefix=f"jev-fixture-{scenario}-"))
    directory.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        str(FIXTURE_SCRIPT),
        "--scenario",
        scenario,
        "--state-dir",
        str(directory),
        "--build-id",
        build_id,
        "--size",
        size,
    ]
    if position:
        command += ["--position", position]
    if topmost:
        command += ["--topmost"]
    if hidden:
        command += ["--hidden"]
    if live:
        command += ["--allow-desktop"]
    if run_id:
        command += ["--run-id", run_id]
    if auto_close_after:
        command += ["--auto-close-after", str(auto_close_after)]
    environment = dict(os.environ)
    environment.setdefault("PYTHONIOENCODING", "utf-8")
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=str(FIXTURE_SCRIPT.parent),
        env=environment,
    )
    fixture = FixtureProcess(
        scenario=scenario,
        build_id=build_id,
        state_dir=directory,
        process=process,
        pid=process.pid,
        run_id=run_id,
    )
    thread = threading.Thread(target=fixture._pump, name=f"fixture-{scenario}", daemon=True)
    thread.start()
    _LIVE.add(fixture)
    fixture.wait_ready(timeout=wait_ready_timeout)
    return fixture


@atexit.register
def _reap() -> None:
    for fixture in list(_LIVE):
        with contextlib.suppress(Exception):
            fixture.stop(timeout=2.0)
        with contextlib.suppress(Exception):
            fixture.kill()
