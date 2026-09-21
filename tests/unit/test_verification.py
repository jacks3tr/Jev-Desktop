"""Assertion evaluation and conservative verdict aggregation."""

from __future__ import annotations

import json
from pathlib import Path

from jev_desktop.contracts import (
    AssertionResult,
    AssertionSpec,
    AssertionStatus,
    Coverage,
    ElementInfo,
    Evaluator,
    Geometry,
    Rect,
    Snapshot,
    Verdict,
    new_id,
    now,
)
from jev_desktop.evidence import EvidenceStore
from jev_desktop.verification import (
    ORIGIN_APPLICATION,
    ORIGIN_ENVIRONMENT,
    ORIGIN_RUNNER,
    EvalContext,
    aggregate_verdict,
    evaluate,
    qualified_assertions,
)

RUN_ID = "run:" + "a" * 24
WINDOW_REF = "win:" + "b" * 24


def element(role: str, name: str, value: str | None = None) -> ElementInfo:
    return ElementInfo(
        element_id=new_id("el"),
        window_ref=WINDOW_REF,
        role=role,
        name=name,
        value=value,
        enabled=True,
        visible=True,
        editable=False,
        focusable=True,
        focused=False,
        operations=("CLICK",),
        rect=Rect(10, 10, 110, 40),
        index=1,
        path=(),
        state={},
        text=value or name,
        truncation=None,
    )


def snapshot(*elements: ElementInfo, coverage: Coverage = Coverage.COMPLETE, truncation=()) -> Snapshot:
    return Snapshot(
        snapshot_id=new_id("snap"),
        app_ref="app:" + "c" * 24,
        captured_at=now(),
        interval_ms=2,
        geometry=Geometry(1, 0, 0, 1920, 1080, 96, 1.0),
        fingerprint="f",
        coverage=coverage,
        truncation=tuple(truncation),
        windows=(),
        elements=tuple(elements),
        context={},
        notes=(),
    )


def context(tmp_path: Path, *, caller_results=None, observation=None) -> EvalContext:
    store = EvidenceStore(root=tmp_path / "evidence", approved_roots=[str(tmp_path)])
    return EvalContext(
        observation=observation,
        evidence=store,
        approved_roots=[str(tmp_path)],
        caller_results=caller_results or {},
        run_id=RUN_ID,
        checkpoint="step-1",
    )


# --------------------------------------------------------------------------------------
# Element and window assertions
# --------------------------------------------------------------------------------------


def test_element_value_assertion_passes_and_fails(tmp_path):
    spec = AssertionSpec(
        assertion_id="saved-flag",
        evaluator=Evaluator.UIA_PROPERTY,
        target={"role": "text", "name": "Saved"},
        expected={"equals": "yes"},
        property="value",
    )
    passing = evaluate(spec, context(tmp_path, observation=snapshot(element("text", "Saved", "yes"))))
    failing = evaluate(spec, context(tmp_path, observation=snapshot(element("text", "Saved", "no"))))
    assert passing.status is AssertionStatus.PASSED and passing.origin == ORIGIN_APPLICATION
    assert failing.status is AssertionStatus.FAILED and failing.origin == ORIGIN_APPLICATION


def test_absence_requires_complete_coverage(tmp_path):
    spec = AssertionSpec(
        assertion_id="no-error",
        evaluator=Evaluator.UIA_ABSENCE,
        target={"role": "text", "name": "Error"},
        expected={},
    )
    complete = evaluate(spec, context(tmp_path, observation=snapshot(element("text", "Status"))))
    truncated = evaluate(
        spec,
        context(
            tmp_path,
            observation=snapshot(element("text", "Status"), coverage=Coverage.TRUNCATED, truncation=("depth cap",)),
        ),
    )
    assert complete.status is AssertionStatus.PASSED
    assert truncated.status is AssertionStatus.INCONCLUSIVE and truncated.origin == ORIGIN_RUNNER


# --------------------------------------------------------------------------------------
# Artifact assertions
# --------------------------------------------------------------------------------------


def test_run_scoped_artifact_detects_a_stale_export(tmp_path):
    path = tmp_path / "export.json"
    path.write_text(json.dumps({"run_id": "run:" + "d" * 24, "name": "x"}), encoding="utf-8")
    spec = AssertionSpec(
        assertion_id="export-fresh",
        evaluator=Evaluator.ARTIFACT,
        target={"path": str(path), "run_id_field": "run_id"},
        expected={},
        property="run_scoped",
    )
    result = evaluate(spec, context(tmp_path))
    assert result.status is AssertionStatus.FAILED
    assert "not bound to this run" in " ".join(result.notes)


