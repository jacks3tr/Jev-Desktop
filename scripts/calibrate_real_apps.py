"""Calibrate against real desktop applications, not just the test fixture.

The fixture in `tests/fixtures/` exists to make failure modes deterministic. It has seven
controls, which makes it useless for calibration: real applications have hundreds of elements,
deep trees, virtualized lists, accessibility providers written by someone else, and slow
transitions. This script measures the plugin against the applications already installed on the
machine, and it runs complete, verifiable user paths through the real runtime.

    set JEV_DESKTOP_LIVE=1
    set TYPESAFE_API_KEY=...
    python scripts/calibrate_real_apps.py --cases notepad,calculator,chromium --json out.json

Cases:

    notepad      type into a document, open the File menu, Save As into a temp folder, verify
                 the file on disk. The canonical desktop test: menus, a modal dialog, typing
                 into a real field, and an artifact assertion.
    calculator   seven plus five equals, then read the result. A UWP application with a grid
                 of similarly named buttons and a computed value to verify.
    chromium     observe a Chromium window and report tree size, time, state bytes, and token
                 estimate. The large-tree case that decides element caps and cost per decision.

Only the applications this script starts are touched, and only to write into a temporary
folder it created.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jev_desktop.contracts import (
    InputMode,
    Limits,
    Operation,
    Purpose,
    RunSpec,
    ScopeSpec,
    new_id,
)
from jev_desktop.drivers.windows import WindowsDriver, win32
from jev_desktop.evidence import EvidenceStore
from jev_desktop.journal import DispatchJournal
from jev_desktop.ownership import Ownership
from jev_desktop.policy import HttpTransport, JevPolicy, PolicyConfig, summarize_state_for_policy
from jev_desktop.runtime import Runtime, RuntimeConfig

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
WM_CLOSE = 0x0010

APPLICATIONS = {
    "notepad": [r"C:\Windows\System32\notepad.exe", r"C:\Windows\notepad.exe"],
    "calculator": [r"C:\Windows\System32\calc.exe"],
    "wordpad": [r"C:\Program Files\Windows NT\Accessories\wordpad.exe"],
    "chrome": [r"C:\Program Files\Google\Chrome\Application\chrome.exe"],
    "edge": [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"],
    "explorer": [r"C:\Windows\explorer.exe"],
}


def resolve(name: str) -> str:
    for path in APPLICATIONS.get(name, []):
        if Path(path).exists():
            return path
    raise SystemExit(f"{name} is not installed on this machine")


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(fraction * (len(ordered) - 1)))]


def describe(values: list[float], unit: str = "") -> dict[str, Any]:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "min": round(min(values), 3),
        "p50": round(percentile(values, 0.5) or 0, 3),
        "p95": round(percentile(values, 0.95) or 0, 3),
        "max": round(max(values), 3),
        "mean": round(sum(values) / len(values), 3),
        "unit": unit,
    }


class DumpingTransport:
    """Wrap a transport and write every request, plus any refusal, to a directory."""

    def __init__(self, inner, directory: Path) -> None:
        self.inner = inner
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.count = 0

    def post_json(self, url, *, headers, payload, timeout_s):
        self.count += 1
        index = f"{self.count:03d}"
        (self.directory / f"{index}-request.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        try:
            status, body = self.inner.post_json(url, headers=headers, payload=payload, timeout_s=timeout_s)
        except Exception:
            raise
        if status >= 400:
            (self.directory / f"{index}-response-{status}.json").write_text(
                json.dumps(body, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        return status, body


class RealApp:
    """Launch an application, bind to the process, and shut it down afterwards."""

    def __init__(
        self,
        driver: WindowsDriver,
        application: str,
        *,
        args: list[str] | None = None,
        title_hint: str | None = None,
        command_line_hint: str | None = None,
    ) -> None:
        self.driver = driver
        self.executable = resolve(application)
        self.launched_at = time.time()
        self._apps_before = {app.app_ref for app in driver.list_apps()}
        self._windows_before = {ref for app in driver.list_apps() for ref in app.window_refs}
        self.process = subprocess.Popen([self.executable, *(args or [])], close_fds=True)
        self.app_ref: str | None = None
        self.window_ref: str | None = None
        self.bound_by = "pid"
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            for app in driver.list_apps():
                if app.process_id == self.process.pid and app.window_refs:
                    self.app_ref = app.app_ref
                    self.window_ref = app.window_refs[0]
                    break
            if self.app_ref is not None:
                break
            # Applications that hand off (UWP, and Chromium when it reuses or spawns a process)
            # need a different binding rule than the launched pid.
            for app in driver.list_apps():
                if app.app_ref in self._apps_before:
                    continue
                fresh_refs = [ref for ref in app.window_refs if ref not in self._windows_before]
                if not fresh_refs:
                    continue
                if command_line_hint:
                    try:
                        command_line = win32.process_command_line(app.process_id)
                    except Exception:
                        command_line = ""
                    if command_line_hint.lower() not in command_line.lower():
                        continue
                    self.app_ref, self.window_ref = app.app_ref, fresh_refs[0]
                    self.bound_by = "new_window_command_line"
                    break
            if self.app_ref is not None:
                break
            if title_hint:
                # A window that did not exist before the launch and matches the hint. This is
                # how UWP apps are found: the launcher exits and another process owns the
                # window, so the owning process's start time says nothing.
                for app in driver.list_apps():
                    for ref in app.window_refs:
                        if ref in self._windows_before and app.app_ref in self._apps_before:
                            continue
                        if title_hint.lower() in win32.window_title(driver.window_handle(ref)).lower():
                            self.app_ref, self.window_ref = app.app_ref, ref
                            self.bound_by = "new_window_by_title"
                            break
                    if self.app_ref is not None:
                        break
            if self.app_ref is not None:
                break
            time.sleep(0.25)
        if self.app_ref is None:
            self.process.terminate()
            raise SystemExit(f"{application} did not expose an observable window")
        self.scope = ScopeSpec(app_ref=self.app_ref, max_elements=240)

    def wait_for_foreground(self, *, timeout: float = 10.0) -> bool:
        """An application behind other windows reports its controls offscreen, and the driver
        refuses to send input to a window that is not on top. Wait for it like a user would."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                handle = self.driver.window_handle(self.window_ref)
                if win32.root_window(win32.foreground_window()) == win32.root_window(handle):
                    return True
            except Exception:
                return False
            time.sleep(0.25)
        return False

    def close(self) -> None:
        try:
            handle = self.driver.window_handle(self.window_ref) if self.window_ref else 0
            if handle and win32.user32.IsWindow(handle):
                user32.PostMessageW(handle, WM_CLOSE, 0, 0)
        except Exception:
            pass
        time.sleep(0.5)
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()


