"""Bounded test runtime: immutable specification, checkpoints, budgets, resumability.

The runtime owns step ordering, pause/resume, retry bounds, assertion scheduling, and
evidence capture. It never lets model output, UI content, or a transport retry widen the
test: the specification and its digest are frozen at run creation, slice deadlines return a
resumable checkpoint before any client tool deadline, and a resuming caller may only supply
fixture values, scoped visual assistance, or an explicitly requested verifier result.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .contracts import (
    ActionRequest,
    AssertionResult,
    AssertionSpec,
    AssertionStatus,
    ContractError,
    Decision,
    DispatchMechanism,
    DispatchState,
    DriverError,
    EmergencyStop,
    Evaluator,
    EvidenceRef,
    Execution,
    InputMode,
    Limits,
    Operation,
    Pause,
    Purpose,
    Reason,
    Receipt,
    RunResult,
    RunSpec,
    RunStatus,
    Snapshot,
    StepRecord,
    TargetCandidate,
    UncertainEffect,
    Verdict,
    digest,
    keyed_fingerprint,
    new_id,
    now,
    validate_id,
)
from .evidence import EvidenceStore
from .journal import DispatchJournal, JournalUnhealthy
from .ownership import Lease, Ownership
from .policy import (
    STATE_BUDGET_BYTES,
    JevPolicy,
    OpContext,
    PolicyError,
    build_contexts,
    fit_state_to_budget,
    summarize_state_for_policy,
    with_permitted_operations,
)
from .verification import (
    ORIGIN_APPLICATION,
    EvalContext,
    aggregate_verdict,
    evaluate,
    qualified_assertions,
)

RESERVED_DISPATCH_STATES = {DispatchState.DISPATCHING, DispatchState.UNCERTAIN}
CONTROL_OPERATIONS = {Operation.HOTKEY, Operation.LAUNCH_APP}


@dataclass
class RuntimeConfig:
    evidence_dir: Path
    approved_roots: tuple[str, ...] = ()
    secret_provider: Callable[[str], str | None] | None = None
    launch_configs: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    capture_checkpoints: bool = True
    capture_failures: bool = True
    capture_scale: float = 0.6
    # Measured time from dispatch to an observable change: 179 ms to 365 ms across click,
    # typing, toggle, and dialog actions, with no misses. 0.8 s is roughly twice the p99.
    settle_seconds: float = 0.8
    # Provider input ceilings are tokenizer dependent, so start conservative and adapt on a
    # refusal instead of trusting one machine's measurement.
    state_budget_bytes: int = STATE_BUDGET_BYTES
    min_state_budget_bytes: int = 6_000
    state_budget_shrink: float = 0.6
    fingerprint_secret: bytes = field(default_factory=lambda: os.urandom(32))
    sleeper: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = now

    def resolve_secret(self, name: str) -> str | None:
        if self.secret_provider is not None:
            value = self.secret_provider(name)
            if value:
                return value
        return os.environ.get(name)


@dataclass
class ResumeInputs:
    """Everything a resuming caller may supply. Nothing here can rewrite the test."""

    fixtures: Mapping[str, str] = field(default_factory=dict)
    visual_results: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    verifier_results: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)


@dataclass
class _RunState:
    run_id: str
    spec: RunSpec
    spec_digest: str
    status: str
    resume_token: str
    steps: list[StepRecord] = field(default_factory=list)
    assertions: list[AssertionResult] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    actions: int = 0
    decisions: int = 0
    slices: int = 0
    started_at: float = 0.0
    identity_checked: bool = False
    identity_verified: bool = False
    identity_status: str | None = None
    current_step_id: str | None = None
    completed_steps: list[str] = field(default_factory=list)
    last_fingerprint: str | None = None
    no_progress: int = 0
    stale_retries: int = 0
    pause_reason: str | None = None
    pause_detail: dict[str, Any] = field(default_factory=dict)
    model_versions: list[str] = field(default_factory=list)
    supplied_fixtures: dict[str, str] = field(default_factory=dict)
    state_budget_bytes: int | None = None
    supplied_visual: dict[str, Mapping[str, Any]] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)
    pending_assertion: str | None = None
    uncertain: bool = False

    # -- persistence ---------------------------------------------------------------

    def to_json(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "spec": self.spec.to_json(),
            "spec_digest": self.spec_digest,
            "status": self.status,
            "resume_token": self.resume_token,
            "steps": [step.to_json() for step in self.steps],
            "assertions": [result.to_json() for result in self.assertions],
            "evidence_ids": list(self.evidence_ids),
            "actions": self.actions,
            "decisions": self.decisions,
            "slices": self.slices,
            "started_at": self.started_at,
            "identity_checked": self.identity_checked,
            "identity_verified": self.identity_verified,
            "identity_status": self.identity_status,
            "current_step_id": self.current_step_id,
            "completed_steps": list(self.completed_steps),
            "last_fingerprint": self.last_fingerprint,
            "no_progress": self.no_progress,
            "stale_retries": self.stale_retries,
            "pause_reason": self.pause_reason,
            "pause_detail": dict(self.pause_detail),
            "model_versions": list(self.model_versions),
            "supplied_fixtures": dict(self.supplied_fixtures),
            "state_budget_bytes": self.state_budget_bytes,
            "supplied_visual": {key: dict(value) for key, value in self.supplied_visual.items()},
            "summary": dict(self.summary),
            "pending_assertion": self.pending_assertion,
            "uncertain": self.uncertain,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> _RunState:
        from .contracts import assertion_result_from_json, step_record_from_json

        spec = RunSpec.from_json(data["spec"])
        state = cls(
            run_id=validate_id("run", data["run_id"]),
            spec=spec,
            spec_digest=str(data["spec_digest"]),
            status=str(data["status"]),
            resume_token=str(data["resume_token"]),
            started_at=float(data.get("started_at", 0.0)),
        )
        state.steps = [step_record_from_json(item) for item in data.get("steps", [])]
        state.assertions = [assertion_result_from_json(item) for item in data.get("assertions", [])]
        state.evidence_ids = [str(item) for item in data.get("evidence_ids", [])]
        state.actions = int(data.get("actions", 0))
        state.decisions = int(data.get("decisions", 0))
        state.slices = int(data.get("slices", 0))
        state.identity_checked = bool(data.get("identity_checked", False))
        state.identity_verified = bool(data.get("identity_verified", False))
        state.identity_status = data.get("identity_status")
        state.current_step_id = data.get("current_step_id")
        state.completed_steps = [str(item) for item in data.get("completed_steps", [])]
        state.last_fingerprint = data.get("last_fingerprint")
        state.no_progress = int(data.get("no_progress", 0))
        state.stale_retries = int(data.get("stale_retries", 0))
        state.pause_reason = data.get("pause_reason")
        state.pause_detail = dict(data.get("pause_detail", {}))
        state.model_versions = [str(item) for item in data.get("model_versions", [])]
        state.supplied_fixtures = {str(k): str(v) for k, v in data.get("supplied_fixtures", {}).items()}
        budget = data.get("state_budget_bytes")
        state.state_budget_bytes = None if budget is None else int(budget)
        state.supplied_visual = {str(k): dict(v) for k, v in data.get("supplied_visual", {}).items()}
        state.summary = dict(data.get("summary", {}))
        state.pending_assertion = data.get("pending_assertion")
        state.uncertain = bool(data.get("uncertain", False))
        return state


class Runtime:
    def __init__(
        self,
        *,
        driver: Any,
        journal: DispatchJournal,
        ownership: Ownership,
        evidence: EvidenceStore,
        config: RuntimeConfig,
        policy: JevPolicy | None = None,
    ) -> None:
        self.driver = driver
        self.journal = journal
        self.ownership = ownership
        self.evidence = evidence
        self.config = config
        self.policy = policy
        self._state: dict[str, _RunState] = {}

    # ------------------------------------------------------------------------------
    # Run lifecycle
    # ------------------------------------------------------------------------------

    def create_run(self, spec: RunSpec, *, session_id: str) -> dict[str, Any]:
        self.ownership.authorize(session_id)
        if not spec.steps and not spec.assertions:
            raise ContractError("a run needs at least one required step or assertion")
        self._validate_spec(spec)
        run_id = new_id("run")
        state = _RunState(
            run_id=run_id,
            spec=spec,
            spec_digest=spec.frozen_digest(),
            status=RunStatus.CREATED.value,
            resume_token=new_id("resume"),
            started_at=self.config.clock(),
        )
        self.journal.put_run(
            run_id=run_id,
            spec_digest=state.spec_digest,
            spec_json=json.dumps(spec.to_json(), ensure_ascii=False),
            status=state.status,
            state_json=json.dumps(state.to_json(), ensure_ascii=False),
            resume_token=state.resume_token,
        )
        self.journal.append_trace(
            run_id, "run_created", {"spec_digest": state.spec_digest, "limits": spec.limits.to_json()}
        )
        self._state[run_id] = state
        return {"run_id": run_id, "resume_token": state.resume_token, "spec_digest": state.spec_digest}

    def slice(
        self,
        *,
        run_id: str,
        session_id: str,
        resume_token: str,
        inputs: ResumeInputs | None = None,
        slice_seconds: float | None = None,
    ) -> RunResult:
        self.ownership.authorize(session_id)
        state = self._load(run_id)
        if resume_token != state.resume_token:
            raise ContractError("resume token does not match the current run checkpoint")
        lease = self.ownership.active_lease()
        if lease is None or lease.run_id != run_id or lease.session_id != session_id:
            raise ContractError("this run does not hold the desktop lease")
        if inputs is not None:
            self._apply_inputs(state, inputs)
        self.journal.require_healthy()

        state.slices += 1
        state.status = RunStatus.RUNNING.value
        requested = state.spec.limits.slice_seconds if slice_seconds is None else slice_seconds
        deadline = self.config.clock() + requested
        run_deadline = state.started_at + state.spec.limits.deadline_seconds
        deadline = min(deadline, run_deadline)
        self.journal.append_trace(
            run_id, "slice_start", {"slice": state.slices, "deadline_s": deadline - self.config.clock()}
        )
        try:
            self._reconcile_unfinished(state)
            result = self._loop(state, lease, deadline)
        except Pause as pause:
            result = self._pause(state, pause.reason_value, pause.detail)
        except EmergencyStop as stop:
            result = self._stopped(state, Reason.PERMISSION_BOUNDARY.value, str(stop))
        except JournalUnhealthy:
            raise
        except UncertainEffect as exc:
            state.uncertain = True
            result = self._pause(state, Reason.UNCERTAIN_EFFECT.value, {"detail": str(exc)})
        except (DriverError, PolicyError, ContractError) as exc:
            result = self._error(state, exc)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:  # unexpected: never leave the run silently unrecorded
            result = self._error(state, exc)
        self._finish_slice(state, result)
        # The token is rotated at the end of every slice, so the result carries the live one.
        return replace(result, resume_token=state.resume_token)

    def stop(self, *, run_id: str, session_id: str, reason: str = "caller cancelled") -> dict[str, Any]:
        self.ownership.authorize(session_id)
        state = self._load(run_id)
        self.ownership.request_cancel(run_id, reason)
        state.status = RunStatus.CANCELLED.value
        lease = self.ownership.active_lease()
        if lease is not None and lease.run_id == run_id:
            self.ownership.release(lease.lease_id)
        self.journal.append_trace(run_id, "stop", {"reason": reason})
        self._persist(state)
        return {"run_id": run_id, "status": state.status, "reason": reason}

    def status(self, run_id: str) -> dict[str, Any]:
        state = self._load(run_id)
        flags = self.ownership.flags(run_id)
        return {
            "run_id": run_id,
            "status": state.status,
            "spec_digest": state.spec_digest,
            "steps": len(state.steps),
            "completed_steps": list(state.completed_steps),
            "assertions": [result.to_json() for result in state.assertions],
            "budgets": self._budget_view(state),
            "cancelled": flags.cancelled,
            "takeover": flags.takeover,
            "pause_reason": state.pause_reason,
            "pending_assertion": state.pending_assertion,
            "identity": {
                "checked": state.identity_checked,
                "verified": state.identity_verified,
                "status": state.identity_status,
            },
        }

    def act(
        self,
        *,
        run_id: str,
        session_id: str,
        resume_token: str,
        request: ActionRequest,
    ) -> dict[str, Any]:
        """Caller-directed single action through the same authorization/journal path."""
        self.ownership.authorize(session_id)
        state = self._load(run_id)
        if resume_token != state.resume_token:
            raise ContractError("resume token does not match the current run checkpoint")
        lease = self.ownership.active_lease()
        if lease is None or lease.run_id != run_id or lease.session_id != session_id:
            raise ContractError("this run does not hold the desktop lease")
        if state.spec.interaction_mode is InputMode.USER_PATH and request.mode is InputMode.SEMANTIC:
            raise ContractError("this specification is bound to the user_path interaction mode")
        snapshot = None
        if request.element_id:
            snapshot = self._observe(state)
        guarded = replace(
            request, run_id=run_id, lease_generation=lease.generation, request_hash=self._request_hash(state, request)
        )

        def guard() -> None:
            self.ownership.checkpoint(
                run_id=run_id, lease_id=lease.lease_id, generation=lease.generation, session_id=session_id
            )

        receipt = self.journal.dispatch_once(
            action_id=guarded.action_id,
            request_hash=guarded.request_hash,
            run_id=run_id,
            guard=guard,
            send=lambda: self.driver.execute(guarded, guard, snapshot),
        )
        self.journal.append_trace(run_id, "desktop_act", {"action_id": guarded.action_id, "receipt": receipt.to_json()})
        observation = self._observe(state)
        state.last_fingerprint = observation.fingerprint
        self._persist(state)
        return {
            "run_id": run_id,
            "receipt": receipt.to_json(),
            "observation": self._observation_summary(observation),
            "resume_token": state.resume_token,
        }

    # ------------------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------------------

    def _loop(self, state: _RunState, lease: Lease, deadline: float) -> RunResult:
        spec = state.spec
        if self.ownership.emergency_active():
            return self._stopped(state, Reason.PERMISSION_BOUNDARY.value, "local emergency stop is set")

        while True:
            if self.ownership.flags(state.run_id).cancelled:
                return self._stopped(state, Reason.USER_TAKEOVER.value, "run cancelled")
            if self.config.clock() >= deadline:
                return self._pause(state, Reason.BUDGET_EXHAUSTED.value, {"budget": "slice_deadline"})
            if state.actions >= spec.limits.max_actions:
                return self._pause(state, Reason.BUDGET_EXHAUSTED.value, {"budget": "max_actions"})
            if state.decisions >= spec.limits.max_model_decisions:
                return self._pause(state, Reason.BUDGET_EXHAUSTED.value, {"budget": "max_model_decisions"})

            snapshot = self._observe(state)
            self._check_identity(state, snapshot)
            if state.identity_checked and not state.identity_verified:
                return self._blocked(
                    state, Reason.INCORRECT_BUILD.value, "the running build is not the expected build", snapshot
                )
            self._evaluate_due(state, snapshot, checkpoint="run_start" if not state.steps else None)

            step = self._current_step(state)
            if step is None:
                return self._complete(state, snapshot)

            pending = self._pending_assertion_spec(state)
            if pending is not None:
                return self._visual_boundary(state, snapshot, pending)

            # Do not spend a model decision on a step the caller has not equipped yet.
            if (
                step.fixture_reference
                and step.operation in {Operation.TYPE_TEXT, Operation.SELECT, Operation.HOTKEY}
                and self._fixture_value(state, step.fixture_reference) is None
            ):
                return self._pause(
                    state,
                    Reason.NEEDS_TEXT.value,
                    {"step": step.step_id, "fixture": step.fixture_reference},
                )

            contexts = self._build_contexts(state, step, snapshot)
            if not contexts:
                if step.operation in CONTROL_OPERATIONS:
                    return self._pause(
                        state,
                        Reason.PERMISSION_BOUNDARY.value,
                        {
                            "step": step.step_id,
                            "operation": step.operation.value,
                            "detail": "no approved configuration or chord is available for this step",
                        },
                    )
                return self._unsupported(state, snapshot, step)

            decision = self._decide(state, snapshot, step, contexts)
            state.decisions += 1

            if decision.operation is Operation.WAIT:
                self.journal.append_trace(state.run_id, "decision", decision.to_json())
                self.config.sleeper(min(1.5, max(0.2, deadline - self.config.clock())))
                continue
            if decision.operation is Operation.ESCALATE:
                return self._pause(
                    state,
                    Reason.STEP_UNRESOLVED.value,
                    {"step": step.step_id, "decision": decision.to_json()},
                )
            if decision.operation is Operation.DONE:
                return self._requested_done(state, snapshot)

            outcome = self._dispatch(state, lease, decision, snapshot, step)
            if isinstance(outcome, RunResult):
                return outcome
            self._evaluate_due(state, outcome, checkpoint=step.step_id)
            if step.checkpoint and self.config.capture_checkpoints:
                self._capture(state, outcome, checkpoint=step.step_id, description=f"checkpoint after {step.step_id}")

    # ------------------------------------------------------------------------------
    # Observation, identity, decisions
    # ------------------------------------------------------------------------------

    def _observe_settled(self, state: _RunState, *, before: str) -> Snapshot:
        """Re-observe after an action, allowing the application a bounded moment to react.

        Input insertion is not application processing, so the first observation after a
        dispatch can legitimately predate the effect; this waits for an observable change
        (or the settle budget) before deciding whether progress was made.
        """
        latest = self._observe(state)
        deadline = self.config.clock() + self.config.settle_seconds
        while latest.fingerprint == before and self.config.clock() < deadline:
            self.config.sleeper(0.12)
            latest = self._observe(state)
        return latest

    def _reconcile_unfinished(self, state: _RunState) -> None:
        """Recover an interrupted action by observation, never by replaying it."""
        unfinished = [
            record for record in self.journal.actions_for_run(state.run_id) if record.state in RESERVED_DISPATCH_STATES
        ]
        if not unfinished:
            return
        snapshot = self._observe(state)
        for record in unfinished:
            before = self._fingerprint_before(state, record.action_id)
            changed = bool(before) and snapshot.fingerprint != before
            if changed:
                self.journal.reconcile(
                    record.action_id,
                    DispatchState.DISPATCHED,
                    "observable state changed since the dispatch intent; effect assumed landed",
                )
            else:
                self.journal.reconcile(
                    record.action_id,
                    DispatchState.NOT_DISPATCHED,
                    "observable state is identical to the pre-dispatch observation",
                )
            state.uncertain = True  # outcome without a native receipt is never a pass
            self.journal.append_trace(
                state.run_id,
                "reconciled",
                {
                    "action_id": record.action_id,
                    "fingerprint_before": before,
                    "fingerprint_now": snapshot.fingerprint,
                    "observed_change": changed,
                },
            )

    def _fingerprint_before(self, state: _RunState, action_id: str) -> str | None:
        for trace in reversed(self.journal.traces(state.run_id)):
            payload = trace.get("payload")
            if trace.get("kind") != "dispatch" or not isinstance(payload, Mapping):
                continue
            if payload.get("action_id") == action_id:
                return str(payload.get("fingerprint_before") or "")
        return None

    def _observe(self, state: _RunState) -> Snapshot:
        attempts = 0
        while True:
            try:
                return self.driver.observe(state.spec.scope)
            except (DriverError, ContractError) as exc:
                attempts += 1
                if attempts > state.spec.limits.stale_retries:
                    raise Pause(Reason.STALE_OBSERVATION, {"attempts": attempts, "error": str(exc)}) from exc
                self.config.sleeper(0.3 * attempts)

    def _check_identity(self, state: _RunState, snapshot: Snapshot) -> None:
        if state.identity_checked:
            return
        report = self.driver.identity(state.spec.app_ref, state.spec.expected_identity)
        state.identity_checked = True
        state.identity_status = report.status.value
        state.identity_verified = report.status.value == "verified"
        self.journal.append_trace(state.run_id, "identity", report.to_json())
        state.summary["identity"] = report.to_json()

    def _build_contexts(self, state: _RunState, step: Any, snapshot: Snapshot) -> list[OpContext]:
        # Operations that act on something other than an observed control get their own
        # candidate groups: an approved launch configuration, or a chord from a fixture.
        if step.operation is Operation.LAUNCH_APP:
            candidates = tuple(
                TargetCandidate(
                    element_id=new_id("cfg"),
                    description=f"approved launch configuration {name!r} runs {config.get('executable')}",
                    operation=Operation.LAUNCH_APP,
                    value=name,
                )
                for name, config in sorted(self.config.launch_configs.items())
            )
            return (
                [
                    OpContext(
                        operation=Operation.LAUNCH_APP,
                        candidates=candidates,
                        note="approved launch configurations only",
                    )
                ]
                if candidates
                else []
            )
        if step.operation is Operation.HOTKEY:
            chord = self._fixture_value(state, step.fixture_reference) if step.fixture_reference else None
            if not chord:
                return []
            return [
                OpContext(
                    operation=Operation.HOTKEY,
                    candidates=(
                        TargetCandidate(
                            element_id=new_id("cfg"),
                            description=f"keyboard chord {chord!r}",
                            operation=Operation.HOTKEY,
                            value=chord,
                        ),
                    ),
                    note="supported chords only",
                )
            ]
        observation = self._observation_for_policy(state, snapshot)
        operations = [step.operation]
        labels: dict[Operation, str] = {}
        if step.fixture_reference:
            value = self._fixture_value(state, step.fixture_reference)
            labels[step.operation] = f"step={step.step_id} fixture={step.fixture_reference}"
            if value is not None and step.operation is Operation.TYPE_TEXT:
                labels[step.operation] += f" text_to_enter={json.dumps(value, ensure_ascii=False)}"
        return build_contexts(
            observation=observation,
            operations=operations,
            fixture_labels=labels,
        )

    def _decide(self, state: _RunState, snapshot: Snapshot, step: Any, contexts: Sequence[OpContext]) -> Decision:
        if self.policy is None:
            raise PolicyError(
                "no decision policy is configured: set a TypeSafe API key, or drive the run with desktop_act"
            )
        observation = self._observation_for_policy(state, snapshot)
        required_remaining = [
            item.step_id for item in self._required_steps(state) if item.step_id not in state.completed_steps
        ]
        allow_done = state.spec.purpose is Purpose.EXPLORATORY or not required_remaining
        summarised = summarize_state_for_policy(
            goal=state.spec.goal,
            current_step={
                "step_id": step.step_id,
                "operation": step.operation.value,
                "target_description": step.target_description,
                "fixture_reference": step.fixture_reference,
                "purpose": state.spec.purpose.value,
                "remaining_steps": required_remaining,
            },
            snapshot_elements=observation["elements"],
            context=observation["context"],
            recent_actions=[
                {
                    "step_id": record.step_id,
                    "operation": record.operation.value,
                    "target": record.target_description,
                    "changed": record.observation_changed,
                }
                for record in state.steps[-6:]
            ],
            mode=state.spec.interaction_mode.value,
        )
        keep = [candidate.element_id for context in contexts for candidate in context.candidates]
        budget = state.state_budget_bytes or self.config.state_budget_bytes
        while True:
            fitted = fit_state_to_budget(summarised, keep_element_ids=keep, budget_bytes=budget)
            try:
                decision = self.policy.decide(
                    goal=state.spec.goal,
                    state=with_permitted_operations(fitted, [step.operation]),
                    contexts=contexts,
                    allow_done=allow_done,
                    allow_escalate=True,
                    current_step={
                        "step_id": step.step_id,
                        "operation": step.operation.value,
                        "target_description": step.target_description,
                    },
                )
                break
            except Pause as pause:
                if pause.detail.get("cause") != "max_tokens_exceeded":
                    raise
                # The provider's ceiling depends on its tokenizer and on the questions sent
                # with the state, so treat the first refusal as a measurement: shrink, remember
                # it for the rest of the run, and try once more.
                if budget <= self.config.min_state_budget_bytes:
                    raise Pause(
                        Reason.NEEDS_NARROWER_OBSERVATION,
                        {
                            "detail": "the state cannot be shrunk far enough for the provider",
                            "budget_bytes": budget,
                            "hint": "narrow scope.window_refs or lower scope.max_elements",
                        },
                    ) from pause
                budget = max(self.config.min_state_budget_bytes, int(budget * self.config.state_budget_shrink))
                state.state_budget_bytes = budget
                self.journal.append_trace(
                    state.run_id,
                    "state_budget_shrunk",
                    {"budget_bytes": budget, "step": step.step_id},
                )
        state.model_versions.append(decision.model)
        self.journal.append_trace(state.run_id, "decision", decision.to_json())
        return decision

    def _observation_for_policy(self, state: _RunState, snapshot: Snapshot) -> dict[str, Any]:
        secrets = self._secret_values(state)
        elements: list[dict[str, Any]] = []
        for element in snapshot.elements:
            payload = {
                "index": element.index,
                "element_id": element.element_id,
                "role": element.role,
                "name": self._redact(element.name, secrets),
                "value": self._redact(element.value, secrets),
                "enabled": element.enabled,
                "visible": element.visible,
                "editable": element.editable,
                "focused": element.focused,
                "operations": list(element.operations),
                "path": [self._redact(part, secrets) for part in element.path],
                "state": dict(element.state),
                "truncation": element.truncation,
            }
            if element.role == "edit" and element.value is None:
                payload["value"] = "(withheld)"  # password fields never leave the driver
            elements.append(payload)
        texts = [
            {
                "role": item.get("role"),
                "name": self._redact(item.get("name"), secrets),
                "text": self._redact(item.get("text"), secrets),
            }
            for item in (snapshot.context.get("texts") or [])
        ]
        observation = {
            "elements": elements,
            "context": {
                "window_titles": [window.title for window in snapshot.windows],
                "modal_windows": list(snapshot.context.get("modal_windows") or []),
                "texts": texts,
                "coverage": snapshot.coverage.value,
                "truncation": list(snapshot.truncation),
                "focused_element_id": snapshot.context.get("focused_element_id"),
            },
        }
        state.summary["observation"] = {
            "snapshot_id": snapshot.snapshot_id,
            "fingerprint": snapshot.fingerprint,
            "coverage": snapshot.coverage.value,
            "elements": len(snapshot.elements),
            "truncation": list(snapshot.truncation),
        }
        return observation

    # ------------------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------------------

    def _dispatch(
        self,
        state: _RunState,
        lease: Lease,
        decision: Decision,
        snapshot: Snapshot,
        step: Any,
    ) -> Snapshot | RunResult:
        spec = state.spec
        mode = spec.interaction_mode
        operation = decision.operation
        target: TargetCandidate | None = decision.target
        text: str | None = None
        option_label: str | None = None
        hotkey: tuple[str, ...] = ()
        launch_config_id: str | None = None
        scroll: dict[str, Any] = {}
        window_ref: str | None = None

        if operation in CONTROL_OPERATIONS:
            if operation is Operation.LAUNCH_APP:
                launch_config_id = (target.value if target else None) or spec.launch_config_id
                if launch_config_id is None and len(self.config.launch_configs) == 1:
                    # A single registered configuration is unambiguous, so the specification
                    # does not have to repeat it.
                    launch_config_id = next(iter(self.config.launch_configs))
                if not launch_config_id or launch_config_id not in self.config.launch_configs:
                    return self._pause(
                        state,
                        Reason.PERMISSION_BOUNDARY.value,
                        {
                            "detail": "LAUNCH_APP requires an approved launch configuration",
                            "configured": sorted(self.config.launch_configs),
                        },
                    )
            else:
                hotkey = tuple(self._hotkey_for(state, step))
        else:
            if target is None and operation is Operation.FOCUS_WINDOW:
                if not snapshot.windows:
                    return self._pause(state, Reason.STALE_OBSERVATION.value, {"step": step.step_id})
                window_ref = snapshot.windows[0].window_ref
            elif target is None:
                return self._pause(state, Reason.NO_APPROPRIATE_TARGET.value, {"step": step.step_id})
            if operation is Operation.TYPE_TEXT:
                if not step.fixture_reference:
                    return self._pause(
                        state, Reason.NEEDS_TEXT.value, {"step": step.step_id, "detail": "no fixture reference"}
                    )
                text = self._fixture_value(state, step.fixture_reference)
                if text is None:
                    return self._pause(
                        state,
                        Reason.NEEDS_TEXT.value,
                        {"step": step.step_id, "fixture": step.fixture_reference},
                    )
            if operation is Operation.SELECT:
                if not step.fixture_reference:
                    return self._pause(
                        state,
                        Reason.NEEDS_TEXT.value,
                        {"step": step.step_id, "detail": "SELECT needs an option fixture"},
                    )
                option_label = self._fixture_value(state, step.fixture_reference)
                if option_label is None:
                    return self._pause(
                        state,
                        Reason.NEEDS_TEXT.value,
                        {"step": step.step_id, "fixture": step.fixture_reference},
                    )
            if operation is Operation.SCROLL:
                scroll = {"notches": 3}

        if target is not None:
            window_ref = snapshot.element(target.element_id).window_ref
        elif window_ref is None and snapshot.windows:
            window_ref = snapshot.windows[0].window_ref

        action_id = new_id("act")
        request = ActionRequest(
            action_id=action_id,
            run_id=state.run_id,
            operation=operation,
            mode=mode,
            element_id=target.element_id if target else None,
            snapshot_id=snapshot.snapshot_id if target else None,
            window_ref=window_ref,
            lease_generation=lease.generation,
            step_id=step.step_id,
            text=text,
            replace_existing=getattr(step, "replace_existing", True),
            option_label=option_label,
            hotkey=hotkey,
            scroll=scroll,
            launch_config_id=launch_config_id,
            deadline_s=min(15.0, max(3.0, state.spec.limits.slice_seconds)),
        )
        request = replace(request, request_hash=self._request_hash(state, request))

        def guard() -> None:
            self.ownership.checkpoint(
                run_id=state.run_id,
                lease_id=lease.lease_id,
                generation=lease.generation,
                session_id=lease.session_id,
            )

        self.journal.append_trace(
            state.run_id,
            "dispatch",
            {
                "action_id": action_id,
                "operation": operation.value,
                "element_id": request.element_id,
                "fingerprint_before": snapshot.fingerprint,
            },
        )
        try:
            receipt = self.journal.dispatch_once(
                action_id=action_id,
                request_hash=request.request_hash,
                run_id=state.run_id,
                guard=guard,
                send=lambda: self._send(state, request, guard, snapshot),
            )
        except Pause as pause:
            if pause.reason_value in {Reason.STALE_OBSERVATION.value, Reason.LOW_CONFIDENCE.value}:
                state.stale_retries += 1
                if state.stale_retries > spec.limits.stale_retries:
                    return self._pause(state, pause.reason_value, {**pause.detail, "retries": state.stale_retries})
                self.journal.append_trace(state.run_id, "stale_retry", {"detail": pause.detail})
                return snapshot
            self._record_step(state, request, None, step, "", DispatchState.NOT_DISPATCHED, error=pause.reason_value)
            if pause.reason_value == Reason.NEEDS_VISUAL_ASSISTANCE.value:
                return self._visual_boundary(state, snapshot, None)
            return self._pause(state, pause.reason_value, pause.detail)
        except EmergencyStop as stop:
            return self._stopped(state, Reason.PERMISSION_BOUNDARY.value, str(stop))

        state.actions += 1
        state.supplied_visual.pop(step.step_id, None)
        fresh = self._observe_settled(state, before=snapshot.fingerprint)
        changed = fresh.fingerprint != snapshot.fingerprint
        self._record_step(
            state,
            request,
            receipt,
            step,
            target.description if target else operation.value,
            DispatchState.DISPATCHED,
            changed=changed,
        )
        self._advance_step(state, step, receipt)
        if changed:
            state.no_progress = 0
        else:
            state.no_progress += 1
            if state.no_progress > spec.limits.no_progress_retries:
                self._capture(state, fresh, checkpoint=step.step_id, description="no observable progress", keep=True)
                return self._pause(
                    state,
                    Reason.STEP_UNRESOLVED.value,
                    {
                        "step": step.step_id,
                        "detail": "no observable progress after bounded retries",
                        "attempts": state.no_progress,
                    },
                )
        state.last_fingerprint = fresh.fingerprint
        return fresh

    def _send(self, state: _RunState, request: ActionRequest, guard: Callable[[], None], snapshot: Snapshot) -> Receipt:
        if request.operation is Operation.LAUNCH_APP:
            return self._launch(state, request)
        return self.driver.execute(request, guard, snapshot)

    def _launch(self, state: _RunState, request: ActionRequest) -> Receipt:
        config = self.config.launch_configs.get(request.launch_config_id or "")
        if not config:
            raise ContractError("unknown launch configuration")
        executable = str(config.get("executable") or "")
        args = [str(item) for item in config.get("args", [])]
        cwd = str(config.get("cwd") or os.getcwd())
        env = {**os.environ, **{str(key): str(value) for key, value in (config.get("env") or {}).items()}}
        # The launched application can echo this into its artifacts so run-scoped assertions work.
        env["JEV_DESKTOP_RUN_ID"] = state.run_id
        started = self.config.clock()
        try:
            process = subprocess.Popen(
                [executable, *args],
                cwd=cwd,
                env=env,
                close_fds=True,
            )
        except OSError as exc:
            raise UncertainEffect(f"launch failed: {exc}", mechanism=DispatchMechanism.NONE) from exc
        self.journal.append_trace(state.run_id, "launch", {"pid": process.pid, "config": request.launch_config_id})
        return Receipt(
            action_id=request.action_id,
            dispatch_state=DispatchState.DISPATCHED,
            mechanism=DispatchMechanism.NONE,
            inserted_events=1,
            started_at=started,
            finished_at=self.config.clock(),
            target={"launch_config_id": request.launch_config_id, "pid": process.pid},
            notes=("process launched; identity must be re-verified before accepting results",),
        )

    # ------------------------------------------------------------------------------
    # Steps, assertions, verdicts
    # ------------------------------------------------------------------------------

    def _required_steps(self, state: _RunState) -> list[Any]:
        return [step for step in state.spec.steps if step.required]

    def _current_step(self, state: _RunState) -> Any | None:
        for step in state.spec.steps:
            if step.step_id not in state.completed_steps:
                if any(dependency not in state.completed_steps for dependency in step.depends_on):
                    raise ContractError(f"step {step.step_id} depends on an incomplete step")
                state.current_step_id = step.step_id
                return step
        state.current_step_id = None
        return None

    def _advance_step(self, state: _RunState, step: Any, receipt: Receipt) -> None:
        if receipt.dispatch_state is DispatchState.DISPATCHED:
            if step.step_id not in state.completed_steps:
                state.completed_steps.append(step.step_id)
        elif receipt.dispatch_state in RESERVED_DISPATCH_STATES:
            state.uncertain = True
        if step.required and receipt.dispatch_state is not DispatchState.DISPATCHED:
            state.uncertain = True

    def _record_step(
        self,
        state: _RunState,
        request: ActionRequest,
        receipt: Receipt | None,
        step: Any,
        description: str,
        dispatch_state: DispatchState,
        *,
        changed: bool | None = None,
        error: str | None = None,
    ) -> None:
        state.steps.append(
            StepRecord(
                step_id=step.step_id if step else request.step_id,
                operation=request.operation,
                target_element_id=request.element_id,
                target_description=description,
                dispatch_state=dispatch_state,
                receipt_action_id=receipt.action_id if receipt else None,
                observation_changed=changed,
                confidence=None,
                fixture_reference=step.fixture_reference if step else None,
                at=self.config.clock(),
                notes=tuple(receipt.notes) if receipt else (),
                error=error,
            )
        )

    def _evaluate_due(self, state: _RunState, snapshot: Snapshot, checkpoint: str | None) -> None:
        if checkpoint is None:
            return
        for spec in state.spec.assertions:
            if spec.checkpoint == "run_end":
                continue
            if spec.checkpoint not in {checkpoint, "any"}:
                continue
            if spec.checkpoint == "any" and checkpoint == "run_start":
                continue
            if spec.checkpoint == checkpoint and self._has_result(state, spec.assertion_id, terminal=True):
                continue
            self._evaluate_one(state, spec, snapshot, checkpoint)

    def _evaluate_one(self, state: _RunState, spec: AssertionSpec, snapshot: Snapshot, checkpoint: str) -> None:
        """Evaluate an assertion, honouring its deadline.

        A real application can take a moment to expose what the assertion looks for: a browser
        updates its window title after the page loads, not when the click is dispatched. The
        deadline lets the assertion wait for the state rather than testing the state at the
        instant the input landed.
        """
        result = self._evaluate_once(state, spec, snapshot, checkpoint)
        deadline = self.config.clock() + max(0.0, spec.deadline_s)
        while result.status is not AssertionStatus.PASSED and self.config.clock() < deadline:
            self.config.sleeper(0.25)
            try:
                snapshot = self._observe(state)
            except Pause:
                break
            result = self._evaluate_once(state, spec, snapshot, checkpoint)
        self._merge_assertion(state, result)
        self.journal.append_trace(state.run_id, "assertion", result.to_json())

    def _evaluate_once(
        self, state: _RunState, spec: AssertionSpec, snapshot: Snapshot, checkpoint: str
    ) -> AssertionResult:
        context = EvalContext(
            observation=snapshot,
            identity=None,
            evidence=self.evidence,
            approved_roots=list(self.config.approved_roots),
            caller_results={
                **state.supplied_visual,
                **{
                    result.assertion_id: result.to_json()
                    for result in state.assertions
                    if result.label == "caller_supplied"
                },
            },
            run_id=state.run_id,
            checkpoint=checkpoint,
            clock=self.config.clock,
        )
        return evaluate(spec, context)

    def _merge_assertion(self, state: _RunState, result: AssertionResult) -> None:
        for index, existing in enumerate(state.assertions):
            if existing.assertion_id != result.assertion_id:
                continue
            if existing.status is AssertionStatus.FAILED:
                return  # a proven failure is never erased by a later evaluation
            if report_rank(result) >= report_rank(existing):
                state.assertions[index] = result
            return
        state.assertions.append(result)

    def _has_result(self, state: _RunState, assertion_id: str, *, terminal: bool) -> bool:
        for result in state.assertions:
            if result.assertion_id == assertion_id and (
                not terminal or result.status is not AssertionStatus.NOT_EVALUATED
            ):
                return True
        return False

    def _pending_assertion_spec(self, state: _RunState) -> AssertionSpec | None:
        for spec in state.spec.assertions:
            if spec.evaluator is not Evaluator.MODEL_VISUAL or spec.oracle != "caller":
                continue
            if spec.checkpoint == "run_end":
                continue
            if spec.assertion_id in state.supplied_visual:
                continue
            if self._has_result(state, spec.assertion_id, terminal=True):
                continue
            return spec
        return None

    def _required_path_completed(self, state: _RunState) -> bool:
        required = {step.step_id for step in self._required_steps(state)}
        return required.issubset(set(state.completed_steps))

    def _uncertain_effects(self, state: _RunState) -> bool:
        if state.uncertain:
            return True
        return any(record.state in RESERVED_DISPATCH_STATES for record in self.journal.actions_for_run(state.run_id))

    def _verdict(self, state: _RunState, execution: str) -> tuple[str, dict[str, str]]:
        required = {spec.assertion_id for spec in state.spec.assertions if spec.required}
        qualified = qualified_assertions(state.assertions, expected_build_verified=state.identity_verified)
        verdict = aggregate_verdict(
            expected_build_verified=state.identity_verified,
            required=required,
            qualified=qualified,
            execution=execution,
            required_path_completed=self._required_path_completed(state),
            uncertain_effects=self._uncertain_effects(state),
        )
        return verdict, qualified

    # ------------------------------------------------------------------------------
    # Outcomes
    # ------------------------------------------------------------------------------

    def _complete(self, state: _RunState, snapshot: Snapshot) -> RunResult:
        for spec in state.spec.assertions:
            if spec.checkpoint == "run_end" or spec.checkpoint == "any":
                self._evaluate_one(state, spec, snapshot, "run_end")
        state.status = RunStatus.COMPLETED.value
        verdict, _ = self._verdict(state, Execution.COMPLETED.value)
        reason = None if verdict == Verdict.PASSED.value else "assertions did not all pass"
        if not state.identity_verified:
            reason = "the running build could not be verified"
        if not {spec.assertion_id for spec in state.spec.assertions}:
            reason = "no assertions were defined; a pass cannot be established"
        return self._result(state, Execution.COMPLETED, verdict, reason, snapshot=snapshot)

    def _requested_done(self, state: _RunState, snapshot: Snapshot) -> RunResult:
        remaining = [step.step_id for step in self._required_steps(state) if step.step_id not in state.completed_steps]
        if remaining:
            self.journal.append_trace(state.run_id, "done_rejected", {"remaining": remaining})
            return self._pause(state, Reason.STEP_UNRESOLVED.value, {"remaining_steps": remaining})
        return self._complete(state, snapshot)

    def _pause(self, state: _RunState, reason: str, detail: Mapping[str, Any]) -> RunResult:
        state.status = RunStatus.PAUSED.value
        state.pause_reason = reason
        state.pause_detail = dict(detail)
        verdict, _ = self._verdict(state, Execution.PAUSED.value)
        return self._result(state, Execution.PAUSED, verdict, reason, detail=detail)

    def _stopped(self, state: _RunState, reason: str, message: str) -> RunResult:
        state.status = RunStatus.CANCELLED.value
        state.pause_reason = reason
        verdict, _ = self._verdict(state, Execution.CANCELLED.value)
        return self._result(state, Execution.CANCELLED, verdict, reason, detail={"message": message})

    def _blocked(
        self,
        state: _RunState,
        reason: str,
        message: str,
        snapshot: Snapshot | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> RunResult:
        state.status = RunStatus.BLOCKED.value
        state.pause_reason = reason
        if snapshot is not None:
            self._capture(state, snapshot, checkpoint="blocked", description=message, keep=True)
        return self._result(
            state,
            Execution.BLOCKED,
            Verdict.INCONCLUSIVE,
            reason,
            detail={"message": message, **(dict(detail) if detail else {})},
        )

    def _error(self, state: _RunState, exc: BaseException) -> RunResult:
        state.status = RunStatus.ERROR.value
        state.pause_reason = "runner_error"
        return self._result(
            state,
            Execution.ERROR,
            Verdict.INCONCLUSIVE,
            "runner_error",
            detail={"error": f"{type(exc).__name__}: {exc}"},
        )

    def _unsupported(self, state: _RunState, snapshot: Snapshot, step: Any) -> RunResult:
        """No candidate supports the required operation: distinguish observation gap from unsupported control."""
        capture = self._capture(
            state,
            snapshot,
            checkpoint=step.step_id,
            description=f"no candidate for {step.operation.value}",
            keep=True,
        )
        if snapshot.coverage.value != "complete" or snapshot.truncation:
            state.status = RunStatus.PAUSED.value
            return self._result(
                state,
                Execution.PAUSED,
                Verdict.INCONCLUSIVE,
                Reason.NEEDS_VISUAL_ASSISTANCE.value,
                snapshot=snapshot,
                detail={
                    "step": step.step_id,
                    "operation": step.operation.value,
                    "truncation": list(snapshot.truncation),
                    "evidence_ref": capture.evidence.evidence_id if capture else None,
                },
            )
        return self._pause(
            state,
            Reason.UNSUPPORTED_CONTROL.value,
            {
                "step": step.step_id,
                "operation": step.operation.value,
                "evidence_ref": capture.evidence.evidence_id if capture else None,
            },
        )

    def _visual_boundary(self, state: _RunState, snapshot: Snapshot, spec: AssertionSpec | None) -> RunResult:
        capture = self._capture(
            state,
            snapshot,
            checkpoint=(spec.checkpoint if spec else state.current_step_id),
            description="visual assistance requested",
            keep=True,
        )
        state.pending_assertion = spec.assertion_id if spec else None
        return self._result(
            state,
            Execution.PAUSED,
            Verdict.INCONCLUSIVE,
            Reason.NEEDS_VISUAL_ASSISTANCE.value,
            snapshot=snapshot,
            detail={
                "assertion_id": spec.assertion_id if spec else None,
                "snapshot_id": snapshot.snapshot_id,
                "evidence_ref": capture.evidence.evidence_id if capture else None,
                "coordinate_transform": {
                    "source_rect": capture.source_rect.to_json() if capture else None,
                    "scale": capture.scale if capture else None,
                    "geometry_epoch": snapshot.geometry.epoch,
                },
            },
        )

    # ------------------------------------------------------------------------------
    # Evidence and persistence
    # ------------------------------------------------------------------------------

    def _capture(
        self, state: _RunState, snapshot: Snapshot, *, checkpoint: str | None, description: str, keep: bool = False
    ) -> Any:
        try:
            capture = self.driver.capture(
                scope=state.spec.scope,
                snapshot_id=snapshot.snapshot_id,
                region=None,
                max_scale=self.config.capture_scale,
                run_id=state.run_id,
                checkpoint=checkpoint,
                description=description,
            )
        except (DriverError, OSError, ContractError) as exc:
            self.journal.append_trace(state.run_id, "capture_failed", {"error": str(exc), "description": description})
            return None
        self.evidence.register(capture.evidence)
        self.journal.add_evidence(capture.evidence.evidence_id, state.run_id, capture.evidence.to_json())
        state.evidence_ids.append(capture.evidence.evidence_id)
        if keep:
            self.evidence._keep.add(capture.evidence.evidence_id)
        return capture

    def _result(
        self,
        state: _RunState,
        execution: Execution,
        verdict: str,
        reason: str | None,
        *,
        snapshot: Snapshot | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> RunResult:
        evidence: list[EvidenceRef] = []
        for evidence_id in state.evidence_ids:
            try:
                evidence.append(self.evidence.get(evidence_id))
            except ContractError:
                continue
        observation = self._observation_summary(snapshot) if snapshot is not None else state.summary.get("observation")
        return RunResult(
            run_id=state.run_id,
            execution=execution,
            verdict=Verdict(verdict),
            reason=reason,
            steps=tuple(state.steps),
            assertions=tuple(state.assertions),
            evidence=tuple(evidence),
            observation=observation,
            resume_token=state.resume_token,
            budgets=self._budget_view(state),
            message=self._message(state, execution, verdict, reason),
            detail=dict(detail or {}),
        )

    def _message(self, state: _RunState, execution: Execution, verdict: str, reason: str | None) -> str:
        if execution is Execution.COMPLETED and verdict == Verdict.PASSED.value:
            return "all required steps completed and every required assertion passed"
        if verdict == Verdict.FAILED.value:
            return "at least one required assertion failed against the expected build"
        if reason == Reason.NEEDS_VISUAL_ASSISTANCE.value:
            return "structured observation is insufficient; a scoped screenshot is attached for visual assistance"
        if reason == Reason.NEEDS_TEXT.value:
            return "a caller fixture is required before this step can continue"
        if reason == Reason.INCORRECT_BUILD.value:
            return "the running build is not the expected build; results would be meaningless"
        if execution is Execution.PAUSED:
            return f"paused for caller input ({reason})"
        if execution is Execution.CANCELLED:
            return "run cancelled; pause state retained, no further input issued"
        if execution is Execution.ERROR:
            return "runner error; no verdict is claimed"
        return f"{execution.value} ({reason or verdict})"

    def _observation_summary(self, snapshot: Snapshot | None) -> dict[str, Any] | None:
        if snapshot is None:
            return None
        return {
            "snapshot_id": snapshot.snapshot_id,
            "fingerprint": snapshot.fingerprint,
            "coverage": snapshot.coverage.value,
            "truncation": list(snapshot.truncation),
            "interval_ms": snapshot.interval_ms,
            "windows": [window.to_json() for window in snapshot.windows],
            "elements": [element.to_json() for element in snapshot.elements[:120]],
            "context": dict(snapshot.context),
        }

    def _budget_view(self, state: _RunState) -> dict[str, Any]:
        return {
            "actions": state.actions,
            "decisions": state.decisions,
            "slices": state.slices,
            "max_actions": state.spec.limits.max_actions,
            "max_model_decisions": state.spec.limits.max_model_decisions,
            "deadline_seconds": state.spec.limits.deadline_seconds,
            "elapsed_seconds": round(self.config.clock() - state.started_at, 3),
        }

    def _load(self, run_id: str) -> _RunState:
        state = self._state.get(run_id)
        if state is not None:
            return state
        row = self.journal.get_run(run_id)
        if row is None:
            raise ContractError(f"unknown run {run_id}")
        spec = RunSpec.from_json(json.loads(str(row["spec_json"])))
        if spec.frozen_digest() != row["spec_digest"]:
            raise ContractError("stored specification no longer matches its digest")
        state = _RunState.from_json(json.loads(str(row["state_json"])))
        self._state[run_id] = state
        return state

    def _persist(self, state: _RunState) -> None:
        self.journal.put_run(
            run_id=state.run_id,
            spec_digest=state.spec_digest,
            spec_json=json.dumps(state.spec.to_json(), ensure_ascii=False),
            status=state.status,
            state_json=json.dumps(state.to_json(), ensure_ascii=False),
            resume_token=state.resume_token,
        )

    def _finish_slice(self, state: _RunState, result: RunResult) -> None:
        state.resume_token = new_id("resume")
        self._persist(state)
        self.journal.append_trace(
            state.run_id,
            "slice_end",
            {"execution": result.execution.value, "verdict": result.verdict.value, "reason": result.reason},
        )
        self.evidence.prune()

    # ------------------------------------------------------------------------------
    # Inputs, fixtures, helpers
    # ------------------------------------------------------------------------------

    def _apply_inputs(self, state: _RunState, inputs: ResumeInputs) -> None:
        allowed_fixtures = set(state.spec.fixtures) | set(state.spec.secret_refs)
        for name in inputs.fixtures:
            if name not in allowed_fixtures:
                raise ContractError(f"resume may not introduce fixture {name!r}")
        state.supplied_fixtures.update({str(key): str(value) for key, value in inputs.fixtures.items()})

        assertion_ids = {spec.assertion_id for spec in state.spec.assertions}
        visual_ids = {
            spec.assertion_id
            for spec in state.spec.assertions
            if spec.evaluator is Evaluator.MODEL_VISUAL and spec.oracle == "caller"
        }
        for assertion_id in inputs.visual_results:
            if assertion_id not in visual_ids:
                raise ContractError(f"visual assistance is not defined for assertion {assertion_id!r}")
        for assertion_id in inputs.verifier_results:
            if assertion_id not in assertion_ids:
                raise ContractError(f"unknown assertion {assertion_id!r}")
        for assertion_id, payload in {**inputs.visual_results, **inputs.verifier_results}.items():
            entry = dict(payload)
            entry.setdefault("origin", ORIGIN_APPLICATION)
            state.supplied_visual[assertion_id] = entry
            spec = next(item for item in state.spec.assertions if item.assertion_id == assertion_id)
            self.journal.append_trace(
                state.run_id,
                "caller_verifier_result" if assertion_id in inputs.verifier_results else "visual_assistance",
                {"assertion_id": assertion_id, "payload_digest": digest(payload)},
            )
            result = evaluate(
                spec,
                EvalContext(
                    observation=None,
                    evidence=self.evidence,
                    approved_roots=list(self.config.approved_roots),
                    caller_results=state.supplied_visual,
                    run_id=state.run_id,
                    checkpoint=spec.checkpoint,
                    clock=self.config.clock,
                ),
            )
            self._merge_assertion(state, result)

    def _fixture_value(self, state: _RunState, name: str) -> str | None:
        if name in state.supplied_fixtures:
            return state.supplied_fixtures[name]
        if name in state.spec.secret_refs:
            return self.config.resolve_secret(str(state.spec.secret_refs[name]))
        literal = state.spec.fixtures.get(name)
        if isinstance(literal, str):
            return literal
        if isinstance(literal, Mapping) and "secret_ref" in literal:
            return self.config.resolve_secret(str(literal["secret_ref"]))
        return None

    def _secret_values(self, state: _RunState) -> list[str]:
        values: list[str] = []
        for name in state.spec.secret_refs:
            value = self.config.resolve_secret(str(state.spec.secret_refs[name]))
            if value:
                values.append(value)
        for literal in state.spec.fixtures.values():
            if isinstance(literal, Mapping) and "secret_ref" in literal:
                value = self.config.resolve_secret(str(literal["secret_ref"]))
                if value:
                    values.append(value)
        values.extend(value for value in state.supplied_fixtures.values() if value)
        return values

    @staticmethod
    def _redact(value: str | None, secrets: Sequence[str]) -> str | None:
        if value is None:
            return None
        redacted = value
        for index, secret in enumerate(secrets):
            if secret and secret in redacted:
                redacted = redacted.replace(secret, f"<secret:{index}>")
        return redacted

    def _request_hash(self, state: _RunState, request: ActionRequest) -> str:
        binding = request.dispatch_binding()
        fingerprint = None
        if request.text is not None:
            fingerprint = keyed_fingerprint(self.config.fingerprint_secret, request.text)
        if request.option_label is not None:
            fingerprint = keyed_fingerprint(
                self.config.fingerprint_secret, f"{request.option_label}|{fingerprint or ''}"
            )
        return digest({**binding, "value_fingerprint": fingerprint})

    def _hotkey_for(self, state: _RunState, step: Any) -> list[str]:
        if step.fixture_reference:
            value = self._fixture_value(state, step.fixture_reference)
            if value:
                return [part for part in value.replace(" ", "").split("+") if part]
        raise Pause(Reason.UNSUPPORTED_CONTROL, {"step": step.step_id, "detail": "hotkey chord not supplied"})

    def _validate_spec(self, spec: RunSpec) -> None:
        limits: Limits = spec.limits.clamped()
        if limits.to_json() != spec.limits.to_json():
            raise ContractError("caller limits exceed local policy; narrow them before creating the run")
        step_ids = [step.step_id for step in spec.steps]
        if len(set(step_ids)) != len(step_ids):
            raise ContractError("step ids must be unique")
        assertion_ids = [item.assertion_id for item in spec.assertions]
        if len(set(assertion_ids)) != len(assertion_ids):
            raise ContractError("assertion ids must be unique")
        known_checkpoints = set(step_ids) | {"any", "run_start", "run_end"}
        for item in spec.assertions:
            if item.checkpoint not in known_checkpoints:
                raise ContractError(f"assertion {item.assertion_id} uses unknown checkpoint {item.checkpoint!r}")
            if item.evaluator is Evaluator.MODEL_VISUAL and not item.oracle:
                raise ContractError(
                    f"visual assertion {item.assertion_id} must name its oracle explicitly (caller or provider)"
                )
        for step in spec.steps:
            for dependency in step.depends_on:
                if dependency not in set(step_ids):
                    raise ContractError(f"step {step.step_id} depends on unknown step {dependency}")


def report_rank(result: AssertionResult) -> int:
    """Ordering used when several evaluations exist for one assertion."""
    order = {
        AssertionStatus.FAILED: 3,
        AssertionStatus.PASSED: 2,
        AssertionStatus.INCONCLUSIVE: 1,
        AssertionStatus.NOT_EVALUATED: 0,
    }
    return order.get(result.status, 0)
