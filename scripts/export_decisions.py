"""Turn recorded Jev requests (JEV_DESKTOP_RECORD) into rows to label for evaluate_thresholds.py.

Re-running over new recordings keeps the rows, and labels, already in the output file.
Label each row with `split` ("calibration" or "validation"; hold out whole applications),
`expected_operation`, and `expected_targets`: every acceptable target key from `candidates`,
or an empty list when no action is appropriate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


def row_from_record(record: dict[str, Any]) -> dict[str, Any] | None:
    request, response = record.get("request"), record.get("response")
    if record.get("status") != 200 or not isinstance(response, dict) or not isinstance(response.get("answers"), dict):
        return None
    questions, answers = request["questions"], response["answers"]
    operation_answer = answers.get("operation")
    if not isinstance(operation_answer, dict) or "choice" not in operation_answer:
        return None
    operation = operation_answer["choice"]
    candidates = {
        key.removesuffix("_target"): {option: text for option, text in question["criteria"].items() if option != "NONE"}
        for key, question in questions.items()
        if key.endswith("_target")
    }
    target = answers.get(f"{operation}_target") if operation in candidates else None
    margin = None
    if isinstance(target, dict):
        ranked = sorted(target["probabilities"].values(), reverse=True)
        margin = round(ranked[0] - (ranked[1] if len(ranked) > 1 else 0.0), 4)
    state = request.get("state") or {}
    application = state.get("application") or {}
    canonical = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return {
        "id": hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16],
        "recorded_at": record.get("recorded_at"),
        "model": response.get("model"),
        "split": None,
        "application": None,
        "goal": state.get("goal"),
        "current_step": state.get("current_step") or None,
        "window_titles": application.get("window_titles", []),
        "operation_options": list(questions["operation"]["criteria"]),
        "candidates": candidates,
        "operation": operation,
        "operation_confidence": operation_answer.get("confidence"),
        "target": target.get("choice") if isinstance(target, dict) else None,
        "target_confidence": target.get("confidence") if isinstance(target, dict) else None,
        "target_margin": margin,
        "candidate_count": len(candidates[operation]) if operation in candidates else None,
        "expected_operation": None,
        "expected_targets": None,
    }


def export(output: Path, recordings: list[Path]) -> dict[str, int]:
    rows = json.loads(output.read_text(encoding="utf-8")) if output.exists() else []
    seen = {row["id"] for row in rows}
    added = 0
    for path in recordings:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = row_from_record(json.loads(line))
            if row is None or row["id"] in seen:
                continue
            seen.add(row["id"])
            rows.append(row)
            added += 1
    output.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"rows": len(rows), "added": added, "unlabeled": sum(row.get("expected_operation") is None for row in rows)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("output", type=Path, help="labeled rows JSON file (created or extended)")
    parser.add_argument("recordings", type=Path, nargs="+", help="decisions-*.jsonl files")
    args = parser.parse_args()
    print(json.dumps(export(args.output, args.recordings)))