def candidate_breakdown(snapshot) -> dict[str, dict[str, int]]:
    """Candidates per operation by role, which is how noisy lists get caught."""
    breakdown: dict[str, dict[str, int]] = {}
    for operation in (Operation.CLICK, Operation.TYPE_TEXT, Operation.SELECT, Operation.TOGGLE, Operation.SCROLL):
        roles: dict[str, int] = {}
        for element in snapshot.elements:
            if operation.value in element.operations:
                roles[element.role] = roles.get(element.role, 0) + 1
        breakdown[operation.value] = dict(sorted(roles.items(), key=lambda item: -item[1]))
    return breakdown


def measure_observation(driver: WindowsDriver, scope: ScopeSpec) -> dict[str, Any]:
    started = time.perf_counter()
    snapshot = driver.observe(scope)
    elapsed = (time.perf_counter() - started) * 1000
    state = summarize_state_for_policy(
        goal="g",
        current_step=None,
        snapshot_elements=[
            {
                "index": element.index,
                "element_id": element.element_id,
                "role": element.role,
                "name": element.name,
                "value": element.value,
                "enabled": element.enabled,
                "visible": element.visible,
                "editable": element.editable,
                "focused": element.focused,
                "operations": list(element.operations),
                "path": list(element.path),
                "state": dict(element.state),
                "truncation": element.truncation,
            }
            for element in snapshot.elements
        ],
        context={
            "window_titles": [window.title for window in snapshot.windows],
            "modal_windows": list(snapshot.context.get("modal_windows") or []),
            "texts": list(snapshot.context.get("texts") or []),
            "coverage": snapshot.coverage.value,
            "truncation": list(snapshot.truncation),
            "focused_element_id": snapshot.context.get("focused_element_id"),
        },
        recent_actions=[],
        mode="user_path",
    )
    encoded = json.dumps(state, ensure_ascii=False)
    candidates = {
        operation.value: sum(1 for element in snapshot.elements if operation.value in element.operations)
        for operation in (Operation.CLICK, Operation.TYPE_TEXT, Operation.SELECT, Operation.TOGGLE, Operation.SCROLL)
    }
    return {
        "observation_ms": round(elapsed, 1),
        "elements": len(snapshot.elements),
        "coverage": snapshot.coverage.value,
        "truncation": list(snapshot.truncation),
        "state_bytes": len(encoded.encode("utf-8")),
        "state_tokens_estimate": round(len(encoded) / 4),
        "candidates": candidates,
        "windows": [
            {"title": window.title, "modal": window.modal, "scope": window.scope} for window in snapshot.windows
        ],
        "candidate_roles": candidate_breakdown(snapshot),
    }


