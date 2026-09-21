"""Compare confidence gates on labeled real Jev decisions without replaying input."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path


def metrics(rows: list[dict], operation_floor: float, target_floor: float) -> dict[str, int]:
    correct = wrong = refused = safe_refusals = 0
    for row in rows:
        accepted = (
            row["operation"] not in {"WAIT", "ESCALATE", "DONE"}
            and row["target"] != "NONE"
            and row["operation_confidence"] >= operation_floor
            and row["target_confidence"] >= target_floor
        )
        appropriate = (
            row["expected_target"] != "NONE"
            and row["target"] == row["expected_target"]
            and row["operation"] == row["expected_operation"]
        )
        correct += int(accepted and appropriate)
        wrong += int(accepted and not appropriate)
        refused += int(not accepted and row["expected_target"] != "NONE")
        safe_refusals += int(not accepted and row["expected_target"] == "NONE")
    return {
        "correct_actions": correct,
        "wrong_actions": wrong,
        "unnecessary_refusals": refused,
        "safe_refusals": safe_refusals,
    }


def evaluate(rows: list[dict]) -> dict:
    if not rows or {r["split"] for r in rows} != {"calibration", "validation"}:
        raise ValueError("both calibration and validation records are required")
    models = {r["model"] for r in rows}
    if len(models) != 1:
        raise ValueError("do not combine different model versions")
    for row in rows:
        for key in ("operation_confidence", "target_confidence"):
            value = row[key]
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError(f"invalid {key}")
        for key in ("expected_target", "expected_operation", "target", "operation"):
            if not isinstance(row[key], str) or not row[key]:
                raise ValueError(f"missing {key}")
    calibration = [r for r in rows if r["split"] == "calibration"]
    validation = [r for r in rows if r["split"] == "validation"]
    if not any(r["expected_target"] == "NONE" for r in calibration):
        raise ValueError("calibration requires absent-target cases")
    scored = []
    for op in range(101):
        for target in range(101):
            result = metrics(calibration, op / 100, target / 100)
            loss = (result["wrong_actions"], -result["correct_actions"], result["unnecessary_refusals"])
            scored.append((loss, op, target))
    best_loss = min(item[0] for item in scored)
    tied = [(op, target) for loss, op, target in scored if loss == best_loss]
    # A tie is no evidence to change the existing defaults. Validation never selects gates.
    op, target = min(tied, key=lambda pair: (abs(pair[0] - 35) + abs(pair[1] - 45), pair))
    return {
        "model": next(iter(models)),
        "objective": "minimize wrong actions, maximize correct actions; ties prefer existing defaults",
        "operation_floor": op / 100,
        "target_floor": target / 100,
        "equally_scoring_pairs": len(tied),
        "grid_step": 0.01,
        "calibration_count": len(calibration),
        "validation_count": len(validation),
        "calibration": metrics(calibration, op / 100, target / 100),
        "validation": metrics(validation, op / 100, target / 100),
        "limitation": "Small correlated sample; held-out instructions reuse application states. No general accuracy guarantee.",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("records", type=Path)
    args = parser.parse_args()
    print(json.dumps(evaluate(json.loads(args.records.read_text(encoding="utf-8"))), indent=2))
