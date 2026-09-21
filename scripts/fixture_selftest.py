"""Fixture contract self-test: no real input, no visible windows.

Starts every scenario with a hidden window and drives the application with explicit window
messages (`WM_COMMAND`, accelerator keys through the message loop), asserting the contract
in `tests/fixtures/FIXTURE_CONTRACT.md` for each one.

Usage: python scripts/fixture_selftest.py
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

from launcher import FixtureProcess, start_fixture

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.SendMessageW.restype = ctypes.c_ssize_t
user32.FindWindowExW.restype = wintypes.HWND

WM_COMMAND = 0x0111
WM_CLOSE = 0x0010
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
BM_CLICK = 0x00F5
BN_CLICKED = 0

ID_NAME_EDIT, ID_ENABLE_CHECK, ID_SAVE_BUTTON = 101, 104, 105
ID_DIALOG_BUTTON, ID_EXPORT_BUTTON, ID_STATUS_TEXT = 106, 107, 109
ID_DIALOG_OK = 202


def _by_id(parent: int, control_id: int) -> int:
    result = {"hwnd": 0}

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _param):
        if user32.GetDlgCtrlID(hwnd) == control_id:
            result["hwnd"] = int(hwnd)
            return False
        return True

    user32.EnumChildWindows(parent, ctypes.cast(callback, ctypes.c_void_p), 0)
    return result["hwnd"]


def set_text(fixture: FixtureProcess, control_id: int, text: str) -> None:
    fixture.type_text(control_id, text)


def click(fixture: FixtureProcess, control_id: int) -> None:
    user32.PostMessageW(fixture.hwnd, WM_COMMAND, control_id | (BN_CLICKED << 16), _by_id(fixture.hwnd, control_id))
    time.sleep(0.15)


def press_save_semantically(fixture: FixtureProcess) -> None:
    """BM_CLICK is what a UI Automation Invoke on a Win32 button performs."""
    user32.SendMessageW(_by_id(fixture.hwnd, ID_SAVE_BUTTON), BM_CLICK, 0, 0)
    time.sleep(0.15)


def basic(fixture: FixtureProcess) -> dict:
    set_text(fixture, ID_NAME_EDIT, "Ada")
    press_save_semantically(fixture)
    state = fixture.read_state()
    click(fixture, ID_EXPORT_BUTTON)
    export = fixture.read_export()
    click(fixture, ID_DIALOG_BUTTON)
    time.sleep(0.2)
    dialog = user32.FindWindowExW(0, 0, "JevFixtureDialog", None)
    if dialog:
        user32.PostMessageW(dialog, WM_CLOSE, 0, 0)
    return {
        "name_persisted": bool(state and state["name"] == "Ada"),
        "mode_persisted": bool(state and state["mode"] == "Draft"),
        "export_written": bool(export and export["run_id"] == "run:0000000000000000"),
        "dialog_opened": bool(dialog),
        "marker_matches": (fixture.read_marker() or {}).get("build_id") == "selftest-1",
    }


def restart_persistence(directory) -> dict:
    """Save, stop, restart against the same state directory, and read the restored value."""
    first = start_fixture("basic", build_id="selftest-1", state_dir=directory, hidden=True)
    try:
        first.type_text(ID_NAME_EDIT, "Ada")
        press_save_semantically(first)  # Ctrl+S needs physical modifier state; the live suite covers it
        saved = first.read_state()
    finally:
        first.stop()
    second = start_fixture("basic", build_id="selftest-1", state_dir=directory, hidden=True)
    try:
        time.sleep(0.3)
        # The fixture reports what it actually restored; a cross-process window read cannot show this.
        restored_name = second.last_state_event().get("name")
        return {
            "saved_value": (saved or {}).get("name") == "Ada",
            "restored_after_restart": restored_name == "Ada",
            "restored_name": restored_name,
        }
    finally:
        second.stop()


def dead_save(fixture: FixtureProcess) -> dict:
    """Synthetic input cannot prove the real-mouse distinction, so only this is asserted.

    Whether a *physical* click is ignored (and Ctrl+S still saves) needs genuine input and is
    asserted by tests/windows/test_fixture_integration.py under the live gate.
    """
    press_save_semantically(fixture)
    return {"semantic_save_works": fixture.read_state() is not None}


def semantic_only(fixture: FixtureProcess) -> dict:
    press_save_semantically(fixture)
    return {"semantic_invoke_saves": fixture.read_state() is not None}


def stale_artifact(fixture: FixtureProcess) -> dict:
    click(fixture, ID_EXPORT_BUTTON)
    export = fixture.read_export() or {}
    return {"export_exists": bool(export), "run_id_is_stale": export.get("run_id") == "run:000000000000deadbeef"}


def nonpersistent(fixture: FixtureProcess) -> dict:
    set_text(fixture, ID_NAME_EDIT, "Ada")
    press_save_semantically(fixture)
    state = fixture.read_state() or {}
    return {"file_written": bool(state), "value_not_persisted": state.get("name") != "Ada"}


def wrong_build(fixture: FixtureProcess) -> dict:
    marker = fixture.read_marker() or {}
    return {"marker_mismatched": marker.get("build_id") == "selftest-1-stale"}


def false_done(fixture: FixtureProcess) -> dict:
    press_save_semantically(fixture)
    dialog = user32.FindWindowExW(0, 0, "JevFixtureDialog", None)
    if dialog:
        user32.PostMessageW(dialog, WM_CLOSE, 0, 0)
    return {"status_claims_saved": fixture.status_now() == "saved", "nothing_persisted": fixture.read_state() is None}


def modal_block(fixture: FixtureProcess) -> dict:
    click(fixture, ID_DIALOG_BUTTON)
    time.sleep(0.2)
    dialog = user32.FindWindowExW(0, 0, "JevFixtureDialog", None)
    owner_disabled = not user32.IsWindowEnabled(fixture.hwnd)
    if dialog:
        user32.PostMessageW(dialog, WM_CLOSE, 0, 0)
    return {"dialog_open": bool(dialog), "owner_disabled": owner_disabled}


def slow_transition(fixture: FixtureProcess) -> dict:
    press_save_semantically(fixture)
    immediate = fixture.status_now()
    deadline = time.monotonic() + 6.0
    while time.monotonic() < deadline and fixture.read_state() is None:
        time.sleep(0.2)
    return {
        "saving_first": immediate == "saving",
        "saved_later": fixture.read_state() is not None,
        "final_status": fixture.status_now(),
    }


def crash_after_save(fixture: FixtureProcess) -> dict:
    click(fixture, ID_EXPORT_BUTTON)
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and fixture.process.poll() is None:
        time.sleep(0.05)
    return {"export_written": bool(fixture.read_export()), "process_exited": fixture.process.poll() is not None}


# name -> (check, required keys, scenario to launch). The scenario is named explicitly so
# composed checks (for example save/restart/save) do not depend on the check's name.
CHECKS = {
    "basic": (
        basic,
        ("name_persisted", "mode_persisted", "export_written", "dialog_opened", "marker_matches"),
        "basic",
    ),
    "restart-persistence": (
        lambda fixture: restart_persistence(fixture.state_dir),
        ("saved_value", "restored_after_restart"),
        "basic",
    ),
    "dead-save": (dead_save, ("semantic_save_works",), "dead-save"),
    "semantic-only": (semantic_only, ("semantic_invoke_saves",), "semantic-only"),
    "stale-artifact": (stale_artifact, ("export_exists", "run_id_is_stale"), "stale-artifact"),
    "nonpersistent": (nonpersistent, ("file_written", "value_not_persisted"), "nonpersistent"),
    "wrong-build": (wrong_build, ("marker_mismatched",), "wrong-build"),
    "false-done": (false_done, ("status_claims_saved", "nothing_persisted"), "false-done"),
    "modal-block": (modal_block, ("dialog_open", "owner_disabled"), "modal-block"),
    "slow-transition": (slow_transition, ("saving_first", "saved_later"), "slow-transition"),
    "crash-after-save": (crash_after_save, ("export_written", "process_exited"), "crash-after-save"),
}


def main() -> int:
    if os.environ.get("JEV_DESKTOP_LIVE") != "1":
        print(
            json.dumps(
                {
                    "ok": False,
                    "refused": "this contract test starts windowed fixture processes; run it with "
                    "JEV_DESKTOP_LIVE=1 when the desktop is free",
                }
            )
        )
        return 3
    results: dict[str, dict] = {}
    ok = True
    for name, (check, required, scenario) in CHECKS.items():
        try:
            fixture = start_fixture(scenario, build_id="selftest-1", hidden=True)
            try:
                outcome = check(fixture)
            finally:
                fixture.stop()
        except Exception as exc:
            results[name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            ok = False
            continue
        failed = [key for key in required if not outcome.get(key)]
        results[name] = {"ok": not failed, "checks": outcome, "failed": failed}
        ok = ok and not failed
    print(json.dumps({"ok": ok, "scenarios": results}, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