def run_spec(
    driver: WindowsDriver,
    policy: JevPolicy,
    spec: RunSpec,
    workdir: Path,
    *,
    slice_seconds: float = 90.0,
    approved_roots: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    journal = DispatchJournal(str(workdir / "journal.sqlite"))
    ownership = Ownership()
    roots = approved_roots or (str(workdir),)
    evidence = EvidenceStore(root=workdir / "evidence", approved_roots=list(roots))
    runtime = Runtime(
        driver=driver,
        journal=journal,
        ownership=ownership,
        evidence=evidence,
        config=RuntimeConfig(evidence_dir=workdir / "evidence", approved_roots=roots),
        policy=policy,
    )
    try:
        session = ownership.create_session("real-app-calibration")
        created = runtime.create_run(spec, session_id=session.session_id)
        ownership.acquire(session.session_id, created["run_id"])
        started = time.perf_counter()
        result = runtime.slice(
            run_id=created["run_id"],
            session_id=session.session_id,
            resume_token=created["resume_token"],
            slice_seconds=slice_seconds,
        )
        wall = time.perf_counter() - started
        return {
            "run_id": created["run_id"],
            "execution": result.execution.value,
            "verdict": result.verdict.value,
            "reason": result.reason,
            "wall_seconds": round(wall, 2),
            "actions": result.budgets["actions"],
            "model_decisions": result.budgets["decisions"],
            "steps": [
                {
                    "step_id": step.step_id,
                    "operation": step.operation.value,
                    "dispatch_state": step.dispatch_state.value,
                    "target": step.target_description,
                    "changed": step.observation_changed,
                    "error": step.error,
                }
                for step in result.steps
            ],
            "assertions": [
                {
                    "id": assertion.assertion_id,
                    "status": assertion.status.value,
                    "observed": dict(assertion.observed),
                    "notes": list(assertion.notes),
                }
                for assertion in result.assertions
            ],
            "evidence_bytes": sum(reference.size_bytes for reference in result.evidence),
            "detail": dict(result.detail),
        }
    finally:
        journal.close()


def case_notepad(driver: WindowsDriver, policy: JevPolicy, workdir: Path) -> dict[str, Any]:
    """Type a document, save it through the File menu into a folder we own, verify the file."""
    output_dir = Path(tempfile.mkdtemp(prefix="jev-real-notepad-"))
    target_file = output_dir / "jev-calibration-note.txt"
    run_tag = new_id("run")
    app = RealApp(driver, "notepad")
    try:
        observations = {"initial": measure_observation(driver, app.scope)}
        app.scope = ScopeSpec(app_ref=app.app_ref, max_elements=int(os.environ.get("JEV_MAX_ELEMENTS", "120")))
        spec = RunSpec.from_json(
            {
                "goal": (
                    "regression: type the caller's fixture text into the document, then save the file as "
                    f"{target_file.name} in {output_dir} using the File menu"
                ),
                "purpose": Purpose.REGRESSION.value,
                "interaction_mode": InputMode.USER_PATH.value,
                "app_ref": app.app_ref,
                "expected_identity": {"mode": "fresh_launch", "launched_after": app.launched_at - 1.0},
                "launch_config_id": None,
                "steps": [
                    {
                        "step_id": "type",
                        "operation": "TYPE_TEXT",
                        "target_description": "the document text area",
                        "fixture_reference": "document_text",
                    },
                    {"step_id": "open-file-menu", "operation": "CLICK", "target_description": "the File menu"},
                    {
                        "step_id": "choose-save-as",
                        "operation": "CLICK",
                        "target_description": "Save As in the File menu",
                    },
                    {
                        "step_id": "name-file",
                        "operation": "TYPE_TEXT",
                        "target_description": "the File name field in the Save As dialog",
                        "fixture_reference": "file_path",
                    },
                    {
                        "step_id": "confirm-save",
                        "operation": "CLICK",
                        "target_description": "the Save button",
                        "checkpoint": True,
                    },
                ],
                "assertions": [
                    {
                        "assertion_id": "file-written",
                        "evaluator": "artifact",
                        "target": {"path": str(target_file)},
                        "expected": {"is_true": True},
                        "property": "exists",
                        "checkpoint": "confirm-save",
                    },
                    {
                        "assertion_id": "file-binds-to-run",
                        "evaluator": "artifact",
                        "target": {"path": str(target_file), "content_regex": run_tag},
                        "expected": {},
                        "property": "content_regex",
                        "checkpoint": "confirm-save",
                    },
                ],
                "fixtures": {"document_text": f"calibration {run_tag}\n", "file_path": str(target_file)},
                "secret_refs": {},
                "limits": {**Limits.defaults().to_json(), "max_model_decisions": 12, "slice_seconds": 90.0},
                "scope": {"app_ref": app.app_ref, "max_elements": 240},
                "allow_restart": False,
            }
        )
        spec = RunSpec.from_json(
            {**json.loads(json.dumps(spec.to_json())), "scope": {"app_ref": app.app_ref, "max_elements": 240}}
        )
        runtime_config_roots = (str(workdir), str(output_dir))
        outcome = run_spec(driver, policy, spec, workdir, approved_roots=runtime_config_roots)
        outcome["observations"] = observations
        outcome["expected_file"] = str(target_file)
        outcome["file_exists"] = target_file.exists()
        outcome["file_bytes"] = target_file.stat().st_size if target_file.exists() else 0
        outcome["file_contains_run_tag"] = (
            run_tag in target_file.read_text(encoding="utf-8", errors="replace") if target_file.exists() else False
        )
        return outcome
    finally:
        app.close()


def case_calculator(driver: WindowsDriver, policy: JevPolicy, workdir: Path) -> dict[str, Any]:
    """Seven plus five equals, then verify the displayed result."""
    app = RealApp(driver, "calculator", title_hint="Calculator")
    try:
        foreground = app.wait_for_foreground()
        observations = {"initial": measure_observation(driver, app.scope), "foreground": foreground}
        spec = RunSpec.from_json(
            {
                "goal": "regression: compute 7 + 5 using the on-screen buttons and verify the result reads 12",
                "purpose": Purpose.REGRESSION.value,
                "interaction_mode": InputMode.USER_PATH.value,
                "app_ref": app.app_ref,
                # A packaged application is identified by package family, not by the process
                # that owns its window: that process is the frame host, which started earlier.
                # No launch timestamp, because UWP apps stay resident and a launch may simply
                # activate the existing instance.
                "expected_identity": {
                    "mode": "package_family",
                    "expect_package": "Microsoft.WindowsCalculator_8wekyb3d8bbwe",
                },
                "launch_config_id": None,
                "steps": [
                    {"step_id": "seven", "operation": "CLICK", "target_description": "the Seven button"},
                    {"step_id": "plus", "operation": "CLICK", "target_description": "the Plus button"},
                    {"step_id": "five", "operation": "CLICK", "target_description": "the Five button"},
                    {
                        "step_id": "equals",
                        "operation": "CLICK",
                        "target_description": "the Equals button",
                        "checkpoint": True,
                    },
                ],
                "assertions": [
                    {
                        "assertion_id": "result-is-12",
                        "evaluator": "uia_property",
                        "target": {"role": "text", "name_regex": "^(Display is|display is)"},
                        "property": "name",
                        "expected": {"contains": "12"},
                        "checkpoint": "equals",
                    },
                ],
                "fixtures": {},
                "secret_refs": {},
                "limits": {**Limits.defaults().to_json(), "max_model_decisions": 12, "slice_seconds": 90.0},
                "scope": {"app_ref": app.app_ref, "max_elements": 240},
                "allow_restart": False,
            }
        )
        outcome = run_spec(driver, policy, spec, workdir)
        outcome["observations"] = observations
        outcome["bound_by"] = app.bound_by
        return outcome
    finally:
        app.close()


def content_page() -> Path:
    """A local page with plenty of interactive elements, plus a link that navigates away."""
    directory = Path(tempfile.mkdtemp(prefix="jev-real-page-"))
    page = directory / "content.html"
    second = directory / "second.html"
    rows = "".join(
        f'<li><a href="#{index}" id="link{index}">Article {index}</a>'
        f'<button id="action{index}">Action {index}</button></li>'
        for index in range(1, 151)
    )
    second.write_text(
        "<html><head><title>Jev second page</title></head><body><h1>Second page</h1>"
        "<p>Navigation completed.</p></body></html>",
        encoding="utf-8",
    )
    page.write_text(
        "<html><head><title>Jev calibration page</title></head><body>"
        "<h1>Calibration content</h1>"
        f"<p><a href='{second.as_uri()}' id='next'>Go to the second page</a></p>"
        "<input id='search' type='text' aria-label='Search'>"
        "<select id='filter' aria-label='Filter'><option>All</option><option>New</option></select>"
        f"<ul>{rows}</ul></body></html>",
        encoding="utf-8",
    )
    return page


def case_chromium(
    driver: WindowsDriver, policy: JevPolicy, workdir: Path, application: str = "chrome"
) -> dict[str, Any]:
    """Observe a Chromium window: the large-tree case that sets element caps and cost."""
    profile = Path(tempfile.mkdtemp(prefix="jev-real-chromium-profile-"))
    app = RealApp(
        driver,
        application,
        args=[
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            # Web content only enters the accessibility tree when the renderer exposes it.
            "--force-renderer-accessibility",
            content_page().as_uri(),
        ],
        command_line_hint=str(profile),
    )
    try:
        time.sleep(2.0)
        measurements: dict[str, Any] = {}
        for cap in (60, 120, 240, 600):
            scope = ScopeSpec(app_ref=app.app_ref, max_elements=cap)
            measurements[f"max_elements_{cap}"] = measure_observation(driver, scope)

        foreground = app.wait_for_foreground(timeout=15.0)
        outcome: dict[str, Any] = {"measurements": measurements, "foreground": foreground}
        if not foreground:
            outcome["skipped"] = (
                "the browser window never reached the foreground; a background "
                "window reports its controls offscreen and the driver refuses to "
                "click a window that is not on top"
            )
            return outcome

        # A real navigation: click a link in the page, then verify the window title changed.
        spec = RunSpec.from_json(
            {
                "goal": "regression: follow the link on the page and land on the second page",
                "purpose": Purpose.REGRESSION.value,
                "interaction_mode": InputMode.USER_PATH.value,
                "app_ref": app.app_ref,
                "expected_identity": {"mode": "fresh_launch", "launched_after": app.launched_at - 2.0},
                "launch_config_id": None,
                "steps": [
                    {"step_id": "focus", "operation": "FOCUS_WINDOW", "target_description": "the browser window"},
                    {
                        "step_id": "follow-link",
                        "operation": "CLICK",
                        "target_description": "the link titled Go to the second page",
                        "checkpoint": True,
                    },
                ],
                "assertions": [
                    {
                        "assertion_id": "navigated",
                        "evaluator": "window_state",
                        "target": {"title_regex": "Jev second page"},
                        "property": "exists",
                        "expected": {"is_true": True},
                        "checkpoint": "follow-link",
                    },
                ],
                "fixtures": {},
                "secret_refs": {},
                "limits": {**Limits.defaults().to_json(), "max_model_decisions": 8, "slice_seconds": 120.0},
                "scope": {"app_ref": app.app_ref, "max_elements": 120},
                "allow_restart": False,
            }
        )
        outcome["navigation"] = run_spec(driver, policy, spec, workdir)
        return outcome
    finally:
        app.close()


CASES = {"notepad": case_notepad, "calculator": case_calculator, "chromium": case_chromium}


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="calibrate_real_apps", description="Calibrate against real desktop applications"
    )
    parser.add_argument("--cases", default="notepad,calculator,chromium")
    parser.add_argument("--json", type=Path)
    parser.add_argument("--dump", type=Path, help="write every policy request, and any refusal, to this directory")
    args = parser.parse_args()

    if os.environ.get("JEV_DESKTOP_LIVE") != "1":
        print("this drives real applications; set JEV_DESKTOP_LIVE=1", file=sys.stderr)
        return 2
    key = (os.environ.get("TYPESAFE_API_KEY") or "").strip()
    if not key:
        print("set TYPESAFE_API_KEY", file=sys.stderr)
        return 2

    workdir = Path(tempfile.mkdtemp(prefix="jev-real-calibration-"))
    driver = WindowsDriver(evidence_dir=workdir / "evidence")
    driver.start()
    transport = DumpingTransport(HttpTransport(), args.dump) if args.dump else HttpTransport()
    policy = JevPolicy(transport=transport, config=PolicyConfig(), api_key=key)
    report: dict[str, Any] = {"workdir": str(workdir)}
    try:
        for name in [item.strip() for item in args.cases.split(",") if item.strip()]:
            handler = CASES.get(name)
            if handler is None:
                print(f"unknown case {name}", file=sys.stderr)
                continue
            print(f"\n=== {name} ===", flush=True)
            try:
                payload = handler(driver, policy, workdir)
                report[name] = payload
            except BaseException as exc:
                report[name] = {"error": f"{type(exc).__name__}: {exc}"}
            print(json.dumps(report[name], indent=2)[:4000], flush=True)
    finally:
        driver.close()
    report["resolved_models"] = sorted(set(policy.resolved_models))
    if args.json:
        args.json.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
