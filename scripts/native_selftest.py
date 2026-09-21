"""Native driver self-test: real UIA observation, real input, real capture, one refusal.

Creates its own throwaway Win32 window (never touches any other application), drives it
through the plugin's Windows driver, and prints a single JSON result object.

Usage: python scripts/native_selftest.py [--json]

Safety: only the window created here is ever clicked or typed into; the foreground window
is verified before every dispatch, and the cursor is restored after mouse input.
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jev_desktop.contracts import (
    ActionRequest,
    DriverError,
    ExpectedIdentity,
    InputMode,
    Operation,
    Pause,
    ScopeSpec,
    new_id,
    now,
)
from jev_desktop.drivers.windows import WindowsDriver, win32

WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_CHILD, WS_VISIBLE = 0x40000000, 0x10000000
WS_BORDER, WS_TABSTOP = 0x00800000, 0x00010000

ID_BUTTON, ID_EDIT, ID_CHECK, ID_STATUS = 1, 2, 3, 4

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON),
    ]


user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HWND,
    wintypes.HMENU,
    wintypes.HINSTANCE,
    ctypes.c_void_p,
]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.RegisterClassExW.restype = wintypes.ATOM
user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
user32.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]

STATE = {"status": "idle", "clicks": 0, "toggle": "off"}
HANDLES: dict[str, int] = {}


def _wndproc(hwnd: int, msg: int, wparam: int, lparam: int) -> int:
    if msg == 0x0111:  # WM_COMMAND
        control = wparam & 0xFFFF
        if control == ID_BUTTON:
            STATE["clicks"] += 1
            STATE["status"] = f"clicked:{STATE['clicks']}"
            user32.SetWindowTextW(HANDLES["status"], f"status: {STATE['status']}")
        elif control == ID_CHECK:
            STATE["toggle"] = "on" if STATE["toggle"] == "off" else "off"
            user32.SetWindowTextW(HANDLES["status"], f"status: toggle-{STATE['toggle']}")
        return 0
    if msg == 0x0010:  # WM_CLOSE
        user32.DestroyWindow(hwnd)
        return 0
    if msg == 0x0002:  # WM_DESTROY
        user32.PostQuitMessage(0)
        return 0
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


PROC = WNDPROC(_wndproc)


def create_window() -> tuple[int, int]:
    instance = kernel32.GetModuleHandleW(None)
    cursor = user32.LoadCursorW(0, 32512)  # IDC_ARROW
    class_name = "JevSelftestWindow"
    wc = WNDCLASSEXW()
    wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
    wc.style = 0x0002 | 0x0001
    wc.lpfnWndProc = PROC
    wc.hInstance = instance
    wc.hCursor = cursor
    wc.hbrBackground = gdi32.GetStockObject(4)  # COLOR_WINDOW + ... (-16+... ) -> use stock brush 4
    wc.lpszClassName = class_name
    atom = user32.RegisterClassExW(ctypes.byref(wc))
    if not atom:
        raise SystemExit(f"RegisterClassExW failed: {ctypes.get_last_error()}")
    # A background brush helps UIA consider the window a real control host.
    wc.hbrBackground = ctypes.cast(gdi32.CreateSolidBrush(0x00F0F0F0), wintypes.HBRUSH)
    hwnd = user32.CreateWindowExW(
        0x00000008,  # WS_EX_TOPMOST: the self-test owns the desktop point it clicks
        class_name,
        "Jev Selftest Window",
        WS_OVERLAPPEDWINDOW,
        200,
        200,
        460,
        260,
        0,
        0,
        instance,
        None,
    )
    if not hwnd:
        raise SystemExit(f"CreateWindowExW failed: {ctypes.get_last_error()}")
    HANDLES["main"] = hwnd
    HANDLES["button"] = user32.CreateWindowExW(
        0, "BUTTON", "Click me", WS_CHILD | WS_VISIBLE | WS_TABSTOP, 20, 20, 140, 34, hwnd, ID_BUTTON, instance, None
    )
    HANDLES["edit"] = user32.CreateWindowExW(
        0,
        "EDIT",
        "",
        WS_CHILD | WS_VISIBLE | WS_BORDER | WS_TABSTOP | 0x0080,
        20,
        70,
        240,
        28,
        hwnd,
        ID_EDIT,
        instance,
        None,
    )
    HANDLES["check"] = user32.CreateWindowExW(
        0,
        "BUTTON",
        "Flag",
        WS_CHILD | WS_VISIBLE | WS_TABSTOP | 0x0003,
        20,
        110,
        200,
        26,
        hwnd,
        ID_CHECK,
        instance,
        None,
    )
    HANDLES["status"] = user32.CreateWindowExW(
        0, "STATIC", "status: idle", WS_CHILD | WS_VISIBLE, 20, 150, 320, 24, hwnd, ID_STATUS, instance, None
    )
    user32.ShowWindow(hwnd, 5)
    user32.UpdateWindow(hwnd)
    return hwnd, instance


def pump() -> None:
    message = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(message), 0, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(message))
        user32.DispatchMessageW(ctypes.byref(message))


def main() -> int:
    if os.environ.get("JEV_DESKTOP_LIVE") != "1":
        print(
            json.dumps(
                {
                    "ok": False,
                    "refused": "this self-test creates a window and moves the mouse; run it with "
                    "JEV_DESKTOP_LIVE=1 when the desktop is free",
                }
            )
        )
        return 3
    steps: list[dict] = []
    ok = True
    driver = WindowsDriver(evidence_dir=Path(os.environ.get("TEMP", ".")) / "jev-selftest-evidence")
    driver.start()
    ready = threading.Event()

    def _window_thread() -> None:
        create_window()
        ready.set()
        pump()

    thread = threading.Thread(target=_window_thread, name="selftest-window", daemon=True)
    thread.start()
    if not ready.wait(10):
        print(json.dumps({"ok": False, "error": "window did not start"}))
        return 1
    hwnd = HANDLES["main"]
    win32.activate_window(hwnd)
    time.sleep(0.4)

    try:
        apps = driver.list_apps()
        mine = next((app for app in apps if app.process_id == os.getpid()), None)
        if mine is None:
            raise SystemExit("selftest window not discovered as an application")
        steps.append({"step": "discover", "ok": True, "app_ref": mine.app_ref, "windows": len(mine.window_refs)})
        scope = ScopeSpec(app_ref=mine.app_ref, max_elements=120)
        snapshot = driver.observe(scope)
        names = {element.role: element.name for element in snapshot.elements}
        found = {
            "button": next((e for e in snapshot.elements if e.name == "Click me"), None),
            "edit": next((e for e in snapshot.elements if e.role == "edit"), None),
            "check": next((e for e in snapshot.elements if e.name == "Flag"), None),
        }
        steps.append(
            {
                "step": "observe",
                "ok": all(found.values()),
                "coverage": snapshot.coverage.value,
                "elements": len(snapshot.elements),
                "roles": sorted({e.role for e in snapshot.elements}),
                "missing": [k for k, v in found.items() if v is None],
                "names": names,
            }
        )
        if not all(found.values()):
            raise SystemExit("required controls were not observed")

        capture = driver.capture(
            scope=scope,
            snapshot_id=snapshot.snapshot_id,
            run_id=new_id("run"),
            checkpoint="selftest",
            description="self-test window",
        )
        with open(capture.evidence.path, "rb") as handle:
            png = handle.read()
        steps.append(
            {
                "step": "capture",
                "ok": png[:8] == b"\x89PNG\r\n\x1a\n" and capture.evidence.size_bytes == len(png),
                "path": capture.evidence.path,
                "bytes": len(png),
                "source_rect": capture.source_rect.to_json(),
                "scale": capture.scale,
                "geometry_epoch": capture.geometry.epoch,
            }
        )

        guard_calls = {"n": 0}

        def guard() -> None:
            guard_calls["n"] += 1

        focus = ActionRequest(
            action_id=new_id("act"),
            run_id=new_id("run"),
            operation=Operation.FOCUS_WINDOW,
            mode=InputMode.USER_PATH,
            element_id=None,
            snapshot_id=snapshot.snapshot_id,
            window_ref=found["button"].window_ref,
            lease_generation=1,
            step_id="focus-window",
        )
        focus_receipt = driver.execute(focus, guard, snapshot)
        steps.append(
            {
                "step": "focus_window",
                "ok": win32.foreground_window() != 0
                and win32.root_window(win32.foreground_window()) == win32.root_window(hwnd),
                "note": focus_receipt.notes,
            }
        )

        click = ActionRequest(
            action_id=new_id("act"),
            run_id=new_id("run"),
            operation=Operation.CLICK,
            mode=InputMode.USER_PATH,
            element_id=found["button"].element_id,
            snapshot_id=snapshot.snapshot_id,
            window_ref=found["button"].window_ref,
            lease_generation=1,
            step_id="click-button",
        )
        receipt = driver.execute(click, guard, snapshot)
        time.sleep(0.4)
        after = driver.observe(scope)
        status = next((e.text for e in after.elements if e.role == "text" and (e.text or "").startswith("status:")), "")
        steps.append(
            {
                "step": "user_path_click",
                "ok": receipt.dispatch_state.value == "dispatched" and STATE["clicks"] >= 1 and "clicked" in status,
                "mechanism": receipt.mechanism.value,
                "inserted_events": receipt.inserted_events,
                "guard_calls": guard_calls["n"],
                "window_status": status,
                "clicks_seen_by_window": STATE["clicks"],
            }
        )

        edit_element = next((e for e in after.elements if e.role == "edit"), None)
        typed = ActionRequest(
            action_id=new_id("act"),
            run_id=new_id("run"),
            operation=Operation.TYPE_TEXT,
            mode=InputMode.USER_PATH,
            element_id=edit_element.element_id,
            snapshot_id=after.snapshot_id,
            window_ref=edit_element.window_ref,
            lease_generation=1,
            step_id="type-name",
            text="hello-jev",
        )
        driver.execute(typed, guard, after)
        time.sleep(0.4)
        after_type = driver.observe(scope)
        read_back = next((e.value for e in after_type.elements if e.role == "edit"), None)
        steps.append({"step": "user_path_type_text", "ok": read_back == "hello-jev", "observed": read_back})

        check_element = next((e for e in after_type.elements if e.name == "Flag"), None)
        toggle = ActionRequest(
            action_id=new_id("act"),
            run_id=new_id("run"),
            operation=Operation.TOGGLE,
            mode=InputMode.SEMANTIC,
            element_id=check_element.element_id,
            snapshot_id=after_type.snapshot_id,
            window_ref=check_element.window_ref,
            lease_generation=1,
            step_id="toggle-flag",
        )
        receipt = driver.execute(toggle, guard, after_type)
        time.sleep(0.3)
        after_toggle = driver.observe(scope)
        checked = next((e.state.get("checked") for e in after_toggle.elements if e.name == "Flag"), None)
        steps.append(
            {
                "step": "semantic_toggle",
                "ok": receipt.mechanism.value == "uia_pattern" and checked == "on",
                "mechanism": receipt.mechanism.value,
                "checked": checked,
            }
        )

        refused = None
        try:
            stale = ActionRequest(
                action_id=new_id("act"),
                run_id=new_id("run"),
                operation=Operation.CLICK,
                mode=InputMode.USER_PATH,
                element_id=found["button"].element_id,
                snapshot_id=snapshot.snapshot_id,
                window_ref=found["button"].window_ref,
                lease_generation=1,
                step_id="stale-click",
            )
            driver.execute(stale, guard, after_toggle)
        except (DriverError, Pause) as exc:
            refused = f"{type(exc).__name__}: {exc}"
        steps.append({"step": "refused_stale_action", "ok": refused is not None, "error": refused})

        identity = driver.identity(mine.app_ref, ExpectedIdentity(mode="fresh_launch", launched_after=now() - 600))
        steps.append(
            {
                "step": "identity",
                "ok": identity.status.value in {"verified", "mismatch", "unverifiable"},
                "status": identity.status.value,
                "pid": identity.observed.get("process_id"),
            }
        )
    except BaseException as exc:
        ok = False
        steps.append({"step": "failure", "ok": False, "error": f"{type(exc).__name__}: {exc}"})
    finally:
        driver.close()
        user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
        thread.join(timeout=3)

    ok = ok and all(step.get("ok") for step in steps)
    print(json.dumps({"ok": ok, "steps": steps, "health": driver.health()}, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