def test_run_scoped_artifact_passes_for_this_run(tmp_path):
    path = tmp_path / "export.json"
    path.write_text(json.dumps({"run_id": RUN_ID}), encoding="utf-8")
    spec = AssertionSpec(
        assertion_id="export-fresh",
        evaluator=Evaluator.ARTIFACT,
        target={"path": str(path)},
        expected={},
        property="run_scoped",
    )
    result = evaluate(spec, context(tmp_path))
    assert result.status is AssertionStatus.PASSED
    assert result.evidence_refs, "the artifact itself becomes evidence"


def test_artifact_outside_approved_roots_is_an_environment_block(tmp_path):
    outside = Path(tmp_path).parent / "not-approved.txt"
    outside.write_text("data", encoding="utf-8")
    spec = AssertionSpec(
        assertion_id="artifact-escape",
        evaluator=Evaluator.ARTIFACT,
        target={"path": str(outside)},
        expected={"is_true": True},
        property="exists",
    )
    result = evaluate(spec, context(tmp_path))
    assert result.status is AssertionStatus.INCONCLUSIVE and result.origin == ORIGIN_ENVIRONMENT
    outside.unlink(missing_ok=True)


# --------------------------------------------------------------------------------------
# Identity and caller-supplied oracles
# --------------------------------------------------------------------------------------


def test_visual_assertion_without_an_oracle_is_never_a_pass(tmp_path):
    spec = AssertionSpec(
        assertion_id="looks-right",
        evaluator=Evaluator.MODEL_VISUAL,
        target={},
        expected={},
    )
    result = evaluate(spec, context(tmp_path))
    assert result.status is AssertionStatus.INCONCLUSIVE
    assert "oracle" in " ".join(result.notes)


def test_caller_result_citing_unknown_evidence_is_rejected(tmp_path):
    spec = AssertionSpec(
        assertion_id="looks-right",
        evaluator=Evaluator.MODEL_VISUAL,
        target={},
        expected={},
        oracle="caller",
    )
    ctx = context(
        tmp_path,
        caller_results={
            "looks-right": {"status": "passed", "evidence_refs": ["ev:" + "f" * 24]},
        },
    )
    result = evaluate(spec, ctx)
    assert result.status is AssertionStatus.INCONCLUSIVE
    assert "unknown evidence" in " ".join(result.notes)


# --------------------------------------------------------------------------------------
# Verdict aggregation
# --------------------------------------------------------------------------------------


def test_verdict_requires_assertions_and_a_verified_build():
    assert (
        aggregate_verdict(
            expected_build_verified=False,
            required={"a"},
            qualified={"a": "passed"},
            execution="completed",
            required_path_completed=True,
            uncertain_effects=False,
        )
        == Verdict.INCONCLUSIVE.value
    )
    assert (
        aggregate_verdict(
            expected_build_verified=True,
            required=set(),
            qualified={},
            execution="completed",
            required_path_completed=True,
            uncertain_effects=False,
        )
        == Verdict.INCONCLUSIVE.value
    )
    assert (
        aggregate_verdict(
            expected_build_verified=True,
            required={"a"},
            qualified={"a": "passed"},
            execution="completed",
            required_path_completed=True,
            uncertain_effects=False,
        )
        == Verdict.PASSED.value
    )


def test_proven_failure_survives_later_cleanup_errors():
    assert (
        aggregate_verdict(
            expected_build_verified=True,
            required={"a", "b"},
            qualified={"a": "failed", "b": "inconclusive"},
            execution="error",
            required_path_completed=False,
            uncertain_effects=True,
        )
        == Verdict.FAILED.value
    )


def test_uncertain_effects_or_skipped_steps_block_a_pass():
    assert (
        aggregate_verdict(
            expected_build_verified=True,
            required={"a"},
            qualified={"a": "passed"},
            execution="completed",
            required_path_completed=True,
            uncertain_effects=True,
        )
        == Verdict.INCONCLUSIVE.value
    )
    assert (
        aggregate_verdict(
            expected_build_verified=True,
            required={"a"},
            qualified={"a": "passed"},
            execution="paused",
            required_path_completed=False,
            uncertain_effects=False,
        )
        == Verdict.INCONCLUSIVE.value
    )


def test_qualified_assertions_downgrade_passes_without_a_verified_build():
    result = AssertionResult(
        assertion_id="a",
        status=AssertionStatus.PASSED,
        evaluator=Evaluator.UIA_PROPERTY,
        origin=ORIGIN_APPLICATION,
        expected={},
        observed={},
        checkpoint="step-1",
        at=now(),
    )
    qualified = qualified_assertions([result], expected_build_verified=False)
    assert qualified == {"a": "inconclusive"}
    qualified_ok = qualified_assertions([result], expected_build_verified=True)
    assert qualified_ok == {"a": "passed"}
