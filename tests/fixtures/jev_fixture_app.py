"""Controlled Windows fixture application.

Implements `tests/fixtures/FIXTURE_CONTRACT.md`: a stdlib-only Win32 program with
deliberately broken behaviours so the plugin's verification path can be proven to catch
failure-masking. It never touches any window other than its own and writes only inside
`--state-dir`.

The scenarios distinguish real mouse input from semantic invocation by checking whether the
physical button is down when the click notification arrives, so `dead-save` and
`semantic-only` behave exactly as the contract requires.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
comctl32 = ctypes.WinDLL("comctl32", use_last_error=True)

WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_CHILD, WS_VISIBLE, WS_TABSTOP = 0x40000000, 0x10000000, 0x00010000
WS_BORDER, WS_VSCROLL, WS_EX_TOPMOST = 0x00800000, 0x00200000, 0x00000008
ES_MULTILINE = 0x0004
CBS_DROPDOWNLIST = 0x0003
BS_AUTOCHECKBOX = 0x0003
WS_EX_CLIENTEDGE = 0x00000200
SW_SHOW = 5
IDC_ARROW = 32512

WM_COMMAND, WM_CLOSE, WM_DESTROY, WM_TIMER = 0x0111, 0x0010, 0x0002, 0x0113
WM_KEYDOWN, WM_GETTEXT, WM_GETTEXTLENGTH, WM_SETTEXT = 0x0100, 0x000D, 0x000E, 0x000C
WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK = 0x0201, 0x0202, 0x0203
VK_CONTROL, VK_S, VK_RETURN, VK_ESCAPE, VK_LBUTTON = 0x11, 0x53, 0x0D, 0x1B, 0x01
GWLP_WNDPROC = -4
BN_CLICKED = 0

ID_LABEL_NAME, ID_NAME_EDIT, ID_NOTES_EDIT, ID_MODE_COMBO = 100, 101, 102, 103
ID_ENABLE_CHECK, ID_SAVE_BUTTON, ID_DIALOG_BUTTON, ID_EXPORT_BUTTON = 104, 105, 106, 107
ID_ITEM_LIST, ID_STATUS_TEXT, ID_BUILD_TEXT = 108, 109, 110
ID_DIALOG_EDIT, ID_DIALOG_OK, ID_DIALOG_CANCEL = 201, 202, 203

SCENARIOS = {
    "basic",
    "dead-save",
    "semantic-only",
    "stale-artifact",
    "nonpersistent",
    "wrong-build",
    "false-done",
    "modal-block",
    "slow-transition",
    "crash-after-save",
}

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
user32.CreateWindowExW.restype = wintypes.HWND
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.SendMessageW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
user32.SetWindowLongPtrW.restype = ctypes.c_void_p
user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.CallWindowProcW.restype = ctypes.c_ssize_t
user32.SetTimer.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.UINT, ctypes.c_void_p]
user32.SetTimer.restype = wintypes.UINT
user32.KillTimer.argtypes = [wintypes.HWND, wintypes.UINT]
user32.KillTimer.restype = wintypes.BOOL
user32.EnableWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
user32.EnableWindow.restype = wintypes.BOOL
user32.GetKeyState.argtypes = [ctypes.c_int]
user32.GetKeyState.restype = ctypes.c_short
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT]
user32.SystemParametersInfoW.restype = wintypes.BOOL
SPI_GETWORKAREA = 0x0030


class Fixture:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.scenario = args.scenario
        self.state_dir = os.path.abspath(args.state_dir)
        self.build_id = args.build_id
        self.run_id = (
            args.run_id
            or os.environ.get("JEV_DESKTOP_RUN_ID")  # set by the runtime for LAUNCH_APP
            or os.environ.get("JEV_FIXTURE_RUN_ID")  # explicit override for hand runs
            or "run:0000000000000000"
        )
        self.status = "ready"
        self.saved_snapshot: dict = {}
        self.baseline_values: dict = {"name": "", "mode": "Draft", "enabled": False, "notes": ""}
        self.hwnd = 0
        self.dialog = 0
        self.controls: dict[int, int] = {}
        self.original_procs: dict[int, int] = {}
        self.procs: list = []
        self.mouse_pressed_on_save = False
        self.timer_id = 0
        os.makedirs(self.state_dir, exist_ok=True)

    # -- helpers -------------------------------------------------------------------

    def path(self, name: str) -> str:
        return os.path.join(self.state_dir, name)

    def write_json(self, name: str, payload: dict) -> None:
        temporary = self.path(name + ".tmp")
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(temporary, self.path(name))

    def read_json(self, name: str) -> dict | None:
        try:
            with open(self.path(name), encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return None

    def text_of(self, control_id: int) -> str:
        hwnd = self.controls.get(control_id, 0)
        if not hwnd:
            return ""
        length = user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(length + 2)
        user32.GetWindowTextW(hwnd, buffer, length + 2)
        return buffer.value

    def item_text(self, index: int) -> str:
        length = user32.SendMessageW(self.controls[ID_ITEM_LIST], 0x018A, index, 0)  # LB_GETTEXTLEN
        buffer = ctypes.create_unicode_buffer(int(length) + 2)
        user32.SendMessageW(self.controls[ID_ITEM_LIST], 0x0189, index, ctypes.cast(buffer, ctypes.c_void_p).value)
        return buffer.value

    @staticmethod
    def _send_text(hwnd: int, message: int, text: str, wparam: int = 0) -> int:
        """Send a message carrying a string pointer, keeping the buffer alive across the call."""
        buffer = ctypes.create_unicode_buffer(text)
        result = user32.SendMessageW(hwnd, message, wparam, ctypes.cast(buffer, ctypes.c_void_p).value)
        del buffer
        return int(result)

    def combo_text(self) -> str:
        return self.text_of(ID_MODE_COMBO)

    def set_status(self, value: str) -> None:
        self.status = value
        user32.SetWindowTextW(self.controls[ID_STATUS_TEXT], f"Status: {value}")
        self.event()

    def event(self) -> None:
        print(
            json.dumps(
                {
                    "event": "state",
                    "status": self.status,
                    "name": self.text_of(ID_NAME_EDIT),
                    "mode": self.combo_text(),
                    "enabled": bool(user32.SendMessageW(self.controls[ID_ENABLE_CHECK], 0x00F0, 0, 0)),  # BM_GETCHECK
                    "dialog": "open" if self.dialog else "none",
                    "saved_at": self.saved_snapshot.get("saved_at"),
                }
            ),
            flush=True,
        )

    def _save_button_proc(self):
        """Record real mouse presses on the Save button; the SWALLOW branch ignores them."""
        fixture = self

        def proc(hwnd: int, msg: int, wparam: int, lparam: int) -> int:
            if msg in (WM_LBUTTONDOWN, WM_LBUTTONDBLCLK):
                physical = bool(user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000)
                fixture.mouse_pressed_on_save = physical
                if fixture.scenario == "semantic-only" and physical:
                    return 0  # real mouse input is ignored entirely; synthetic BM_CLICK still works
            elif msg == WM_LBUTTONUP and fixture.scenario == "semantic-only" and fixture.mouse_pressed_on_save:
                return 0
            original = fixture.original_procs.get(hwnd)
            if original:
                return user32.CallWindowProcW(original, hwnd, msg, wparam, lparam)
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        return WNDPROC(proc)

    def subclass_save_button(self) -> None:
        hwnd = self.controls[ID_SAVE_BUTTON]
        proc = self._save_button_proc()
        self.procs.append(proc)
        self.original_procs[hwnd] = user32.SetWindowLongPtrW(
            hwnd, GWLP_WNDPROC, ctypes.cast(proc, ctypes.c_void_p).value
        )

    # -- domain actions ------------------------------------------------------------

    def current_values(self) -> dict:
        return {
            "name": self.text_of(ID_NAME_EDIT),
            "mode": self.combo_text(),
            "enabled": bool(user32.SendMessageW(self.controls[ID_ENABLE_CHECK], 0x00F0, 0, 0)),
            "notes": self.text_of(ID_NOTES_EDIT),
        }

    def do_save(self, *, via_mouse: bool) -> None:
        scenario = self.scenario
        if scenario == "slow-transition":
            self.set_status("saving")
            self.timer_id = user32.SetTimer(self.hwnd, 1, 3000, None)
            return
        if scenario == "false-done":
            self.set_status("saved")
            self.open_dialog(title="Saved", modal=True)
            return
        values = self.current_values()
        if scenario == "nonpersistent":
            # The setting does not persist: the file is written with the values the
            # application started from, never the edits the caller just made.
            values = dict(self.baseline_values)
        payload = {**values, "run_id": self.run_id, "saved_at": time.time()}
        self.write_json("state.json", payload)
        self.saved_snapshot = payload
        self.set_status("saved")

    def finish_slow_save(self) -> None:
        values = self.current_values()
        payload = {**values, "run_id": self.run_id, "saved_at": time.time()}
        self.write_json("state.json", payload)
        self.saved_snapshot = payload
        self.set_status("saved")

    def do_export(self) -> None:
        run_id = "run:000000000000deadbeef" if self.scenario == "stale-artifact" else self.run_id
        self.write_json(
            "export.json",
            {
                "run_id": run_id,
                "name": self.text_of(ID_NAME_EDIT),
                "exported_at": time.time(),
            },
        )
        self.set_status("exported")
        if self.scenario == "crash-after-save":
            os._exit(0)

    def open_dialog(self, *, title: str = "Fixture Dialog", modal: bool) -> None:
        if self.dialog:
            return
        instance = kernel32.GetModuleHandleW(None)
        self.dialog = user32.CreateWindowExW(
            0,
            "JevFixtureDialog",
            title,
            WS_OVERLAPPEDWINDOW | (0 if self.args.hidden else WS_VISIBLE),
            self.args.position[0] + 80,
            self.args.position[1] + 80,
            360,
            180,
            self.hwnd,
            0,
            instance,
            None,
        )
        self.controls[ID_DIALOG_EDIT] = user32.CreateWindowExW(
            0,
            "EDIT",
            "",
            WS_CHILD | WS_VISIBLE | WS_BORDER | WS_TABSTOP | WS_EX_CLIENTEDGE,
            20,
            20,
            300,
            26,
            self.dialog,
            ID_DIALOG_EDIT,
            instance,
            None,
        )
        self.controls[ID_DIALOG_OK] = user32.CreateWindowExW(
            0,
            "BUTTON",
            "OK",
            WS_CHILD | WS_VISIBLE | WS_TABSTOP,
            120,
            70,
            90,
            30,
            self.dialog,
            ID_DIALOG_OK,
            instance,
            None,
        )
        self.controls[ID_DIALOG_CANCEL] = user32.CreateWindowExW(
            0,
            "BUTTON",
            "Cancel",
            WS_CHILD | WS_VISIBLE | WS_TABSTOP,
            220,
            70,
            90,
            30,
            self.dialog,
            ID_DIALOG_CANCEL,
            instance,
            None,
        )
        if modal:
            user32.EnableWindow(self.hwnd, False)
        self.event()

    def close_dialog(self) -> None:
        if not self.dialog:
            return
        if user32.IsWindowEnabled(self.hwnd) == 0:
            user32.EnableWindow(self.hwnd, True)
        user32.DestroyWindow(self.dialog)
        self.dialog = 0
        for control in (ID_DIALOG_EDIT, ID_DIALOG_OK, ID_DIALOG_CANCEL):
            self.controls.pop(control, None)
        self.event()

    # -- message handling ----------------------------------------------------------

    def on_command(self, control_id: int, notification: int) -> bool:
        if control_id == ID_SAVE_BUTTON and notification == BN_CLICKED:
            via_mouse = self.mouse_pressed_on_save
            self.mouse_pressed_on_save = False
            if self.scenario == "dead-save" and via_mouse:
                self.set_status("save-ignored")
                return True
            self.do_save(via_mouse=via_mouse)
            return True
        if control_id == ID_DIALOG_BUTTON and notification == BN_CLICKED:
            self.open_dialog(modal=self.scenario != "basic")
            return True
        if control_id == ID_EXPORT_BUTTON and notification == BN_CLICKED:
            self.do_export()
            return True
        if control_id in {ID_DIALOG_OK, ID_DIALOG_CANCEL} and notification == BN_CLICKED:
            self.close_dialog()
            return True
        return False

    def on_key(self, vk: int) -> bool:
        if vk == VK_S and (user32.GetKeyState(VK_CONTROL) & 0x8000):
            self.do_save(via_mouse=False)  # the keyboard path always performs a real save
            return True
        if vk == VK_ESCAPE and self.dialog:
            self.close_dialog()
            return True
        return False

    def handle_accelerator(self, message) -> bool:
        """Ctrl+S is a window-level accelerator: it must work from any focused child control."""
        if message.message == WM_KEYDOWN and message.wParam == VK_S and (user32.GetKeyState(VK_CONTROL) & 0x8000):
            self.do_save(via_mouse=False)
            return True
        return False

    # -- window lifecycle ----------------------------------------------------------

    def create(self) -> int:
        instance = kernel32.GetModuleHandleW(None)
        cursor = user32.LoadCursorW(0, IDC_ARROW)
        main_proc = self._main_proc()
        dialog_proc = self._dialog_proc()
        self.procs.extend([main_proc, dialog_proc])

        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.style = 0x0002 | 0x0001
        wc.lpfnWndProc = main_proc
        wc.hInstance = instance
        wc.hCursor = cursor
        wc.hbrBackground = ctypes.cast(gdi32.CreateSolidBrush(0x00F0F0F0), wintypes.HBRUSH)
        wc.lpszClassName = "JevFixtureWindow"
        if not user32.RegisterClassExW(ctypes.byref(wc)):
            raise SystemExit(f"RegisterClassExW(main) failed: {ctypes.get_last_error()}")

        wc.lpfnWndProc = dialog_proc
        wc.lpszClassName = "JevFixtureDialog"
        wc.hbrBackground = ctypes.cast(gdi32.CreateSolidBrush(0x00FFFFFF), wintypes.HBRUSH)
        if not user32.RegisterClassExW(ctypes.byref(wc)):
            raise SystemExit(f"RegisterClassExW(dialog) failed: {ctypes.get_last_error()}")

        x, y = self.args.position
        width, height = self.args.size
        self.hwnd = user32.CreateWindowExW(
            WS_EX_TOPMOST if self.args.topmost else 0,
            "JevFixtureWindow",
            f"Jev Fixture - {self.scenario} - build {self.build_id}",
            WS_OVERLAPPEDWINDOW,
            x,
            y,
            width,
            height,
            0,
            0,
            instance,
            None,
        )
        if not self.hwnd:
            raise SystemExit(f"CreateWindowExW(main) failed: {ctypes.get_last_error()}")

        def child(class_name: str, text: str, style: int, cx: int, cy: int, cw: int, ch: int, control_id: int) -> int:
            handle = user32.CreateWindowExW(
                0,
                class_name,
                text,
                WS_CHILD | WS_VISIBLE | style,
                cx,
                cy,
                cw,
                ch,
                self.hwnd,
                control_id,
                instance,
                None,
            )
            self.controls[control_id] = handle
            return handle

        child("STATIC", "Name", 0, 16, 14, 60, 20, ID_LABEL_NAME)
        child("EDIT", "", WS_BORDER | WS_TABSTOP, 80, 12, 240, 24, ID_NAME_EDIT)
        child("EDIT", "", WS_BORDER | WS_TABSTOP | WS_VSCROLL | ES_MULTILINE, 16, 44, 380, 60, ID_NOTES_EDIT)
        combo = child("COMBOBOX", "", WS_BORDER | WS_TABSTOP | CBS_DROPDOWNLIST, 16, 112, 160, 200, ID_MODE_COMBO)
        for option in ("Draft", "Review", "Final"):
            self._send_text(combo, 0x0143, option)  # CB_ADDSTRING
        user32.SendMessageW(combo, 0x014E, 0, 0)  # CB_SETCURSEL -> Draft
        child("BUTTON", "Enable feature", WS_TABSTOP | BS_AUTOCHECKBOX, 190, 112, 200, 24, ID_ENABLE_CHECK)
        child("BUTTON", "Save", WS_TABSTOP, 16, 146, 100, 30, ID_SAVE_BUTTON)
        child("BUTTON", "Open dialog", WS_TABSTOP, 126, 146, 120, 30, ID_DIALOG_BUTTON)
        child("BUTTON", "Export", WS_TABSTOP, 256, 146, 100, 30, ID_EXPORT_BUTTON)
        child("LISTBOX", "", WS_BORDER | WS_TABSTOP | WS_VSCROLL, 16, 186, 380, 150, ID_ITEM_LIST)
        for index in range(1, 41):
            self._send_text(self.controls[ID_ITEM_LIST], 0x0180, f"Item {index:02d}")  # LB_ADDSTRING
        child("STATIC", "Status: ready", 0, 16, 344, 380, 20, ID_STATUS_TEXT)
        child("STATIC", f"Build: {self.build_id}", 0, 16, 364, 380, 20, ID_BUILD_TEXT)

        self.subclass_save_button()
        self.restore_state()
        if not self.args.hidden:
            user32.ShowWindow(self.hwnd, SW_SHOW)
            user32.UpdateWindow(self.hwnd)
        self.announce_ready()
        return self.hwnd

    def restore_state(self) -> None:
        payload = self.read_json("state.json")
        if not payload:
            self.saved_snapshot = {}
            return
        self.saved_snapshot = payload
        self.baseline_values = {
            "name": str(payload.get("name") or ""),
            "mode": str(payload.get("mode") or "Draft"),
            "enabled": bool(payload.get("enabled")),
            "notes": str(payload.get("notes") or ""),
        }
        user32.SetWindowTextW(self.controls[ID_NAME_EDIT], str(payload.get("name") or ""))
        user32.SetWindowTextW(self.controls[ID_NOTES_EDIT], str(payload.get("notes") or ""))
        mode = str(payload.get("mode") or "Draft")
        options = ["Draft", "Review", "Final"]
        index = options.index(mode) if mode in options else 0
        user32.SendMessageW(self.controls[ID_MODE_COMBO], 0x014E, index, 0)
        user32.SendMessageW(
            self.controls[ID_ENABLE_CHECK], 0x00F1, 1 if payload.get("enabled") else 0, 0
        )  # BM_SETCHECK

    def announce_ready(self) -> None:
        marker_build = f"{self.build_id}-stale" if self.scenario == "wrong-build" else self.build_id
        self.write_json(
            "build_marker.json",
            {
                "build_id": marker_build,
                "scenario": self.scenario,
                "started_at": time.time(),
                "pid": os.getpid(),
            },
        )
        self.write_json(
            "ready.json",
            {
                "ok": True,
                "pid": os.getpid(),
                "hwnd": hex(self.hwnd),
                "build_id": self.build_id,
                "scenario": self.scenario,
                "started_at": time.time(),
            },
        )
        print(
            json.dumps(
                {
                    "event": "ready",
                    "pid": os.getpid(),
                    "hwnd": hex(self.hwnd),
                    "scenario": self.scenario,
                    "build_id": self.build_id,
                }
            ),
            flush=True,
        )
        self.event()

    # -- window procedures ---------------------------------------------------------

    def _main_proc(self):
        fixture = self

        def proc(hwnd: int, msg: int, wparam: int, lparam: int) -> int:
            if msg == WM_COMMAND:
                if fixture.on_command(wparam & 0xFFFF, (wparam >> 16) & 0xFFFF):
                    return 0
            elif msg == WM_KEYDOWN:
                if fixture.on_key(int(wparam)):
                    return 0
            elif msg == WM_TIMER:
                if fixture.timer_id:
                    user32.KillTimer(hwnd, fixture.timer_id)
                    fixture.timer_id = 0
                fixture.finish_slow_save()
                return 0
            elif msg == WM_CLOSE:
                user32.DestroyWindow(hwnd)
                return 0
            elif msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        return WNDPROC(proc)

    def _dialog_proc(self):
        fixture = self

        def proc(hwnd: int, msg: int, wparam: int, lparam: int) -> int:
            if msg == WM_COMMAND:
                if fixture.on_command(wparam & 0xFFFF, (wparam >> 16) & 0xFFFF):
                    return 0
            elif msg == WM_CLOSE:
                fixture.close_dialog()
                return 0
            elif msg == WM_DESTROY:
                if fixture.dialog == hwnd:
                    fixture.dialog = 0
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        return WNDPROC(proc)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="jev_fixture_app", description="Controlled test application")
    parser.add_argument("--scenario", default="basic")
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--build-id", default="fixture-1")
    parser.add_argument("--run-id")
    parser.add_argument("--position", default=None, help="default: bottom-right of the work area")
    parser.add_argument("--topmost", action="store_true", help="keep the window above others (opt-in)")
    parser.add_argument(
        "--hidden", action="store_true", help="never show the main window (contract tests only; no desktop footprint)"
    )
    parser.add_argument(
        "--allow-desktop",
        action="store_true",
        help="required to create any window; without it the fixture refuses to start",
    )
    parser.add_argument("--size", default="720x520")
    parser.add_argument("--auto-close-after", type=float)
    args = parser.parse_args(argv)
    if args.scenario not in SCENARIOS:
        print(f"unknown scenario {args.scenario!r}; expected one of {sorted(SCENARIOS)}", file=sys.stderr)
        raise SystemExit(2)
    if not args.allow_desktop and os.environ.get("JEV_DESKTOP_LIVE") != "1":
        # This program creates real windows. Nothing may start it implicitly.
        print(
            "refused: this fixture creates windows; pass --allow-desktop or set JEV_DESKTOP_LIVE=1",
            file=sys.stderr,
        )
        raise SystemExit(3)
    width, height = (int(part) for part in args.size.lower().split("x"))
    args.size = (width, height)
    if args.position is None:
        # Stay inside the work area: a window overlapping the taskbar makes real clicks land on
        # whatever is on top of it and turns tests into flakes.
        work = wintypes.RECT()
        if user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(work), 0):
            right, bottom = work.right, work.bottom
        else:  # pragma: no cover - fall back to full screen metrics
            right, bottom = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        args.position = (max(0, right - width - 24), max(0, bottom - height - 24))
    else:
        args.position = tuple(int(part) for part in args.position.split(","))
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        user32.SetProcessDPIAware()
    fixture = Fixture(args)
    fixture.create()
    if args.auto_close_after:
        user32.SetTimer(fixture.hwnd, 99, int(args.auto_close_after * 1000), None)
    message = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(message), 0, 0, 0) > 0:
        if fixture.handle_accelerator(message):
            continue
        user32.TranslateMessage(ctypes.byref(message))
        user32.DispatchMessageW(ctypes.byref(message))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
