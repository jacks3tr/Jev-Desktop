"""Compare confidence gates on labeled real Jev decisions without replaying input.

Sweeps the operation floor, target floor, and target margin together. Gates only read the
recorded answers, so no request is sent. Rows come from export_decisions.py, or the older
format with a single `expected_target` ("NONE" when no action is appropriate).
"""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

NON_ACTING = {"WAIT", "DONE", "ESCALATE"}
DEFAULTS = (0.35, 0.45, 0.1)  # PolicyConfig: operation_floor, target_floor, target_margin
MAX_MARGIN = 0.5


def _probability(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1


def normalize(row: dict[str, Any]) -> dict[str, Any]:
    """Validate one labeled row and reduce it to what the gates and labels need."""
    expected = row.get("expected_targets")
    if expected is None and "expected_target" in row:
        expected = [] if row["expected_target"] == "NONE" else [row["expected_target"]]
    if not isinstance(row.get("expected_operation"), str) or not isinstance(expected, list):
        raise ValueError(f"row {row.get('id', '?')} is missing expected_operation or expected_targets")
    if row.get("split") not in {"calibration", "validation"}:
        raise ValueError(f"row {row.get('id', '?')} needs split calibration or validation")
    operation, target = row["operation"], row.get("target")
    acting = operation not in NON_ACTING and target not in (None, "NONE")
    if not _probability(row.get("operation_confidence")):
        raise ValueError(f"row {row.get('id', '?')} has an invalid operation_confidence")
    if acting and not _probability(row.get("target_confidence")):
        raise ValueError(f"row {row.get('id', '?')} has an invalid target_confidence")
    margin = row.get("target_margin")
    if margin is not None and not _probability(margin):
        raise ValueError(f"row {row.get('id', '?')} has an invalid target_margin")
    count = row.get("candidate_count")
    return {
        "split": row["split"],
        "model": row.get("model"),
        "operation": operation,
        "acting": acting,
        "operation_confidence": float(row["operation_confidence"]),
        "target_confidence": float(row["target_confidence"]) if acting else 0.0,
        "target_margin": None if margin is None else float(margin),
        "appropriate": operation == row["expected_operation"] and target in expected,
        "action_expected": bool(expected) and row["expected_operation"] not in NON_ACTING,
        "bucket": "unknown" if count is None else "2" if count <= 2 else "3-20" if count <= 20 else "21+",
    }


def accepted(row: dict[str, Any], gates: tuple[float, float, float]) -> bool:
    operation_floor, target_floor, margin = gates
    return (
        row["acting"]
        and row["operation_confidence"] >= operation_floor
        and row["target_confidence"] >= target_floor
        and (row["target_margin"] is None or row["target_margin"] + 1e-9 >= margin)
    )


def metrics(rows: list[dict[str, Any]], gates: tuple[float, float, float]) -> dict[str, int]:
    correct = wrong = refused = safe_refusals = 0
    for row in rows:
        if accepted(row, gates):
            correct += int(row["appropriate"])
            wrong += int(not row["appropriate"])
        else:
            refused += int(row["action_expected"])
            safe_refusals += int(not row["action_expected"])
    return {
        "correct_actions": correct,
        "wrong_actions": wrong,
        "unnecessary_refusals": refused,
        "safe_refusals": safe_refusals,
    }


def breakdown(rows: list[dict[str, Any]], gates: tuple[float, float, float], key: str) -> dict[str, dict[str, int]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[row[key]].append(row)
    return {name: metrics(group, gates) for name, group in sorted(groups.items())}


def _gates(values: tuple[float, float, float]) -> dict[str, float]:
    return dict(zip(("operation_floor", "target_floor", "target_margin"), values, strict=True))


def evaluate(raw_rows: list[dict[str, Any]], *, step: float = 0.05, wrong_cost: float | None = None) -> dict[str, Any]:
    labeled = [row for row in raw_rows if row.get("expected_operation") is not None]
    rows = [normalize(row) for row in labeled]
    if {row["split"] for row in rows} != {"calibration", "validation"}:
        raise ValueError("both calibration and validation records are required")
    models = {row["model"] for row in rows}
    if len(models) != 1:
        raise ValueError("do not combine different model versions")
    calibration = [row for row in rows if row["split"] == "calibration"]
    validation = [row for row in rows if row["split"] == "validation"]
    if not any(not row["action_expected"] for row in calibration):
        raise ValueError("calibration requires cases where no action is appropriate")
    # Older rows lack margins; sweeping a gate they cannot evaluate would be meaningless.
    margin_known = all(row["target_margin"] is not None for row in rows if row["acting"])
    steps = round(1 / step)
    floors = [index / steps for index in range(steps + 1)]
    margins = [value for value in floors if value <= MAX_MARGIN] if margin_known else [0.0]

    def loss(result: dict[str, int]) -> tuple[float, ...]:
        if wrong_cost is None:  # no wrong action is worth any number of refusals
            return (result["wrong_actions"], -result["correct_actions"], result["unnecessary_refusals"])
        return (wrong_cost * result["wrong_actions"] + result["unnecessary_refusals"], -result["correct_actions"])

    def distance(gates: tuple[float, float, float]) -> float:
        return sum(abs(value - default) for value, default in zip(gates, DEFAULTS, strict=True))

    scored = []
    for operation_floor in floors:
        for target_floor in floors:
            for margin in margins:
                gates = (operation_floor, target_floor, margin)
                result = metrics(calibration, gates)
                scored.append((loss(result), result, gates))
    best = min(item[0] for item in scored)
    tied = [gates for score, _result, gates in scored if score == best]
    # A tie is no evidence to change the existing defaults. Validation never selects gates.
    selected = min(tied, key=lambda gates: (distance(gates), gates))

    frontier: dict[int, tuple[int, tuple[float, float, float]]] = {}
    for _score, result, gates in scored:
        wrong, correct = result["wrong_actions"], result["correct_actions"]
        current = frontier.get(wrong)
        if current is None or (correct, -distance(gates)) > (current[0], -distance(current[1])):
            frontier[wrong] = (correct, gates)
    held_out = metrics(validation, selected)
    accepted_held_out = held_out["correct_actions"] + held_out["wrong_actions"]
    defaults = DEFAULTS if margin_known else (DEFAULTS[0], DEFAULTS[1], 0.0)
    return {
        "model": next(iter(models)),
        "objective": (
            "minimize wrong actions, then maximize correct actions; ties prefer existing defaults"
            if wrong_cost is None
            else f"minimize {wrong_cost} x wrong actions + unnecessary refusals; ties prefer existing defaults"
        ),
        "margin_swept": margin_known,
        "grid_step": step,
        "selected": _gates(selected),
        "equally_scoring_settings": len(tied),
        "calibration": metrics(calibration, selected),
        "validation": held_out,
        # Rule of three: zero wrong in n accepted bounds the wrong-action rate near 3/n (95%).
        "validation_wrong_rate_upper_95": (
            round(3 / accepted_held_out, 4) if held_out["wrong_actions"] == 0 and accepted_held_out else None
        ),
        "current_defaults": {
            "gates": _gates(defaults),
            "calibration": metrics(calibration, defaults),
            "validation": metrics(validation, defaults),
        },
        "calibration_frontier": [
            {"wrong_actions": wrong, "correct_actions": correct, **_gates(gates)}
            for wrong, (correct, gates) in sorted(frontier.items())[:6]
        ],
        "validation_by_operation": breakdown(validation, selected, "operation"),
        "validation_by_candidate_count": breakdown(validation, selected, "bucket"),
        "counts": {
            "calibration": len(calibration),
            "validation": len(validation),
            "unlabeled": len(raw_rows) - len(labeled),
        },
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("records", type=Path)
    parser.add_argument("--step", type=float, default=0.05, help="grid step for every gate (default 0.05)")
    parser.add_argument(
        "--wrong-cost",
        type=float,
        help="weigh one wrong action as this many unnecessary refusals instead of ranking wrong actions first",
    )
    args = parser.parse_args()
    report = evaluate(json.loads(args.records.read_text(encoding="utf-8")), step=args.step, wrong_cost=args.wrong_cost)
    print(json.dumps(report, indent=2))
