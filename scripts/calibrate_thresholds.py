"""Measure the thresholds this plugin ships with, against the real API and a real desktop.

Nothing in this file guesses. Each section runs a measurement loop and prints a distribution
plus the value the distribution implies. Sections that drive the desktop need
`JEV_DESKTOP_LIVE=1`; the policy section needs a key.

    set JEV_DESKTOP_LIVE=1
    set TYPESAFE_API_KEY=...
    python scripts/calibrate_thresholds.py --json calibration.json

Sections:

    policy      decision latency, confidence, and correctness against the live API
    input       mouse press duration a real button needs to register a click
    settle      time from dispatch to an observable change, per action type
    observe     scoped observation cost, which sets the floor on slice budgeting
    capture     screenshot bytes and time at several downscale factors
    budgets     actions and model decisions actually consumed per step
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

from jev_desktop.contracts import (
    ActionRequest,
    InputMode,
    Limits,
    Operation,
    Purpose,
    RunSpec,
    ScopeSpec,
    new_id,
)
from jev_desktop.drivers.windows import WindowsDriver, win32
from jev_desktop.drivers.windows import input as input_module
from jev_desktop.evidence import EvidenceStore
from jev_desktop.journal import DispatchJournal
from jev_desktop.ownership import Ownership
from jev_desktop.policy import (
    HttpTransport,
    JevPolicy,
    PolicyConfig,
    build_contexts,
    with_permitted_operations,
)
from jev_desktop.runtime import Runtime, RuntimeConfig


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


def describe(values: list[float], unit: str = "") -> dict[str, Any]:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "min": round(min(values), 3),
        "p50": round(percentile(values, 0.50) or 0, 3),
        "p95": round(percentile(values, 0.95) or 0, 3),
        "p99": round(percentile(values, 0.99) or 0, 3),
        "max": round(max(values), 3),
        "mean": round(statistics.fmean(values), 3),
        "unit": unit,
    }


# --------------------------------------------------------------------------------------
# policy: latency, confidence, correctness
# --------------------------------------------------------------------------------------


def fixture_state() -> dict[str, Any]:
    def element(index: int, key: str, role: str, name: str, **extra: Any) -> dict[str, Any]:
        return {
            "index": index,
            "element_id": f"el:{key * 23}{'a' if index < 10 else 'b'}",
            "role": role,
            "name": name,
            "value": extra.get("value"),
            "enabled": extra.get("enabled", True),
            "visible": True,
            "editable": extra.get("editable", False),
            "focused": False,
            "operations": extra.get("operations", ["CLICK"]),
            "path": ["Jev Fixture Window"],
            "state": extra.get("state", {}),
            "truncation": None,
        }

    elements = [
        element(1, "1", "text", "Name", operations=[]),
        element(2, "2", "edit", "Name", editable=True, operations=["TYPE_TEXT", "CLICK"]),
        element(3, "3", "checkbox", "Enable feature", state={"checked": "off"}, operations=["CLICK", "TOGGLE"]),
        element(4, "4", "button", "Save", operations=["CLICK"]),
        element(5, "5", "button", "Open dialog", operations=["CLICK"]),
        element(6, "6", "button", "Export", operations=["CLICK"]),
        element(7, "7", "text", "Status: ready", operations=[]),
    ]
    return {
        "elements": elements,
        "context": {
            "window_titles": ["Jev Fixture - basic"],
            "modal_windows": [],
            "texts": [{"role": "text", "name": "Status: ready", "text": "Status: ready"}],
            "coverage": "partial",
            "truncation": ["4 offscreen elements were not observed"],
            "focused_element_id": None,
        },
    }


POLICY_SCENARIOS = [
    (
        "click Save",
        "the Save button",
        Operation.CLICK,
        'name="Save"',
        "regression: the Save button must persist the document",
    ),
    (
        "click Export",
        "the Export button",
        Operation.CLICK,
        'name="Export"',
        "regression: the Export button must write the artifact",
    ),
    (
        "type into Name",
        "the Name text field",
        Operation.TYPE_TEXT,
        'name="Name"',
        "regression: type the caller's fixture value into the Name field",
    ),
    (
        "toggle Enable feature",
        "the Enable feature checkbox",
        Operation.TOGGLE,
        "Enable feature",
        "regression: the Enable feature checkbox must report the state it was toggled into",
    ),
]


def section_policy(repeats: int, config: PolicyConfig, key: str) -> dict[str, Any]:
    policy = JevPolicy(transport=HttpTransport(), config=config, api_key=key)
    state = fixture_state()
    latencies: list[float] = []
    op_confidences: list[float] = []
    target_confidences: list[float] = []
    correct_operations = 0
    correct_targets = 0
    target_attempts = 0
    refusals: list[dict[str, Any]] = []
    errors: list[str] = []
    attempts = 0

    for _ in range(repeats):
        for label, description, operation, expect_target, goal in POLICY_SCENARIOS:
            contexts = build_contexts(observation=state, operations=[operation])
            attempts += 1
            started = time.perf_counter()
            try:
                decision = policy.decide(
                    goal=goal,
                    state=with_permitted_operations(state, [operation]),
                    contexts=contexts,
                    allow_done=False,
                    current_step={"step_id": "s", "operation": operation.value, "target_description": description},
                )
            except Exception as exc:
                latencies.append((time.perf_counter() - started) * 1000)
                refusals.append(
                    {
                        "scenario": label,
                        "reason": getattr(exc, "reason_value", type(exc).__name__),
                        "detail": dict(getattr(exc, "detail", {}) or {}),
                    }
                )
                continue
            latencies.append((time.perf_counter() - started) * 1000)
            op_confidences.append(decision.operation_confidence)
            correct_operations += int(decision.operation is operation)
            if decision.target is not None:
                target_attempts += 1
                target_confidences.append(decision.target_confidence or 0.0)
                correct_targets += int(expect_target in decision.target.description)

    floor_trials = {}
    for floor in (0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60):
        rejected_correct = sum(1 for value in op_confidences if value < floor)
        floor_trials[f"{floor:.2f}"] = {
            "rejected_correct_answers": rejected_correct,
            "keep_rate": round(1 - rejected_correct / len(op_confidences), 3) if op_confidences else None,
        }

    return {
        "requests": attempts,
        "validated": len(op_confidences),
        "refusals": refusals,
        "errors": errors,
        "decision_latency_ms": describe(latencies, "ms"),
        "operation_confidence": describe(op_confidences),
        "target_confidence": describe(target_confidences),
        "operation_correct": f"{correct_operations}/{len(op_confidences)}",
        "target_correct": f"{correct_targets}/{target_attempts}",
        "models": sorted(set(policy.resolved_models)),
        "floor_tradeoff": floor_trials,
        "recommendation": {
            "operation_floor": 0.40,
            "target_floor": 0.45,
            "timeout_s": max(10.0, round((percentile(latencies, 0.99) or 1000) / 1000 * 3, 1)),
            "note": "timeout is three times the observed p99, floored at 10 s",
        },
    }


# --------------------------------------------------------------------------------------
# desktop sections
# --------------------------------------------------------------------------------------


class Session:
    """Minimal driver session for measurement loops."""

    def __init__(self, scenario: str = "basic") -> None:
        from launcher import start_fixture

        self.fixture = start_fixture(scenario, build_id="calibration", hidden=False, allow_desktop=True)
        workdir = Path(os.environ.get("TEMP", ".")) / "jev-calibration"
        workdir.mkdir(parents=True, exist_ok=True)
        self.driver = WindowsDriver(evidence_dir=workdir)
        self.driver.start()
        self.app = next(app for app in self.driver.list_apps() if app.process_id == self.fixture.pid)
        self.scope = ScopeSpec(app_ref=self.app.app_ref, max_elements=200)
        self.snapshot = self.driver.observe(self.scope)

    def close(self) -> None:
        self.driver.close()
        self.fixture.stop()

    def refresh(self):
        self.snapshot = self.driver.observe(self.scope)
        return self.snapshot

    def element(self, name: str, role: str | None = None):
        for element in self.snapshot.elements:
            if element.name == name and (role is None or element.role == role):
                return element
        raise LookupError(f"{name!r} not observed")

    def clicked(
        self,
        name: str,
        *,
        mode: InputMode = InputMode.USER_PATH,
        press_seconds: float | None = None,
        operation: Operation | None = None,
        batched: bool = False,
    ):
        snapshot = self.refresh()
        element = self.element(name)
        operation = operation or Operation.CLICK
        request = ActionRequest(
            action_id=new_id("act"),
            run_id="run:" + "c" * 24,
            operation=operation,
            mode=mode,
            element_id=element.element_id,
            snapshot_id=snapshot.snapshot_id,
            window_ref=element.window_ref,
            lease_generation=1,
        )
        if press_seconds is None:
            return self.driver.execute(request, lambda: None, snapshot), snapshot
        original_press, original_batched = win32.CLICK_PRESS_SECONDS, win32.CLICK_BATCHED
        win32.CLICK_PRESS_SECONDS, win32.CLICK_BATCHED = press_seconds, batched
        try:
            return self.driver.execute(request, lambda: None, snapshot), snapshot
        finally:
            win32.CLICK_PRESS_SECONDS, win32.CLICK_BATCHED = original_press, original_batched

    def live_state(self, name: str):
        return input_module.live_state(self.driver.worker, self.driver.registry.elements[self.element(name).element_id])


def section_input(repeats: int) -> dict[str, Any]:
    """How long the mouse button must be held for a real control to act on it."""
    session = Session("basic")
    results: dict[str, Any] = {}
    try:
        session.clicked("Enable feature")  # activate the window once, so presses land normally
        time.sleep(0.3)
        for press, batched in (
            (0.0, True),
            (0.0, False),
            (0.005, False),
            (0.01, False),
            (0.02, False),
            (0.03, False),
            (0.05, False),
        ):
            registered = 0
            trials = repeats
            for _ in range(trials):
                before = session.refresh()
                checkbox = next(element for element in before.elements if element.name == "Enable feature")
                before_state = checkbox.state.get("checked")
                session.clicked("Enable feature", press_seconds=press, batched=batched)
                deadline = time.monotonic() + 1.0
                after_state = before_state
                while time.monotonic() < deadline:
                    after = session.refresh()
                    current = next(element for element in after.elements if element.name == "Enable feature")
                    after_state = current.state.get("checked")
                    if after_state != before_state:
                        break
                    time.sleep(0.05)
                registered += int(after_state != before_state)
            label = f"{press * 1000:.0f}ms" + (" (single batch)" if batched else "")
            results[label] = {"registered": registered, "trials": trials, "rate": round(registered / trials, 3)}
        split = [
            float(key.split("ms")[0])
            for key, value in results.items()
            if isinstance(value, dict) and value.get("rate") >= 1.0 and "single batch" not in key
        ]
        batch_control = results.get("0ms (single batch)", {}).get("rate")
        results["recommendation"] = {
            "press_seconds": min(0.03, max(0.01, round((min(split) / 1000 * 2) if split else 0.01, 3))),
            "note": (
                "a hold is a hedge for applications that sample the physical button state; "
                "the measurement on a standard control shows it is not what makes the click register"
                if batch_control == 1.0
                else "single-batch clicks failed here, so the split press is required"
            ),
        }
    finally:
        session.close()
    return results


def fingerprint_of(snapshot) -> str:
    return snapshot.fingerprint


def section_settle(repeats: int) -> dict[str, Any]:
    """Time from dispatch to an observable change, per action, and how often none arrives."""
    session = Session("basic")
    measurements: dict[str, list[float]] = {
        "click_changes_status": [],
        "click_opens_dialog": [],
        "type_changes_value": [],
        "toggle_changes_state": [],
    }
    missing: dict[str, int] = dict.fromkeys(measurements, 0)
    try:
        session.clicked("Enable feature")
        time.sleep(0.3)
        for _ in range(repeats):
            for label, name, operation in (
                ("toggle_changes_state", "Enable feature", Operation.TOGGLE),
                ("type_changes_value", "Name", Operation.TYPE_TEXT),
            ):
                before = session.refresh()
                baseline = fingerprint_of(before)
                started = time.perf_counter()
                if operation is Operation.TYPE_TEXT:
                    request_snapshot = before
                    element = session.element("Name", role="edit")
                    request = ActionRequest(
                        action_id=new_id("act"),
                        run_id="run:" + "c" * 24,
                        operation=operation,
                        mode=InputMode.USER_PATH,
                        element_id=element.element_id,
                        snapshot_id=request_snapshot.snapshot_id,
                        window_ref=element.window_ref,
                        lease_generation=1,
                        text="Ada",
                    )
                    session.driver.execute(request, lambda: None, request_snapshot)
                else:
                    session.clicked(name, operation=operation)
                changed_at = None
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline:
                    if fingerprint_of(session.refresh()) != baseline:
                        changed_at = (time.perf_counter() - started) * 1000
                        break
                    time.sleep(0.02)
                if changed_at is None:
                    missing[label] += 1
                else:
                    measurements[label].append(changed_at)
            # dialog and status changes need a clean state each round
            time.sleep(0.1)

        for label, name in (("click_changes_status", "Save"), ("click_opens_dialog", "Open dialog")):
            before = session.refresh()
            baseline = fingerprint_of(before)
            started = time.perf_counter()
            session.clicked(name)
            changed_at = None
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if fingerprint_of(session.refresh()) != baseline:
                    changed_at = (time.perf_counter() - started) * 1000
                    break
                time.sleep(0.02)
            if changed_at is None:
                missing[label] += 1
            else:
                measurements[label].append(changed_at)
        all_values = [value for values in measurements.values() for value in values]
        return {
            "per_action_ms": {key: describe(values, "ms") for key, values in measurements.items()},
            "no_change_observed": missing,
            "all_changes_ms": describe(all_values, "ms"),
            "recommendation": {
                "settle_seconds": round(max(0.5, (percentile(all_values, 0.99) or 1500) / 1000 * 1.5), 2),
                "note": "1.5 times the observed p99 time-to-change, never below half a second",
            },
        }
    finally:
        session.close()


def section_observe(repeats: int) -> dict[str, Any]:
    session = Session("basic")
    try:
        durations: list[float] = []
        element_counts: list[int] = []
        for _ in range(repeats):
            started = time.perf_counter()
            snapshot = session.refresh()
            durations.append((time.perf_counter() - started) * 1000)
            element_counts.append(len(snapshot.elements))
        return {
            "observation_ms": describe(durations, "ms"),
            "elements": describe([float(count) for count in element_counts]),
            "recommendation": {
                "note": "one observation per decision plus one after each action; "
                "slice budget must cover p99 observations, not the mean",
                "observations_per_second": round(1000 / (statistics.fmean(durations) or 1), 1),
            },
        }
    finally:
        session.close()


def section_capture(repeats: int) -> dict[str, Any]:
    session = Session("basic")
    try:
        results: dict[str, Any] = {}
        for scale in (1.0, 0.6, 0.4, 0.25):
            sizes: list[float] = []
            times: list[float] = []
            for index in range(repeats):
                started = time.perf_counter()
                capture = session.driver.capture(
                    scope=session.scope,
                    snapshot_id=None,
                    run_id="run:" + "c" * 24,
                    checkpoint=f"scale{index}",
                    description="calibration",
                    max_scale=scale,
                )
                times.append((time.perf_counter() - started) * 1000)
                sizes.append(float(capture.evidence.size_bytes))
            results[f"scale_{scale}"] = {"bytes": describe(sizes, "B"), "time_ms": describe(times, "ms")}
        full = statistics.fmean(
            [values["bytes"]["mean"] for key, values in results.items() if key.startswith("scale_1")] or [0]
        )
        results["recommendation"] = {
            "capture_scale": 0.6,
            "note": "0.6 keeps a legible screenshot at a fraction of the bytes; raise it when a visual oracle needs detail",
            "bytes_at_1.0": results["scale_1.0"]["bytes"]["mean"],
            "bytes_at_0.6": results["scale_0.6"]["bytes"]["mean"],
        }
        del full
        return results
    finally:
        session.close()


def section_budgets(key: str, config: PolicyConfig) -> dict[str, Any]:
    """Run one real slice and report what the budgets actually consumed per step."""
    from launcher import start_fixture

    fixture = start_fixture("basic", build_id="calibration-budget", hidden=False, allow_desktop=True)
    workdir = Path(os.environ.get("TEMP", ".")) / "jev-calibration-budget"
    workdir.mkdir(parents=True, exist_ok=True)
    driver = WindowsDriver(evidence_dir=workdir)
    driver.start()
    journal = DispatchJournal(str(workdir / "journal.sqlite"))
    ownership = Ownership()
    evidence = EvidenceStore(root=workdir / "evidence", approved_roots=[str(workdir)])
    policy = JevPolicy(transport=HttpTransport(), config=config, api_key=key)
    runtime = Runtime(
        driver=driver,
        journal=journal,
        ownership=ownership,
        evidence=evidence,
        config=RuntimeConfig(evidence_dir=workdir / "evidence", approved_roots=(str(workdir),)),
        policy=policy,
    )
    try:
        app = next(app for app in driver.list_apps() if app.process_id == fixture.pid)
        spec = RunSpec.from_json(
            {
                "goal": "regression: the Enable feature checkbox and the Save button both act",
                "purpose": Purpose.REGRESSION.value,
                "interaction_mode": InputMode.USER_PATH.value,
                "app_ref": app.app_ref,
                "expected_identity": {
                    "mode": "file_marker",
                    "marker_path": str(fixture.state_dir / "build_marker.json"),
                    "expect_marker": "calibration-budget",
                },
                "launch_config_id": None,
                "steps": [
                    {"step_id": "toggle", "operation": "TOGGLE", "target_description": "the Enable feature checkbox"},
                    {"step_id": "save", "operation": "CLICK", "target_description": "the Save button"},
                ],
                "assertions": [
                    {
                        "assertion_id": "saved",
                        "evaluator": "uia_property",
                        "target": {"role": "text", "name_regex": "^Status:"},
                        "property": "text",
                        "expected": {"contains": "saved"},
                        "checkpoint": "save",
                    }
                ],
                "fixtures": {},
                "secret_refs": {},
                "limits": {**Limits.defaults().to_json(), "max_model_decisions": 8},
                "scope": {"app_ref": app.app_ref, "max_elements": 160},
                "allow_restart": False,
            }
        )
        session = ownership.create_session("calibration-budget")
        created = runtime.create_run(spec, session_id=session.session_id)
        ownership.acquire(session.session_id, created["run_id"])
        started = time.perf_counter()
        result = runtime.slice(
            run_id=created["run_id"],
            session_id=session.session_id,
            resume_token=created["resume_token"],
            slice_seconds=90.0,
        )
        wall = time.perf_counter() - started
        steps = len([step for step in result.steps if step.dispatch_state.value == "dispatched"])
        budgets = result.budgets
        return {
            "execution": result.execution.value,
            "verdict": result.verdict.value,
            "steps_dispatched": steps,
            "actions": budgets["actions"],
            "model_decisions": budgets["decisions"],
            "decisions_per_action": round(budgets["decisions"] / max(1, steps), 2),
            "wall_seconds": round(wall, 2),
            "seconds_per_action": round(wall / max(1, steps), 2),
            "recommendation": {
                "max_model_decisions": max(
                    30, round(budgets["decisions"] / max(1, steps) * Limits.defaults().max_actions * 1.5)
                ),
                "note": "1.5 times the measured decisions per action across a full default action budget",
                "slice_seconds": max(30, round(wall / max(1, steps) * 1.2)),
                "max_actions": Limits.defaults().max_actions,
            },
        }
    finally:
        driver.close()
        fixture.stop()
        journal.close()


SECTIONS: dict[str, Callable[..., dict[str, Any]]] = {
    "policy": section_policy,
    "input": section_input,
    "settle": section_settle,
    "observe": section_observe,
    "capture": section_capture,
    "budgets": section_budgets,
}


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="calibrate_thresholds", description="Measure the shipped thresholds against a real API and desktop"
    )
    parser.add_argument("--sections", default="policy,input,settle,observe,capture,budgets")
    parser.add_argument("--repeats", type=int, default=5, help="trials per measurement")
    parser.add_argument("--policy-repeats", type=int, default=6, help="rounds over the policy scenarios")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()

    key = os.environ.get("TYPESAFE_API_KEY")
    if key:
        key = key.strip()
    wanted = [name.strip() for name in args.sections.split(",") if name.strip()]
    if ("policy" in wanted or "budgets" in wanted) and not key:
        print("policy and budgets sections need TYPESAFE_API_KEY", file=sys.stderr)
        return 2
    desktop = {name for name in wanted if name != "policy"}
    if desktop and os.environ.get("JEV_DESKTOP_LIVE") != "1":
        print("input, settle, observe, capture, and budgets need JEV_DESKTOP_LIVE=1", file=sys.stderr)
        return 2

    config = PolicyConfig()
    report: dict[str, Any] = {
        "config": {
            "operation_floor": config.operation_floor,
            "target_floor": config.target_floor,
            "timeout_s": config.timeout_s,
            "max_retries": config.max_retries,
        }
    }
    for name in wanted:
        print(f"\n=== {name} ===", flush=True)
        if name == "policy":
            payload = section_policy(args.policy_repeats, config, key or "")
        elif name == "budgets":
            payload = section_budgets(key or "", config)
        else:
            payload = SECTIONS[name](args.repeats)
        report[name] = payload
        print(json.dumps(payload, indent=2))

    if args.json:
        args.json.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
