"""Evidence-backed assertions and conservative verdict aggregation.

Only this module may populate assertion outcomes. An entry counts toward a pass only when
it was evaluated at the correct checkpoint against a fresh observation or an artifact bound
to the intended run and build, and when its origin is the application rather than the
runner or the environment. A model-declared completion is never a verdict.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .contracts import (
    AssertionResult,
    AssertionSpec,
    AssertionStatus,
    ContractError,
    Evaluator,
    IdentityReport,
    IdentityStatus,
    Snapshot,
    Verdict,
    now,
)
from .evidence import EvidenceStore, resolve_approved_path, sha256_file

ORIGIN_APPLICATION = "application"
ORIGIN_RUNNER = "runner"
ORIGIN_ENVIRONMENT = "environment"
ORIGIN_UNSPECIFIED = "unspecified"

COMPARATORS = {
    "equals",
    "not_equals",
    "contains",
    "regex",
    "in",
    "is_true",
    "is_false",
    "gte",
    "lte",
    "prefix",
    "suffix",
}


@dataclass
class EvalContext:
    observation: Snapshot | None = None
    identity: IdentityReport | None = None
    evidence: EvidenceStore | None = None
    approved_roots: Sequence[str] = ()
    caller_results: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    run_id: str = ""
    checkpoint: str = ""
    clock: Callable[[], float] = now


# --------------------------------------------------------------------------------------
# Comparators
# --------------------------------------------------------------------------------------


def compare(observed: Any, expected: Mapping[str, Any]) -> tuple[bool, str]:
    """Exactly one comparator key is required; returns (matched, explanation)."""
    keys = [key for key in expected if key in COMPARATORS]
    if len(keys) != 1:
        raise ContractError(f"expected exactly one comparator, found {sorted(keys) or 'none'}")
    comparator = keys[0]
    target = expected[comparator]
    if comparator == "equals":
        return observed == target, f"{observed!r} == {target!r}"
    if comparator == "not_equals":
        return observed != target, f"{observed!r} != {target!r}"
    if comparator == "contains":
        return (observed is not None and str(target) in str(observed)), f"{target!r} in {observed!r}"
    if comparator == "regex":
        matched = observed is not None and re.search(str(target), str(observed)) is not None
        return matched, f"{target!r} matches {observed!r}"
    if comparator == "in":
        return observed in (target or []), f"{observed!r} in {target!r}"
    if comparator == "is_true":
        return observed is True, f"{observed!r} is true"
    if comparator == "is_false":
        return observed is False, f"{observed!r} is false"
    if comparator in {"gte", "lte"}:
        if not isinstance(observed, (int, float)) or isinstance(observed, bool):
            return False, f"{observed!r} is not numeric"
        if comparator == "gte":
            return observed >= target, f"{observed!r} >= {target!r}"
        return observed <= target, f"{observed!r} <= {target!r}"
    if comparator == "prefix":
        return (observed is not None and str(observed).startswith(str(target))), f"{observed!r} starts with {target!r}"
    if comparator == "suffix":
        return (observed is not None and str(observed).endswith(str(target))), f"{observed!r} ends with {target!r}"
    raise ContractError(f"unsupported comparator {comparator}")


# --------------------------------------------------------------------------------------
# Element matching
# --------------------------------------------------------------------------------------


def _element_filter(target: Mapping[str, Any]) -> Callable[[Mapping[str, Any]], bool]:
    role = target.get("role")
    name = target.get("name")
    name_regex = target.get("name_regex")
    value_regex = target.get("value_regex")
    window_ref = target.get("window_ref")
    index = target.get("index")
    pattern = re.compile(str(name_regex)) if name_regex else None
    value_pattern = re.compile(str(value_regex)) if value_regex else None

    def predicate(element: Mapping[str, Any]) -> bool:
        if role is not None and element.get("role") != role:
            return False
        if name is not None and element.get("name") != name:
            return False
        if pattern is not None and not pattern.search(str(element.get("name") or "")):
            return False
        if value_pattern is not None and not value_pattern.search(str(element.get("value") or "")):
            return False
        if window_ref is not None and element.get("window_ref") != window_ref:
            return False
        return index is None or element.get("index") == index

    return predicate


def matching_elements(observation: Snapshot | None, target: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    if observation is None:
        return []
    predicate = _element_filter(target)
    return [element.to_json() for element in observation.elements if predicate(element.to_json())]


def _coverage_supports_absence(observation: Snapshot | None) -> tuple[bool, str]:
    if observation is None:
        return False, "no observation available"
    if observation.coverage.value != "complete":
        return False, f"observation coverage is {observation.coverage.value}"
    if observation.truncation:
        return False, f"observation was truncated: {', '.join(observation.truncation[:3])}"
    return True, ""


# --------------------------------------------------------------------------------------
# Evaluators
# --------------------------------------------------------------------------------------


def _result(
    spec: AssertionSpec,
    status: AssertionStatus,
    *,
    observed: Mapping[str, Any],
    origin: str,
    notes: Sequence[str] = (),
    evidence_refs: Sequence[str] = (),
    label: str = "deterministic",
    checkpoint: str,
    at: float,
) -> AssertionResult:
    return AssertionResult(
        assertion_id=spec.assertion_id,
        status=status,
        evaluator=spec.evaluator,
        origin=origin,
        expected=dict(spec.expected),
        observed=dict(observed),
        checkpoint=checkpoint,
        at=at,
        evidence_refs=tuple(evidence_refs),
        notes=tuple(notes),
        label=label,
    )


def evaluate(spec: AssertionSpec, context: EvalContext) -> AssertionResult:
    at = context.clock()
    observation = context.observation
    target = dict(spec.target or {})
    expected = dict(spec.expected or {})

    if spec.evaluator in {Evaluator.UIA_PRESENCE, Evaluator.UIA_ABSENCE}:
        matches = matching_elements(observation, target)
        if spec.evaluator is Evaluator.UIA_PRESENCE:
            status = AssertionStatus.PASSED if matches else AssertionStatus.FAILED
            origin = ORIGIN_APPLICATION
            notes = [] if matches else ["no observed element matched the target"]
            if not matches:
                ok, why = _coverage_supports_absence(observation)
                if not ok:
                    status, origin = AssertionStatus.INCONCLUSIVE, ORIGIN_RUNNER
                    notes = [f"absence of a match cannot be interpreted: {why}"]
            return _result(
                spec,
                status,
                observed={"matches": [m["element_id"] for m in matches], "count": len(matches)},
                origin=origin,
                notes=notes,
                checkpoint=context.checkpoint,
                at=at,
            )
        if matches:
            return _result(
                spec,
                AssertionStatus.FAILED,
                observed={"count": len(matches), "matches": [m["element_id"] for m in matches]},
                origin=ORIGIN_APPLICATION,
                notes=["element is present"],
                checkpoint=context.checkpoint,
                at=at,
            )
        ok, why = _coverage_supports_absence(observation)
        if not ok:
            return _result(
                spec,
                AssertionStatus.INCONCLUSIVE,
                observed={"count": 0},
                origin=ORIGIN_RUNNER,
                notes=[f"truncated observation cannot establish absence: {why}"],
                checkpoint=context.checkpoint,
                at=at,
            )
        return _result(
            spec,
            AssertionStatus.PASSED,
            observed={"count": 0},
            origin=ORIGIN_APPLICATION,
            checkpoint=context.checkpoint,
            at=at,
        )

    if spec.evaluator is Evaluator.UIA_PROPERTY:
        matches = matching_elements(observation, target)
        if not matches:
            return _result(
                spec,
                AssertionStatus.FAILED,
                observed={"count": 0},
                origin=ORIGIN_APPLICATION,
                notes=["no observed element matched the target"],
                checkpoint=context.checkpoint,
                at=at,
            )
        prop = spec.property or "value"
        if prop == "count":
            observed_value: Any = len(matches)
        else:
            observed_value = _dig(matches[0], prop)
        try:
            matched, explanation = compare(observed_value, expected)
        except ContractError as exc:
            return _result(
                spec,
                AssertionStatus.INCONCLUSIVE,
                observed={"value": observed_value},
                origin=ORIGIN_RUNNER,
                notes=[str(exc)],
                checkpoint=context.checkpoint,
                at=at,
            )
        return _result(
            spec,
            AssertionStatus.PASSED if matched else AssertionStatus.FAILED,
            observed={"value": observed_value, "element_id": matches[0]["element_id"], "count": len(matches)},
            origin=ORIGIN_APPLICATION,
            notes=[explanation, f"property={prop}"],
            checkpoint=context.checkpoint,
            at=at,
        )

    if spec.evaluator is Evaluator.WINDOW_STATE:
        if observation is None:
            return _result(
                spec,
                AssertionStatus.INCONCLUSIVE,
                observed={},
                origin=ORIGIN_RUNNER,
                notes=["no observation available"],
                checkpoint=context.checkpoint,
                at=at,
            )
        windows = list(observation.windows)
        window_ref = target.get("window_ref")
        title_regex = target.get("title_regex")
        pattern = re.compile(str(title_regex)) if title_regex else None
        selected = [
            window
            for window in windows
            if (window_ref is None or window.window_ref == window_ref)
            and (pattern is None or pattern.search(window.title))
        ]
        prop = spec.property or "exists"
        if prop == "exists":
            observed_value = bool(selected)
        elif not selected:
            observed_value = None
        else:
            observed_value = {
                "visible": selected[0].visible,
                "enabled": selected[0].enabled,
                "modal": selected[0].modal,
                "focused": selected[0].focused,
            }.get(prop)
        try:
            matched, explanation = compare(observed_value, expected)
        except ContractError as exc:
            return _result(
                spec,
                AssertionStatus.INCONCLUSIVE,
                observed={"value": observed_value},
                origin=ORIGIN_RUNNER,
                notes=[str(exc)],
                checkpoint=context.checkpoint,
                at=at,
            )
        return _result(
            spec,
            AssertionStatus.PASSED if matched else AssertionStatus.FAILED,
            observed={"value": observed_value, "windows": len(selected)},
            origin=ORIGIN_APPLICATION,
            notes=[explanation, f"property={prop}"],
            checkpoint=context.checkpoint,
            at=at,
        )

    if spec.evaluator is Evaluator.ARTIFACT:
        return _evaluate_artifact(spec, context, target, expected, at)

    if spec.evaluator is Evaluator.PROCESS_IDENTITY:
        report = context.identity
        if report is None:
            return _result(
                spec,
                AssertionStatus.INCONCLUSIVE,
                observed={},
                origin=ORIGIN_RUNNER,
                notes=["no identity report was produced"],
                checkpoint=context.checkpoint,
                at=at,
            )
        observed = {"status": report.status.value, **dict(report.observed)}
        expectation = dict(expected)
        if not expectation:
            expectation = {"equals": IdentityStatus.VERIFIED.value}
        observed_value = observed.get(spec.property) if spec.property else observed["status"]
        try:
            matched, explanation = compare(observed_value, expectation)
        except ContractError as exc:
            return _result(
                spec,
                AssertionStatus.INCONCLUSIVE,
                observed=observed,
                origin=ORIGIN_RUNNER,
                notes=[str(exc)],
                checkpoint=context.checkpoint,
                at=at,
            )
        status = AssertionStatus.PASSED if matched else AssertionStatus.FAILED
        origin = ORIGIN_APPLICATION
        notes = [explanation, f"identity={report.status.value}"]
        if report.status is IdentityStatus.UNVERIFIABLE:
            status, origin = AssertionStatus.INCONCLUSIVE, ORIGIN_ENVIRONMENT
            notes.append("the running build could not be verified")
        return _result(
            spec,
            status,
            observed=observed,
            origin=origin,
            notes=notes,
            evidence_refs=report.evidence_refs,
            checkpoint=context.checkpoint,
            at=at,
        )

    if spec.evaluator in {Evaluator.MODEL_VISUAL, Evaluator.CALLER_RESULT}:
        if spec.evaluator is Evaluator.MODEL_VISUAL and not spec.oracle:
            return _result(
                spec,
                AssertionStatus.INCONCLUSIVE,
                observed={},
                origin=ORIGIN_RUNNER,
                notes=["visual assertion has no explicitly selected oracle"],
                checkpoint=context.checkpoint,
                at=at,
            )
        supplied = context.caller_results.get(spec.assertion_id)
        if supplied is None:
            return _result(
                spec,
                AssertionStatus.INCONCLUSIVE,
                observed={},
                origin=ORIGIN_RUNNER,
                notes=["awaiting an explicitly selected evaluator result"],
                checkpoint=context.checkpoint,
                at=at,
            )
        return _caller_result(spec, context, supplied, at)

    return _result(
        spec,
        AssertionStatus.INCONCLUSIVE,
        observed={},
        origin=ORIGIN_RUNNER,
        notes=[f"evaluator {spec.evaluator.value} is not implemented"],
        checkpoint=context.checkpoint,
        at=at,
    )


def _caller_result(
    spec: AssertionSpec, context: EvalContext, supplied: Mapping[str, Any], at: float
) -> AssertionResult:
    raw_status = supplied.get("status")
    try:
        status = AssertionStatus(str(raw_status))
    except ValueError:
        return _result(
            spec,
            AssertionStatus.INCONCLUSIVE,
            observed=dict(supplied),
            origin=ORIGIN_RUNNER,
            notes=["caller result used an unknown status"],
            checkpoint=context.checkpoint,
            at=at,
        )
    refs = [str(ref) for ref in supplied.get("evidence_refs", [])]
    if context.evidence is not None:
        for ref in refs:
            try:
                context.evidence.get(ref)
            except ContractError:
                return _result(
                    spec,
                    AssertionStatus.INCONCLUSIVE,
                    observed=dict(supplied),
                    origin=ORIGIN_RUNNER,
                    notes=[f"caller result cites unknown evidence {ref}"],
                    checkpoint=context.checkpoint,
                    at=at,
                )
    label = "model_assessed" if spec.evaluator is Evaluator.MODEL_VISUAL else "caller_supplied"
    if status is AssertionStatus.INCONCLUSIVE and supplied.get("origin"):
        origin = str(supplied["origin"])
    elif status is AssertionStatus.INCONCLUSIVE:
        origin = ORIGIN_RUNNER
    else:
        origin = ORIGIN_APPLICATION
    return _result(
        spec,
        status,
        observed=dict(supplied.get("observed") or {}),
        origin=origin,
        notes=[str(supplied.get("note", "supplied by an explicitly selected evaluator"))],
        evidence_refs=refs,
        label=label,
        checkpoint=context.checkpoint,
        at=at,
    )


def _evaluate_artifact(
    spec: AssertionSpec, context: EvalContext, target: Mapping[str, Any], expected: Mapping[str, Any], at: float
) -> AssertionResult:
    raw_path = target.get("path")
    if not raw_path:
        return _result(
            spec,
            AssertionStatus.INCONCLUSIVE,
            observed={},
            origin=ORIGIN_RUNNER,
            notes=["artifact assertion has no path"],
            checkpoint=context.checkpoint,
            at=at,
        )
    roots = list(context.approved_roots) or (list(context.evidence.approved_roots) if context.evidence else [])
    try:
        path = resolve_approved_path(str(raw_path), roots)
    except ContractError as exc:
        return _result(
            spec,
            AssertionStatus.INCONCLUSIVE,
            observed={"path": str(raw_path)},
            origin=ORIGIN_ENVIRONMENT,
            notes=[str(exc)],
            checkpoint=context.checkpoint,
            at=at,
        )
    prop = spec.property or "exists"
    if prop == "exists":
        exists = os.path.isfile(path)
        try:
            matched, explanation = compare(exists, expected if expected else {"is_true": True})
        except ContractError as exc:
            return _result(
                spec,
                AssertionStatus.INCONCLUSIVE,
                observed={"exists": exists},
                origin=ORIGIN_RUNNER,
                notes=[str(exc)],
                checkpoint=context.checkpoint,
                at=at,
            )
        return _result(
            spec,
            AssertionStatus.PASSED if matched else AssertionStatus.FAILED,
            observed={"exists": exists, "path": path},
            origin=ORIGIN_APPLICATION,
            notes=[explanation],
            checkpoint=context.checkpoint,
            at=at,
        )
    if not os.path.isfile(path):
        return _result(
            spec,
            AssertionStatus.FAILED,
            observed={"exists": False, "path": path},
            origin=ORIGIN_APPLICATION,
            notes=["artifact is missing"],
            checkpoint=context.checkpoint,
            at=at,
        )

    observed: dict[str, Any] = {"path": path, "size_bytes": os.path.getsize(path), "sha256": sha256_file(path)}
    if prop in {"mtime_after", "run_scoped"}:
        observed["mtime"] = os.path.getmtime(path)
    if prop in {"json_path", "run_scoped"}:
        try:
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError) as exc:
            return _result(
                spec,
                AssertionStatus.INCONCLUSIVE,
                observed=observed,
                origin=ORIGIN_RUNNER,
                notes=[f"artifact is not readable JSON: {exc}"],
                checkpoint=context.checkpoint,
                at=at,
            )
        observed["json"] = payload
    if prop == "content_regex":
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                observed["content_head"] = handle.read(65536)
        except OSError as exc:
            return _result(
                spec,
                AssertionStatus.INCONCLUSIVE,
                observed=observed,
                origin=ORIGIN_RUNNER,
                notes=[f"artifact could not be read: {exc}"],
                checkpoint=context.checkpoint,
                at=at,
            )

    if prop == "sha256":
        try:
            matched, explanation = compare(observed["sha256"], expected)
        except ContractError as exc:
            return _result(
                spec,
                AssertionStatus.INCONCLUSIVE,
                observed=observed,
                origin=ORIGIN_RUNNER,
                notes=[str(exc)],
                checkpoint=context.checkpoint,
                at=at,
            )
        return _result(
            spec,
            AssertionStatus.PASSED if matched else AssertionStatus.FAILED,
            observed=observed,
            origin=ORIGIN_APPLICATION,
            notes=[explanation],
            checkpoint=context.checkpoint,
            at=at,
        )

    if prop == "size_at_least":
        minimum = expected.get("bytes", expected.get("gte"))
        matched = observed["size_bytes"] >= int(minimum or 0)
        return _result(
            spec,
            AssertionStatus.PASSED if matched else AssertionStatus.FAILED,
            observed=observed,
            origin=ORIGIN_APPLICATION,
            notes=[f"size {observed['size_bytes']} >= {minimum}"],
            evidence_refs=_artifact_evidence(context, spec, observed),
            checkpoint=context.checkpoint,
            at=at,
        )

    if prop == "mtime_after":
        threshold = float(expected.get("after", 0.0))
        matched = observed.get("mtime", 0.0) >= threshold
        return _result(
            spec,
            AssertionStatus.PASSED if matched else AssertionStatus.FAILED,
            observed=observed,
            origin=ORIGIN_APPLICATION,
            notes=[f"mtime {observed.get('mtime')} >= {threshold}"],
            evidence_refs=_artifact_evidence(context, spec, observed),
            checkpoint=context.checkpoint,
            at=at,
        )

    if prop in {"json_path", "run_scoped"}:
        if prop == "run_scoped":
            field = str(target.get("run_id_field", "run_id"))
            observed_value = _json_path(observed.get("json"), field)
            matched = observed_value == context.run_id
            explanation = f"{field}={observed_value!r} equals run id"
            if not matched:
                return _result(
                    spec,
                    AssertionStatus.FAILED,
                    observed={**observed, "value": observed_value},
                    origin=ORIGIN_APPLICATION,
                    notes=[f"artifact is not bound to this run ({explanation})"],
                    evidence_refs=_artifact_evidence(context, spec, observed),
                    checkpoint=context.checkpoint,
                    at=at,
                )
            return _result(
                spec,
                AssertionStatus.PASSED,
                observed={**observed, "value": observed_value},
                origin=ORIGIN_APPLICATION,
                notes=[explanation],
                evidence_refs=_artifact_evidence(context, spec, observed),
                checkpoint=context.checkpoint,
                at=at,
            )
        field = str(target.get("json_field", spec.property or ""))
        observed_value = _json_path(observed.get("json"), field)
        return _result(
            spec,
            AssertionStatus.PASSED if _safe_compare(observed_value, expected) else AssertionStatus.FAILED,
            observed={**observed, "value": observed_value},
            origin=ORIGIN_APPLICATION,
            notes=[f"json field {field} = {observed_value!r}"],
            evidence_refs=_artifact_evidence(context, spec, observed),
            checkpoint=context.checkpoint,
            at=at,
        )

    if prop == "content_regex":
        observed_value = observed.get("content_head") or ""
        matched = re.search(str(target.get("content_regex") or expected.get("regex") or ""), observed_value) is not None
        return _result(
            spec,
            AssertionStatus.PASSED if matched else AssertionStatus.FAILED,
            observed={"path": path, "size_bytes": observed["size_bytes"]},
            origin=ORIGIN_APPLICATION,
            notes=["content regex matched" if matched else "content regex missed"],
            evidence_refs=_artifact_evidence(context, spec, observed),
            checkpoint=context.checkpoint,
            at=at,
        )

    return _result(
        spec,
        AssertionStatus.INCONCLUSIVE,
        observed=observed,
        origin=ORIGIN_RUNNER,
        notes=[f"artifact property {prop!r} is not implemented"],
        checkpoint=context.checkpoint,
        at=at,
    )


def _safe_compare(observed_value: Any, expected: Mapping[str, Any]) -> bool:
    try:
        matched, _ = compare(observed_value, expected)
    except ContractError:
        return False
    return matched


def _artifact_evidence(context: EvalContext, spec: AssertionSpec, observed: Mapping[str, Any]) -> tuple[str, ...]:
    """Register the artifact itself as evidence so a failure carries its bytes and hash."""
    if context.evidence is None:
        return ()
    path = str(observed.get("path") or "")
    if not os.path.isfile(path):
        return ()
    try:
        with open(path, "rb") as handle:
            data = handle.read(8 * 1024 * 1024)
    except OSError:
        return ()
    reference = context.evidence.save_bytes(
        run_id=context.run_id,
        checkpoint=spec.checkpoint,
        description=f"artifact for assertion {spec.assertion_id}",
        data=data,
        kind="artifact",
        media_type="application/octet-stream",
        suffix=".copy",
        keep=True,
    )
    return (reference.evidence_id,)


def _dig(payload: Any, path: str) -> Any:
    """Read a dotted path from an observed element: `value`, `state.checked`, `rect.width`."""
    current = payload
    for part in path.split("."):
        if isinstance(current, Mapping):
            current = current.get(part)
        else:
            return None
    return current


def _json_path(payload: Any, path: str) -> Any:
    if not path:
        return None
    current = payload
    for part in path.split("."):
        if isinstance(current, Mapping):
            current = current.get(part)
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
        if current is None:
            return None
    return current


# --------------------------------------------------------------------------------------
# Verdict aggregation
# --------------------------------------------------------------------------------------


def aggregate_verdict(
    *,
    expected_build_verified: bool,
    required: set[str],
    qualified: Mapping[str, str],
    execution: str,
    required_path_completed: bool,
    uncertain_effects: bool,
) -> str:
    """Conservative aggregation: no assertions means no pass; a proven failure survives."""
    if not expected_build_verified:
        return Verdict.INCONCLUSIVE.value

    if any(qualified.get(key) == AssertionStatus.FAILED.value for key in required):
        return Verdict.FAILED.value

    if (
        required
        and execution == "completed"
        and required_path_completed
        and not uncertain_effects
        and all(qualified.get(key) == AssertionStatus.PASSED.value for key in required)
    ):
        return Verdict.PASSED.value

    return Verdict.INCONCLUSIVE.value


def qualified_assertions(results: Sequence[AssertionResult], *, expected_build_verified: bool) -> dict[str, str]:
    """Only fresh, correctly checkpointed, application-origin results may qualify."""
    qualified: dict[str, str] = {}
    for result in results:
        if result.status is AssertionStatus.NOT_EVALUATED:
            continue
        if result.status is AssertionStatus.INCONCLUSIVE and result.origin != ORIGIN_APPLICATION:
            qualified[result.assertion_id] = AssertionStatus.INCONCLUSIVE.value
            continue
        if not expected_build_verified and result.status is AssertionStatus.PASSED:
            qualified[result.assertion_id] = AssertionStatus.INCONCLUSIVE.value
            continue
        qualified[result.assertion_id] = result.status.value
    return qualified
