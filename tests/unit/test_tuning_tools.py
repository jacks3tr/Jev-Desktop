"""Decision recording and the offline gate sweep: no desktop input, no network."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

from jev_desktop.policy import RecordingTransport

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def choice(selected: str, probabilities: dict[str, float], confidence: float) -> dict:
    return {"type": "choice", "choice": selected, "probabilities": probabilities, "confidence": confidence}


REQUEST = {
    "model": "jev-1.13.0",
    "state": {"goal": "Save", "application": {"window_titles": ["Editor"]}},
    "questions": {
        "operation": {"type": "choice", "criteria": {"CLICK": "c", "WAIT": "w", "ESCALATE": "e"}},
        "CLICK_target": {"type": "choice", "criteria": {"t1": "Save", "t2": "Save as", "NONE": "none"}},
    },
}
RESPONSE = {
    "model": "jev-1.13.0",
    "answers": {
        "operation": choice("CLICK", {"CLICK": 0.9, "WAIT": 0.05, "ESCALATE": 0.05}, 0.85),
        "CLICK_target": choice("t1", {"t1": 0.5, "t2": 0.42, "NONE": 0.08}, 0.4),
    },
    "usage": {"input_tokens": 10, "output_tokens": 1},
}


class Inner:
    retry_after_s = 2.0

    def post_json(self, url, *, headers, payload, timeout_s):
        return 200, RESPONSE


def test_recording_transport_keeps_bodies_but_never_headers(tmp_path):
    transport = RecordingTransport(Inner(), tmp_path / "records")
    status, body = transport.post_json(
        "https://example.invalid", headers={"Authorization": "Bearer secret-key"}, payload=REQUEST, timeout_s=1.0
    )
    assert (status, body) == (200, RESPONSE)
    assert transport.retry_after_s == 2.0
    [path] = (tmp_path / "records").glob("decisions-*.jsonl")
    text = path.read_text(encoding="utf-8")
    assert "secret-key" not in text and "Authorization" not in text
    record = json.loads(text)
    assert record["request"] == REQUEST and record["response"] == RESPONSE and record["status"] == 200


def test_export_computes_margin_and_keeps_existing_labels(tmp_path):
    export = load_script("export_decisions")
    recording = tmp_path / "decisions.jsonl"
    recording.write_text(json.dumps({"status": 200, "request": REQUEST, "response": RESPONSE}) + "\n", encoding="utf-8")
    output = tmp_path / "rows.json"
    assert export.export(output, [recording]) == {"rows": 1, "added": 1, "unlabeled": 1}
    [row] = json.loads(output.read_text(encoding="utf-8"))
    assert row["target"] == "t1" and row["target_margin"] == 0.08 and row["candidate_count"] == 2
    assert row["candidates"] == {"CLICK": {"t1": "Save", "t2": "Save as"}}
    row.update(split="calibration", expected_operation="CLICK", expected_targets=["t1"])
    output.write_text(json.dumps([row]), encoding="utf-8")
    assert export.export(output, [recording]) == {"rows": 1, "added": 0, "unlabeled": 0}


def labeled(split, target, expected, *, margin, confidence=0.9, operation="CLICK", model="jev-1.13.0"):
    return {
        "split": split,
        "model": model,
        "operation": operation,
        "operation_confidence": 0.9,
        "target": target,
        "target_confidence": confidence,
        "target_margin": margin,
        "candidate_count": 30,
        "expected_operation": "CLICK",
        "expected_targets": expected,
    }


def test_sweep_raises_the_margin_to_refuse_a_near_tie():
    evaluate = load_script("evaluate_thresholds").evaluate
    rows = [
        labeled("calibration", "t1", ["t1"], margin=0.6),
        labeled("calibration", "t2", ["t1"], margin=0.05),  # confident, but a near tie on the wrong control
        labeled("calibration", "t3", [], margin=0.7, confidence=0.3),  # no action appropriate
        labeled("validation", "t1", ["t1"], margin=0.5),
        labeled("validation", "t4", ["t5"], margin=0.02),
        {**labeled("validation", "t1", ["t1"], margin=0.3), "expected_operation": None},  # unlabeled
    ]
    report = evaluate(rows)
    assert report["margin_swept"] is True
    # Only the margin separates the near tie; ties then settle on the current defaults.
    assert report["selected"] == {"operation_floor": 0.35, "target_floor": 0.45, "target_margin": 0.1}
    assert report["calibration"] == {
        "correct_actions": 1,
        "wrong_actions": 0,
        "unnecessary_refusals": 1,  # refusing the near tie was right, but an action was still needed
        "safe_refusals": 1,
    }
    assert report["validation"] == {
        "correct_actions": 1,
        "wrong_actions": 0,
        "unnecessary_refusals": 1,
        "safe_refusals": 0,
    }
    assert report["current_defaults"]["calibration"]["wrong_actions"] == 0
    assert report["counts"] == {"calibration": 3, "validation": 2, "unlabeled": 1}


def test_sweep_accepts_legacy_rows_without_margins():
    evaluate = load_script("evaluate_thresholds").evaluate
    legacy = []
    for split in ("calibration", "validation"):
        for target, expected in (("t1", "t1"), ("t2", "NONE")):
            row = labeled(split, target, None, margin=None)
            del row["expected_targets"]
            legacy.append({**row, "expected_target": expected, "target_confidence": 0.9 if expected != "NONE" else 0.3})
    report = evaluate(legacy, step=0.1)
    assert report["margin_swept"] is False
    assert report["validation"]["wrong_actions"] == 0


def test_sweep_refuses_mixed_models_and_missing_splits():
    evaluate = load_script("evaluate_thresholds").evaluate
    base = [labeled("calibration", "t1", ["t1"], margin=0.6), labeled("calibration", "t2", [], margin=0.6)]
    with pytest.raises(ValueError, match="calibration and validation"):
        evaluate(base)
    with pytest.raises(ValueError, match="model versions"):
        evaluate([*base, labeled("validation", "t1", ["t1"], margin=0.6, model="jev-1.12.0")])
