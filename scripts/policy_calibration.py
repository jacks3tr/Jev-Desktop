"""Calibrate the Jev policy against the live TypeSafe API.

Runs the real request and answer-validation path in `jev_desktop.policy` over scenarios
shaped like the ones this plugin actually issues, and reports what the API resolved, how
confident it was, and what it cost.

Usage:

    set TYPESAFE_API_KEY=...            # or pass --env-file path/to/.env
    python scripts/policy_calibration.py --repeats 2 --observe-fixture
    python scripts/policy_calibration.py --json calibration.json

The key is read from the environment or a dotenv file and never written to disk by this
script. Results are printed and, on request, saved as JSON for the record.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jev_desktop.contracts import Operation, Pause, TargetCandidate
from jev_desktop.policy import HttpTransport, JevPolicy, OpContext, PolicyConfig, PolicyError


def read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        values[name.strip()] = value.strip().strip('"').strip("'")
    return values


def synthetic_observation() -> dict[str, Any]:
    """Element payload shaped exactly like the one the runtime sends to the policy."""

    def element(index: int, element_id: str, role: str, name: str, **extra: Any) -> dict[str, Any]:
        payload = {
            "index": index,
            "element_id": element_id,
            "role": role,
            "name": name,
            "value": extra.get("value"),
            "enabled": extra.get("enabled", True),
            "visible": True,
            "editable": extra.get("editable", False),
            "focused": extra.get("focused", False),
            "operations": extra.get("operations", ["CLICK"]),
            "path": ["Jev Fixture Window"],
            "state": extra.get("state", {}),
            "truncation": None,
        }
        return payload

    elements = [
        element(1, "el:" + "1" * 23 + "a", "text", "Name", operations=[]),
        element(2, "el:" + "1" * 23 + "b", "edit", "Name", editable=True, operations=["TYPE_TEXT", "CLICK"]),
        element(3, "el:" + "1" * 23 + "c", "combobox", "", value="Draft", operations=["SELECT", "TYPE_TEXT"]),
        element(
            4,
            "el:" + "1" * 23 + "d",
            "checkbox",
            "Enable feature",
            state={"checked": "off"},
            operations=["CLICK", "TOGGLE"],
        ),
        element(5, "el:" + "1" * 23 + "e", "button", "Save", operations=["CLICK"]),
        element(6, "el:" + "1" * 23 + "f", "button", "Open dialog", operations=["CLICK"]),
        element(7, "el:" + "1" * 23 + "g", "button", "Export", operations=["CLICK"]),
        element(8, "el:" + "1" * 23 + "h", "listitem", "Item 01", operations=["CLICK", "SELECT"]),
        element(9, "el:" + "1" * 23 + "i", "text", "Status: ready", operations=[]),
    ]
    return {
        "elements": elements,
        "context": {
            "window_titles": ["Jev Fixture - basic - build fixture-1"],
            "modal_windows": [],
            "texts": [{"role": "text", "name": "Status: ready", "text": "Status: ready"}],
            "coverage": "partial",
            "truncation": ["6 offscreen elements were not observed"],
            "focused_element_id": None,
        },
    }


def observed_state(truncation: list[str]) -> dict[str, Any]:
    """Rebuild the policy state shape from a real observation of the fixture application."""
    from jev_desktop.contracts import ScopeSpec
    from jev_desktop.drivers.windows import WindowsDriver

    sys.path.insert(0, str(ROOT / "tests" / "fixtures"))
    from launcher import start_fixture  # type: ignore[import-not-found]

    fixture = start_fixture("basic", build_id="calibration", hidden=False, allow_desktop=True)
    driver = WindowsDriver(evidence_dir=Path(os.environ.get("TEMP", ".")) / "jev-calibration")
    driver.start()
    try:
        app = next(app for app in driver.list_apps() if app.process_id == fixture.pid)
        snapshot = driver.observe(ScopeSpec(app_ref=app.app_ref, max_elements=240))
        elements = [
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
        ]
        truncation.extend(snapshot.truncation)
        return {
            "elements": elements,
            "context": {
                "window_titles": [window.title for window in snapshot.windows],
                "modal_windows": list(snapshot.context.get("modal_windows") or []),
                "texts": list(snapshot.context.get("texts") or []),
                "coverage": snapshot.coverage.value,
                "truncation": list(snapshot.truncation),
                "focused_element_id": snapshot.context.get("focused_element_id"),
            },
        }
    finally:
        driver.close()
        fixture.stop()


def candidates_for(
    state: dict[str, Any], operation: Operation, wanted: str | None = None
) -> tuple[TargetCandidate, ...]:
    from jev_desktop.policy import build_contexts

    contexts = build_contexts(observation=state, operations=[operation])
    if not contexts:
        return ()
    items = contexts[0].candidates
    if wanted is None:
        return items
    narrowed = tuple(item for item in items if wanted.lower() in item.description.lower())
    return narrowed or items


def scenario_specs(state: dict[str, Any]) -> list[dict[str, Any]]:
    click_targets = candidates_for(state, Operation.CLICK, "Save")
    type_targets = candidates_for(state, Operation.TYPE_TEXT)
    toggle_targets = candidates_for(state, Operation.TOGGLE, "Enable feature")
    save_exact = tuple(item for item in click_targets if 'name="Save"' in item.description) or click_targets
    name_edit = tuple(
        item for item in type_targets if item.description.startswith("[2]") or 'name="Name"' in item.description
    )
    return [
        {
            "name": "click the Save button",
            "goal": "regression: the Save button must persist the document",
            "step": {"step_id": "save", "operation": "CLICK", "target_description": "the Save button"},
            "contexts": [OpContext(operation=Operation.CLICK, candidates=save_exact)],
            "expect_operation": Operation.CLICK,
            "expect_target_contains": 'name="Save"',
        },
        {
            "name": "choose the editable Name field for typing",
            "goal": "regression: type the caller's fixture value into the Name field",
            "step": {
                "step_id": "type-name",
                "operation": "TYPE_TEXT",
                "target_description": "the Name text field",
                "fixture_reference": "name_value",
            },
            "contexts": [
                OpContext(
                    operation=Operation.TYPE_TEXT,
                    candidates=type_targets or name_edit,
                    note="step=type-name fixture=name_value",
                )
            ],
            "expect_operation": Operation.TYPE_TEXT,
            "expect_target_contains": None,
        },
        {
            "name": "toggle the Enable feature checkbox",
            "goal": "regression: the Enable feature checkbox reports the state it was toggled into",
            "step": {"step_id": "toggle", "operation": "TOGGLE", "target_description": "the Enable feature checkbox"},
            "contexts": [OpContext(operation=Operation.TOGGLE, candidates=toggle_targets)],
            "expect_operation": Operation.TOGGLE,
            "expect_target_contains": "Enable feature",
        },
        {
            "name": "nothing left to do, request verification",
            "goal": "regression: every required step already completed, the document is saved",
            "step": {"step_id": "finish", "operation": "WAIT", "target_description": "no further interaction"},
            "contexts": [],
            "allow_done": True,
            "expect_operation": Operation.DONE,
            "expect_target_contains": None,
        },
    ]


def run(repeats: int, use_fixture: bool, config: PolicyConfig, key: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    truncation: list[str] = []
    state = observed_state(truncation) if use_fixture else synthetic_observation()
    if not use_fixture:
        truncation.extend(state["context"]["truncation"])
    policy = JevPolicy(transport=HttpTransport(), config=config, api_key=key)
    results: list[dict[str, Any]] = []
    for round_index in range(repeats):
        for spec in scenario_specs(state):
            entry: dict[str, Any] = {
                "scenario": spec["name"],
                "round": round_index + 1,
                "ok": False,
                "expected_operation": spec["expect_operation"].value,
                "expect_target_contains": spec["expect_target_contains"],
            }
            started = time.perf_counter()
            try:
                if not spec["contexts"]:
                    decision = policy.decide(
                        goal=spec["goal"],
                        state=state,
                        contexts=[],
                        allow_done=True,
                        allow_escalate=True,
                        current_step=spec["step"],
                    )
                else:
                    decision = policy.decide(
                        goal=spec["goal"],
                        state=state,
                        contexts=spec["contexts"],
                        allow_done=spec.get("allow_done", False),
                        allow_escalate=True,
                        current_step=spec["step"],
                    )
                elapsed = (time.perf_counter() - started) * 1000
                entry.update(
                    {
                        "ok": True,
                        "operation": decision.operation.value,
                        "target": decision.target.description if decision.target else None,
                        "operation_confidence": round(decision.operation_confidence, 4),
                        "target_confidence": None
                        if decision.target_confidence is None
                        else round(decision.target_confidence, 4),
                        "model": decision.model,
                        "latency_ms": round(elapsed),
                        "usage": decision.usage,
                        "operation_matched": decision.operation is spec["expect_operation"],
                        "target_matched": (
                            None
                            if spec["expect_target_contains"] is None
                            else bool(decision.target and spec["expect_target_contains"] in decision.target.description)
                        ),
                    }
                )
            except (Pause, PolicyError) as exc:
                entry.update(
                    {
                        "ok": False,
                        "error": f"{type(exc).__name__}: {exc}",
                        "reason": getattr(exc, "reason_value", None),
                        "detail": dict(getattr(exc, "detail", {}) or {}),
                        "latency_ms": round((time.perf_counter() - started) * 1000),
                    }
                )
            results.append(entry)
            marker = "ok " if entry["ok"] else "FAIL"
            print(
                f"  [{marker}] {spec['name']:<42} -> {entry.get('operation', entry.get('error'))}"
                f"  op_conf={entry.get('operation_confidence')} target_conf={entry.get('target_confidence')}"
                f"  {entry['latency_ms']} ms"
            )
            if entry.get("target") and entry.get("target_matched") is False:
                print(f"          target was: {entry['target'][:110]}")
    summary = {
        "state_source": "live fixture observation" if use_fixture else "synthetic fixture-shaped state",
        "truncation": truncation,
        "requests": len(results),
        "failures": [entry for entry in results if not entry["ok"]],
        "models": sorted({entry.get("model") for entry in results if entry.get("model")}),
        "refused_operation_confidence": [
            entry.get("detail", {}).get("confidence")
            for entry in results
            if not entry["ok"] and entry.get("reason") == "low_confidence"
        ],
        "operation_confidence": {
            "min": min((entry["operation_confidence"] for entry in results if entry["ok"]), default=None),
            "median": round(statistics.median([entry["operation_confidence"] for entry in results if entry["ok"]]), 4)
            if any(entry["ok"] for entry in results)
            else None,
        },
        "target_confidence": {
            "min": min(
                (
                    entry["target_confidence"]
                    for entry in results
                    if entry["ok"] and entry["target_confidence"] is not None
                ),
                default=None,
            ),
            "median": round(
                statistics.median(
                    [
                        entry["target_confidence"]
                        for entry in results
                        if entry["ok"] and entry["target_confidence"] is not None
                    ]
                ),
                4,
            )
            if any(entry["ok"] and entry["target_confidence"] is not None for entry in results)
            else None,
        },
        "operation_match_rate": f"{sum(entry.get('operation_matched', False) for entry in results)}/{len(results)}",
        "target_match_rate": f"{sum(entry.get('target_matched') is True for entry in results)}/"
        f"{sum(entry.get('target_matched') is not None for entry in results)}",
        "latency_ms": {
            "min": min(entry["latency_ms"] for entry in results),
            "median": round(statistics.median([entry["latency_ms"] for entry in results])),
            "max": max(entry["latency_ms"] for entry in results),
        },
        "input_tokens": sum(int(entry.get("usage", {}).get("input_tokens", 0)) for entry in results),
        "output_tokens": sum(int(entry.get("usage", {}).get("output_tokens", 0)) for entry in results),
        "floors": {"operation": config.operation_floor, "target": config.target_floor},
    }
    return results, summary


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="policy_calibration", description="Live TypeSafe calibration for the Jev policy"
    )
    parser.add_argument("--repeats", type=int, default=2, help="rounds over every scenario")
    parser.add_argument("--env-file", type=Path, help="dotenv file holding TYPESAFE_API_KEY")
    parser.add_argument("--model", default=PolicyConfig().model_id)
    parser.add_argument("--operation-floor", type=float, default=PolicyConfig().operation_floor)
    parser.add_argument("--target-floor", type=float, default=PolicyConfig().target_floor)
    parser.add_argument(
        "--observe-fixture",
        action="store_true",
        help="use a real observation of the fixture application (needs JEV_DESKTOP_LIVE=1)",
    )
    parser.add_argument("--json", type=Path, help="write the full results here")
    args = parser.parse_args()

    key = os.environ.get("TYPESAFE_API_KEY")
    if not key and args.env_file:
        key = read_dotenv(args.env_file).get("TYPESAFE_API_KEY")
    if not key:
        print("no TYPESAFE_API_KEY in the environment and none found in the given dotenv file", file=sys.stderr)
        return 2

    config = PolicyConfig(model_id=args.model, operation_floor=args.operation_floor, target_floor=args.target_floor)
    print(f"pinned model: {config.model_id} | floors: op={config.operation_floor} target={config.target_floor}")
    results, summary = run(args.repeats, args.observe_fixture, config, key)
    print("\nsummary:")
    print(json.dumps(summary, indent=2))
    if args.json:
        args.json.write_text(json.dumps({"summary": summary, "results": results}, indent=2), encoding="utf-8")
        print(f"wrote {args.json}")
    return 0 if not summary["failures"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
