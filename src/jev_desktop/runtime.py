"""Bounded test runtime: immutable specification, checkpoints, budgets, resumability.

The runtime owns step ordering, pause/resume, retry bounds, assertion scheduling, and
evidence capture. It never lets model output, UI content, or a transport retry widen the
test: the specification and its digest are frozen at run creation, slice deadlines return a
resumable checkpoint before any client tool deadline, and a resuming caller may only supply
fixture values, scoped visual assistance, or an explicitly requested verifier result.
"""

from __future__ import annotations

import json
import math
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
    IdentityReport,
    Limits,
    Operation,
    Pause,
    Purpose,
    Reason,
    Receipt,
    RequiredStep,
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
    JevPolicy,
    OpContext,
    PolicyError,
    build_contexts,
    summarize_state_for_policy,
    with_permitted_operations,
)
from .verification import (
    ORIGIN_APPLICATION,
    EvalContext,
    aggregate_verdict,
    evaluate,
    qualified_assertions,
    validate_assertion,
)

RESERVED_DISPATCH_STATES = {DispatchState.DISPATCHING, DispatchState.UNCERTAIN}
CONTROL_OPERATIONS = {Operation.HOTKEY}


def caller_view(snapshot: Snapshot, *, limit: int, query: str = "") -> dict[str, Any]:
    """Elements, context, and truncation as callers see them; the model and verification read the snapshot.

    Rows without a name, value, text, operation, or focus are layout structure: bulk, not meaning.
    Context texts already carried by a returned element are dropped for the same reason.
    """
    elements = [e for e in snapshot.elements if e.name or e.value or e.text or e.operations or e.focused]
    truncation = list(snapshot.truncation)
    if query:
        elements = [e for e in elements if e.matches(query)]
        if len(elements) > limit:
            truncation.append(f"{len(elements)} elements match the query; showing the first {limit}")
    shown = elements[:limit]
    context = dict(snapshot.context)
    if "texts" in context:
        represented = {part for e in shown for part in (e.name, e.value, e.text) if part}
        context["texts"] = [item for item in context["texts"] or [] if item.get("text") not in represented]
    return {"elements": [e.to_json() for e in shown], "context": context, "truncation": truncation}


@dataclass
class RuntimeConfig:
    evidence_dir: Path
    approved_roots: tuple[str, ...] = ()
    secret_provider: Callable[[str], str | None] | None = None
    launch_configs: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    capture_checkpoints: bool = True
    capture_failures: bool = True
    capture_scale: float = 0.6
    # Bounded settling allowance; a receipt is not proof the application processed input.
    settle_seconds: float = 0.8
    fingerprint_secret: bytes = field(default_factory=lambda: os.urandom(32))
    sleeper: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = now
    # After physical input, wait this long without any before observing again and continuing.
    human_grace_seconds: float = 3.0
    # Waiting for the user extends the run deadline by at most this much in total.
    max_hold_seconds: float = 300.0

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

    def __post_init__(self) -> None:
        if any(not isinstance(value, str) for value in self.fixtures.values()):
            raise ContractError("resume fixture values must be strings")
        results = {**self.visual_results, **self.verifier_results}
        if any(not isinstance(value, Mapping) for value in results.values()):
            raise ContractError("resume evaluator results must be objects")


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
    supplied_visual: dict[str, Mapping[str, Any]] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)
    pending_assertion: str | None = None
    uncertain: bool = False
    slice_deadline: float = float("inf")
    # In memory only: the per-call limit, and the physical-input count at the last observation.
    slice_limit: float = float("inf")
    observed_epoch: int = 0

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
            # Resumed fixture values may be credentials; retain them only in memory.
            "supplied_fixtures": {},
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
        self.config.fingerprint_secret = journal.fingerprint_key()
        self.policy = policy
        if hasattr(driver, "quiesce"):
            self.ownership._quiesce = driver.quiesce
            self.ownership._on_acquire = driver.retain_lease
        # Physical input while Jev holds the desktop: Esc stops the run, anything else makes it wait.
        self.presence = getattr(driver, "presence", None)
        if self.presence is not None:
            self.ownership._on_active = self.presence.set_active
            self.presence.on_escape = self._on_escape
        self._state: dict[str, _RunState] = {}

    # ------------------------------------------------------------------------------
    # Run lifecycle
    # ------------------------------------------------------------------------------

    def create_run(self, spec: RunSpec, *, session_id: str) -> dict[str, Any]:
        self.ownership.authorize(session_id)
        if not spec.steps and not spec.assertions and spec.purpose is not Purpose.TASK:
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
            summary={"owner_session": session_id},
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
        if state.status in {RunStatus.CANCELLED.value, RunStatus.COMPLETED.value}:
            raise ContractError("run is terminal; create a new run")
        lease = self.ownership.active_lease()
        if lease is None or lease.run_id != run_id or lease.session_id != session_id:
            raise ContractError("this run does not hold the desktop lease")
        if inputs is not None:
            self._apply_inputs(state, inputs)
        self.journal.require_healthy()

        state.slices += 1
        state.status = RunStatus.RUNNING.value
        requested = state.spec.limits.slice_seconds if slice_seconds is None else slice_seconds
        if not math.isfinite(requested) or requested <= 0:
            raise ContractError("slice_seconds must be finite and positive")
        requested = min(requested, state.spec.limits.slice_seconds)
        state.slice_limit = self.config.clock() + requested
        deadline = state.slice_deadline = min(state.slice_limit, self._run_deadline(state))
        if hasattr(self.driver, "set_boundary"):
            self.driver.set_boundary(
                lambda: self.ownership.checkpoint(
                    run_id=run_id, lease_id=lease.lease_id, generation=lease.generation, session_id=session_id
                ),
                deadline - self.config.clock(),
            )
        self.journal.append_trace(
            run_id, "slice_start", {"slice": state.slices, "deadline_s": deadline - self.config.clock()}
        )
        try:
            self._reconcile_unfinished(state)
            result = self._task_loop(state, lease) if state.spec.purpose is Purpose.TASK else self._loop(state, lease)
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
        finally:
            if hasattr(self.driver, "set_boundary"):
                self.driver.set_boundary(None)
        self._finish_slice(state, result)
        # The token is rotated at the end of every slice, so the result carries the live one.
        return replace(result, resume_token=state.resume_token)

    def stop(
        self, *, run_id: str, session_id: str, reason: str = "caller cancelled", resume_token: str | None = None
    ) -> dict[str, Any]:
        self.ownership.authorize(session_id)
        state = self._load(run_id)
        if state.summary.get("owner_session") != session_id and resume_token != state.resume_token:
            raise ContractError("stopping another session's run requires its current resume token")
        if state.status == RunStatus.COMPLETED.value:
            return {"run_id": run_id, "status": state.status, "reason": "run already completed"}
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
            "app_ref": self._scope(state).app_ref,
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
        state = self._load(run_id)
        state.slice_limit = self.config.clock() + state.spec.limits.slice_seconds
        state.slice_deadline = min(state.slice_limit, self._run_deadline(state))
        lease = self.ownership.active_lease()
        if lease is None:
            raise ContractError("this run does not hold the desktop lease")
        if hasattr(self.driver, "set_boundary"):
            self.driver.set_boundary(
                lambda: self.ownership.checkpoint(
                    run_id=run_id, lease_id=lease.lease_id, generation=lease.generation, session_id=session_id
                ),
                state.slice_deadline - self.config.clock(),
            )
        actions = state.actions
        try:
            result = self._act(run_id=run_id, session_id=session_id, resume_token=resume_token, request=request)
            state.resume_token = new_id("resume")
            self._persist(state)
            result["resume_token"] = state.resume_token
            return result
        except UncertainEffect:
            state.uncertain = True
            self._persist(state)
            raise
        except Pause as pause:
            if state.actions == actions:
                raise
            # Input was sent, so the presented token must not authorize another action.
            state.resume_token = new_id("resume")
            raise Pause(pause.reason, {**pause.detail, "resume_token": state.resume_token}) from pause
        finally:
            self._persist(state)
            if hasattr(self.driver, "set_boundary"):
                self.driver.set_boundary(None)

    def _act(
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
        self._reconcile_unfinished(state)
        if state.status in {RunStatus.CANCELLED.value, RunStatus.COMPLETED.value}:
            raise ContractError("run is terminal; create a new run")
        if request.mode is not state.spec.interaction_mode:
            raise ContractError("action mode must match the immutable specification")
        step = self._current_step(state)
        if step is None or request.operation is not step.operation or request.step_id != step.step_id:
            raise ContractError("action must match the current required step")
        if state.actions >= state.spec.limits.max_actions:
            raise Pause(Reason.BUDGET_EXHAUSTED, {"budget": "max_actions"})
        if self.config.clock() >= self._run_deadline(state):
            raise Pause(Reason.BUDGET_EXHAUSTED, {"budget": "run_deadline"})
        if request.operation in {Operation.TYPE_TEXT, Operation.SELECT}:
            value = self._fixture_value(state, step.fixture_reference) if step.fixture_reference else None
            supplied = request.text if request.operation is Operation.TYPE_TEXT else request.option_label
            if value is None or supplied != value or request.replace_existing != step.replace_existing:
                raise ContractError("action text and replacement mode must match the step fixture")
        if request.operation is Operation.HOTKEY and tuple(self._hotkey_for(state, step)) != request.hotkey:
            raise ContractError("action hotkey must match the step fixture")
        if request.operation is Operation.LAUNCH_APP:
            raise ContractError("launch must use desktop_run with an approved launch configuration")
        self._restore_binding(state)
        snapshot = self.driver.snapshot(request.snapshot_id, self._scope(state))
        self._check_identity(state, snapshot)
        if not state.identity_verified:
            raise Pause(Reason.INCORRECT_BUILD)
        if request.window_ref not in {window.window_ref for window in snapshot.windows}:
            raise ContractError("action window is outside the run scope")
        if request.element_id:
            element = snapshot.element(request.element_id)
            if element.window_ref != request.window_ref:
                raise ContractError("action element and window do not match")
        if request.point is not None:
            reference = self.evidence.get(request.point.evidence_id)
            request.point.resolve(reference, run_id, request.snapshot_id)
        if not state.steps:
            self._evaluate_due(state, snapshot, checkpoint="run_start")
        guarded = replace(request, run_id=run_id, lease_generation=lease.generation)
        guarded = replace(guarded, request_hash=self._request_hash(state, guarded))
        epoch = self._human_epoch()

        def guard() -> None:
            if self.config.clock() >= state.slice_deadline:
                raise Pause(Reason.BUDGET_EXHAUSTED, self._deadline_budget(state))
            if self._human_epoch() != epoch:
                raise Pause(Reason.USER_TAKEOVER, {"reason": "physical input during the action; inspect again"})
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
        state.actions += 1
        self._record_step(state, guarded, receipt, step, step.target_description, DispatchState.DISPATCHED)
        self._advance_step(state, step, receipt)
        state.summary["pending_checkpoint"] = step.step_id
        self._persist(state)
        observation = (
            snapshot if self._launch_follows(state, step) else self._observe_settled(state, before=snapshot.fingerprint)
        )
        self._evaluate_due(state, observation, checkpoint=step.step_id)
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

    def _task_loop(self, state: _RunState, lease: Lease) -> RunResult:
        """Keep routine decisions inside the broker until completion or escalation."""
        if self.policy is None:
            raise PolicyError("desktop tasks require a TypeSafe API key")
        if state.summary.get("restore_binding"):
            raise ContractError("inspect again and start a new task after restarting the broker")
        snapshot = self._observe(state)
        completion_probe = False
        # The first low-confidence refusal since the last dispatch; a second one reports it.
        doubt: Pause | None = None
        value_target: TargetCandidate | None = None
        inputs_without_value: set[str] = set()
        # Operations whose target answer was NONE, withdrawn until the snapshot changes.
        operations_without_target: set[Operation] = set()
        operations_checked_on = snapshot.snapshot_id
        auto_focused = False
        while True:
            self.ownership.checkpoint(
                run_id=state.run_id,
                lease_id=lease.lease_id,
                generation=lease.generation,
                session_id=lease.session_id,
            )
            if self._hold_for_user(state, lease):
                # The screen may have changed under the user; a refocus is among the next choices.
                value_target = None
                snapshot = self._observe(state)
                continue
            state.summary["task_observation"] = self._observation_summary(snapshot)
            if self.config.clock() >= state.slice_deadline:
                return self._pause(state, Reason.BUDGET_EXHAUSTED.value, self._deadline_budget(state))
            if state.decisions >= state.spec.limits.max_model_decisions:
                return self._pause(state, Reason.BUDGET_EXHAUSTED.value, {"budget": "max_model_decisions"})
            observation = self._observation_for_policy(state, snapshot)
            contexts = build_contexts(observation=observation, operations=[Operation.CLICK, Operation.TOGGLE])
            bindings: dict[str, tuple[TargetCandidate, str | None, dict[str, Any]]] = {}
            for operation in (Operation.TYPE_TEXT, Operation.SCROLL):
                base = build_contexts(observation=observation, operations=[operation])
                choices = []
                for context in base:
                    for target in context.candidates:
                        if target.element_id in inputs_without_value:
                            continue
                        values = (
                            [("up", 3), ("down", -3)]
                            if operation is Operation.SCROLL
                            else [
                                (name, value)
                                for name, value in state.spec.fixtures.items()
                                if name.startswith(("text:", "secret:"))
                            ]
                        )
                        if operation is not Operation.SCROLL and len(values) > 1:
                            choices.append(target)
                            continue
                        for name, value in values:
                            if operation is Operation.TYPE_TEXT and snapshot.element(target.element_id).value == value:
                                continue
                            candidate = replace(
                                target,
                                element_id=new_id("cfg"),
                                description=f"{target.description}; supplied value {name}",
                            )
                            choices.append(candidate)
                            bindings[candidate.element_id] = (
                                target,
                                name if operation is not Operation.SCROLL else None,
                                {"notches": value} if operation is Operation.SCROLL else {},
                            )
                if choices:
                    contexts.append(OpContext(operation=operation, candidates=tuple(choices)))
            focused = next((window for window in snapshot.windows if window.focused), None)
            unfocused = [window for window in snapshot.windows if not window.focused and window.enabled]
            if unfocused:
                contexts.append(
                    OpContext(
                        operation=Operation.FOCUS_WINDOW,
                        candidates=tuple(
                            TargetCandidate(window.window_ref, f"Focus window {window.title!r}", Operation.FOCUS_WINDOW)
                            for window in unfocused
                        ),
                    )
                )
            if focused:
                keys = []
                for name, value in state.spec.fixtures.items():
                    if not name.startswith("key:"):
                        continue
                    candidate = TargetCandidate(new_id("cfg"), f"Send {value!r} to {focused.title!r}", Operation.HOTKEY)
                    keys.append(candidate)
                    bindings[candidate.element_id] = (candidate, name, {})
                if keys:
                    chords = [value for name, value in state.spec.fixtures.items() if name.startswith("key:")]
                    contexts.append(
                        OpContext(
                            operation=Operation.HOTKEY,
                            candidates=tuple(keys),
                            note=f"Send one of {chords!r} to the focused window.",
                        )
                    )
            if focused is not None:
                auto_focused = False
            elif len(unfocused) == 1 and not auto_focused and state.actions < state.spec.limits.max_actions:
                # Focusing the one approved window is routine, not a judgment. Left to the model,
                # every other control is withheld and escalation looks like the only choice.
                auto_focused = True
                window = unfocused[0]
                target = TargetCandidate(window.window_ref, f"Focus window {window.title!r}", Operation.FOCUS_WINDOW)
                step = RequiredStep(
                    step_id=f"action-{state.actions + 1}",
                    operation=Operation.FOCUS_WINDOW,
                    target_description=target.description,
                )
                state.current_step_id = step.step_id
                dispatched = self._dispatch(
                    state,
                    lease,
                    Decision(Operation.FOCUS_WINDOW, target, 1.0, 1.0, "broker", {}, 0, "", ""),
                    snapshot,
                    step,
                )
                if isinstance(dispatched, RunResult):
                    return dispatched
                snapshot = self._observe(state) if dispatched.snapshot_id == snapshot.snapshot_id else dispatched
                continue
            if focused is None:
                contexts = [context for context in contexts if context.operation is Operation.FOCUS_WINDOW]
            if value_target is not None:
                choices = []
                for name, value in state.spec.fixtures.items():
                    if not name.startswith(("text:", "secret:")):
                        continue
                    if (
                        value_target.operation is Operation.TYPE_TEXT
                        and snapshot.element(value_target.element_id).value == value
                    ):
                        continue
                    candidate = replace(
                        value_target,
                        element_id=new_id("cfg"),
                        description=(
                            f"Enter the secret value {name.removeprefix('secret:')} ({len(value)} characters)"
                            if name.startswith("secret:")
                            else f"Enter {value!r}, supplied as {name.removeprefix('text:')}"
                        ),
                        fixture_ref=name,
                    )
                    choices.append(candidate)
                    bindings[candidate.element_id] = (value_target, name, {})
                contexts = (
                    [
                        OpContext(
                            operation=value_target.operation,
                            candidates=tuple(choices),
                            note=f"Choose a supplied value for the selected control: {value_target.description}",
                            selecting_value=True,
                        )
                    ]
                    if choices
                    else []
                )
            if operations_checked_on != snapshot.snapshot_id:
                operations_without_target.clear()
                operations_checked_on = snapshot.snapshot_id
            contexts = [context for context in contexts if context.operation not in operations_without_target]
            # At the action limit, a final decision may report completion but cannot dispatch.
            if completion_probe or state.actions >= state.spec.limits.max_actions:
                contexts = []
            model_state = summarize_state_for_policy(
                goal=state.spec.goal,
                current_step=None,
                snapshot_elements=observation["elements"],
                context=observation["context"],
                mode=state.spec.interaction_mode.value,
                recent_actions=[
                    {
                        "operation": step.operation.value,
                        "target": step.target_description,
                        "changed": step.observation_changed,
                        "input": self._redact(
                            self._fixture_value(state, step.fixture_reference), self._secret_values(state)
                        )
                        if step.fixture_reference
                        else None,
                    }
                    for step in state.steps
                ],
            )
            if value_target is not None:
                model_state["selected_input_control"] = value_target.description
            model_state["supplied_text"] = {
                name.removeprefix("text:"): value
                for name, value in state.spec.fixtures.items()
                if name.startswith("text:")
            }
            secret_text = {
                name.removeprefix("secret:"): f"withheld, {len(value)} characters"
                for name, value in state.spec.fixtures.items()
                if name.startswith("secret:")
            }
            if secret_text:
                model_state["secret_text"] = secret_text
            model_state["allowed_hotkeys"] = [
                value for name, value in state.spec.fixtures.items() if name.startswith("key:")
            ]
            model_state["focused_window"] = focused.title if focused else None
            model_state = with_permitted_operations(model_state, [context.operation for context in contexts])
            state.decisions += 1
            self._persist(state)
            try:
                decision = self.policy.decide(
                    goal=state.spec.goal,
                    state=model_state,
                    contexts=contexts,
                    allow_done=True,
                    allow_escalate=True,
                    current_step=None,
                    **(
                        {"deadline": time.monotonic() + max(0.0, state.slice_deadline - self.config.clock())}
                        if isinstance(self.policy, JevPolicy)
                        else {}
                    ),
                )
            except Pause as pause:
                if pause.reason is Reason.NO_APPROPRIATE_TARGET and value_target is not None:
                    inputs_without_value.add(value_target.element_id)
                    value_target = None
                    continue
                offered = {context.operation.value: context.operation for context in contexts}
                if pause.reason is Reason.NO_APPROPRIATE_TARGET and pause.detail.get("operation") in offered:
                    # The operation choice cannot see every target; another operation may reach it.
                    operations_without_target.add(offered[pause.detail["operation"]])
                    continue
                if pause.reason is not Reason.LOW_CONFIDENCE or not state.actions:
                    raise
                if doubt is not None:
                    raise doubt from None
                # A transition can hide the next control or the result, so observe once more. Only
                # doubt about finishing is rechecked without offering more input.
                doubt = pause
                completion_probe = pause.detail.get("operation") in {Operation.DONE.value, Operation.WAIT.value}
                self.config.sleeper(min(1.0, max(0.0, state.slice_deadline - self.config.clock())))
                value_target = None
                snapshot = self._observe(state, completion=completion_probe)
                continue
            finally:
                if isinstance(self.policy, JevPolicy):
                    self._record_model_attempts(state)
            self.ownership.checkpoint(
                run_id=state.run_id,
                lease_id=lease.lease_id,
                generation=lease.generation,
                session_id=lease.session_id,
            )
            state.model_versions.append(decision.model)
            if not isinstance(self.policy, JevPolicy):
                metrics = state.summary.setdefault(
                    "task_metrics",
                    {"model_latency_ms": 0, "input_tokens": 0, "output_tokens": 0, "usage_complete": True},
                )
                metrics["model_latency_ms"] += decision.latency_ms
                for key in ("input_tokens", "output_tokens"):
                    value = decision.usage.get(key)
                    if isinstance(value, int) and value >= 0:
                        metrics[key] += value
                    else:
                        metrics["usage_complete"] = False
            self.journal.append_trace(state.run_id, "decision", decision.to_json())
            self._persist(state)
            unsettled = {"low_confidence": doubt.detail} if completion_probe and doubt is not None else {}
            if decision.operation is Operation.DONE:
                # Refresh independently of the model's observation; a changed screen needs another decision.
                fresh = self._observe(state, completion=completion_probe)
                if fresh.fingerprint != snapshot.fingerprint:
                    snapshot = fresh
                    continue
                confirm_state = {key: value for key, value in model_state.items() if key != "permitted_operations"}
                try:
                    confirmed = self.policy.confirm_done(
                        goal=state.spec.goal,
                        state=confirm_state,
                        **(
                            {"deadline": time.monotonic() + max(0.0, state.slice_deadline - self.config.clock())}
                            if isinstance(self.policy, JevPolicy)
                            else {}
                        ),
                    )
                except Pause as pause:
                    if pause.reason is not Reason.LOW_CONFIDENCE:
                        raise
                    confirmed = False
                finally:
                    if isinstance(self.policy, JevPolicy):
                        self._record_model_attempts(state)
                if not confirmed:
                    return self._pause(
                        state,
                        Reason.NEEDS_VISUAL_ASSISTANCE.value,
                        {
                            "detail": "completion is not visible in the final observation; inspect the final window",
                            **unsettled,
                        },
                    )
                state.status = RunStatus.COMPLETED.value
                return self._result(
                    state,
                    Execution.COMPLETED,
                    Verdict.INCONCLUSIVE.value,
                    None,
                    snapshot=fresh,
                    detail={"completion": "model_reported"},
                )
            if completion_probe:
                return self._pause(
                    state,
                    Reason.NEEDS_VISUAL_ASSISTANCE.value,
                    {"detail": "completion could not be established; inspect the final window", **unsettled},
                )
            if state.actions >= state.spec.limits.max_actions:
                return self._pause(state, Reason.BUDGET_EXHAUSTED.value, {"budget": "max_actions"})
            if decision.operation is Operation.ESCALATE:
                return self._pause(
                    state,
                    Reason.NEEDS_VISUAL_ASSISTANCE.value,
                    {"detail": "task needs caller judgment or an input not supplied"},
                )
            if decision.operation is Operation.WAIT:
                value_target = None
                inputs_without_value.clear()
                self.config.sleeper(min(0.2, max(0.0, state.slice_deadline - self.config.clock())))
                snapshot = self._observe(state)
                continue
            if decision.target is None:
                return self._pause(state, Reason.NO_APPROPRIATE_TARGET.value, {})
            target, fixture, scroll = bindings.get(decision.target.element_id, (decision.target, None, {}))
            if decision.operation in {Operation.TYPE_TEXT, Operation.SELECT} and fixture is None:
                value_target = target
                continue
            value_target = None
            step = RequiredStep(
                step_id=f"action-{state.actions + 1}",
                operation=decision.operation,
                target_description=decision.target.description,
                fixture_reference=fixture,
            )
            state.current_step_id = step.step_id
            dispatched = self._dispatch(
                state, lease, replace(decision, target=target), snapshot, step, task_scroll=scroll
            )
            if isinstance(dispatched, RunResult):
                return dispatched
            snapshot = self._observe(state) if dispatched.snapshot_id == snapshot.snapshot_id else dispatched
            inputs_without_value.clear()
            doubt = None

    def _record_model_attempts(self, state: _RunState) -> None:
        if not isinstance(self.policy, JevPolicy):
            return
        metrics = state.summary.setdefault(
            "task_metrics",
            {
                "model_latency_ms": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "usage_complete": True,
            },
        )
        metrics["model_latency_ms"] += self.policy.last_latency_ms
        self.policy.last_latency_ms = 0
        for attempt in self.policy.last_attempts:
            self.journal.append_trace(state.run_id, "model_attempt", attempt)
            metrics["http_requests"] = metrics.get("http_requests", 0) + 1
            usage = attempt.get("usage")
            for key in ("input_tokens", "output_tokens"):
                value = usage.get(key) if isinstance(usage, dict) else None
                if type(value) is int and value >= 0:
                    metrics[key] += value
                else:
                    metrics["usage_complete"] = False
        self.policy.last_attempts = []
        self._persist(state)

    def _loop(self, state: _RunState, lease: Lease) -> RunResult:
        spec = state.spec
        if self.ownership.emergency_active():
            return self._stopped(state, Reason.PERMISSION_BOUNDARY.value, "local emergency stop is set")

        while True:
            if self.ownership.flags(state.run_id).cancelled:
                return self._stopped(state, Reason.USER_TAKEOVER.value, "run cancelled")
            if self.config.clock() >= state.slice_deadline:
                return self._pause(state, Reason.BUDGET_EXHAUSTED.value, self._deadline_budget(state))
            self._hold_for_user(state, lease)  # this loop observes afresh below either way

            step = self._current_step(state)
            if (
                step is not None
                and step.operation is Operation.LAUNCH_APP
                and not state.summary.get("pending_checkpoint")
            ):
                self._launch_step(state, lease, step)
            self._rebind_launched(state)
            self._restore_binding(state)

            snapshot = self._observe(state)
            self._check_identity(state, snapshot)
            if state.identity_checked and not state.identity_verified:
                return self._blocked(
                    state, Reason.INCORRECT_BUILD.value, "the running build is not the expected build", snapshot
                )
            self._evaluate_due(state, snapshot, checkpoint="run_start" if not state.steps else None)
            checkpoint = state.summary.get("pending_checkpoint")
            if checkpoint:
                self._evaluate_due(state, snapshot, checkpoint=str(checkpoint))
                checkpoint_step = next((item for item in spec.steps if item.step_id == checkpoint), None)
                if checkpoint_step and checkpoint_step.checkpoint and self.config.capture_checkpoints:
                    self._capture(
                        state, snapshot, checkpoint=str(checkpoint), description=f"checkpoint after {checkpoint}"
                    )
                state.summary.pop("pending_checkpoint", None)

            step = self._current_step(state)
            if step is None:
                return self._complete(state, snapshot)
            if step.operation is Operation.LAUNCH_APP:
                continue

            if state.actions >= spec.limits.max_actions:
                return self._pause(state, Reason.BUDGET_EXHAUSTED.value, {"budget": "max_actions"})
            if state.decisions >= spec.limits.max_model_decisions:
                return self._pause(state, Reason.BUDGET_EXHAUSTED.value, {"budget": "max_model_decisions"})

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

            if decision.operation is Operation.WAIT:
                self.journal.append_trace(state.run_id, "decision", decision.to_json())
                self.config.sleeper(min(1.5, max(0.2, state.slice_deadline - self.config.clock())))
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
            if step.step_id not in state.completed_steps:
                continue
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
            record
            for record in self.journal.actions_for_run(state.run_id)
            if record.state in RESERVED_DISPATCH_STATES
            or (
                record.state is DispatchState.DISPATCHED
                and record.action_id not in {step.receipt_action_id for step in state.steps}
            )
        ]
        if not unfinished:
            return
        for record in unfinished:
            self.journal.reconcile(
                record.action_id,
                DispatchState.UNCERTAIN,
                "no dispatch receipt; observation alone cannot establish whether input occurred",
            )
        state.uncertain = True
        raise Pause(Reason.UNCERTAIN_EFFECT, {"actions": [record.action_id for record in unfinished]})

    def _fingerprint_before(self, state: _RunState, action_id: str) -> str | None:
        for trace in reversed(self.journal.traces(state.run_id)):
            payload = trace.get("payload")
            if trace.get("kind") != "dispatch" or not isinstance(payload, Mapping):
                continue
            if payload.get("action_id") == action_id:
                return str(payload.get("fingerprint_before") or "")
        return None

    def _observe(self, state: _RunState, *, completion: bool = False) -> Snapshot:
        attempts = 0
        while True:
            if self.config.clock() >= state.slice_deadline:
                raise Pause(Reason.BUDGET_EXHAUSTED, self._deadline_budget(state))
            try:
                scope = self._scope(state)
                if completion:
                    scope = replace(scope, max_depth=max(scope.max_depth, 24))
                epoch = self._human_epoch()
                snapshot = self.driver.observe(scope)
                state.observed_epoch = epoch
                return snapshot
            except (DriverError, ContractError) as exc:
                attempts += 1
                if attempts > state.spec.limits.stale_retries:
                    raise Pause(Reason.STALE_OBSERVATION, {"attempts": attempts, "error": str(exc)}) from exc
                self.config.sleeper(0.3 * attempts)

    def _check_identity(self, state: _RunState, snapshot: Snapshot) -> None:
        report = self.driver.identity(self._scope(state).app_ref, state.spec.expected_identity)
        state.identity_checked = True
        state.identity_status = report.status.value
        state.identity_verified = report.status.value == "verified"
        self.journal.append_trace(state.run_id, "identity", report.to_json())
        state.summary["identity"] = report.to_json()

    def _build_contexts(self, state: _RunState, step: Any, snapshot: Snapshot) -> list[OpContext]:
        if step.operation is Operation.FOCUS_WINDOW:
            return [
                OpContext(
                    operation=Operation.FOCUS_WINDOW,
                    candidates=tuple(
                        TargetCandidate(
                            element_id=window.window_ref,
                            description=self._redact(window.title, self._secret_values(state)) or "application window",
                            operation=Operation.FOCUS_WINDOW,
                        )
                        for window in snapshot.windows
                        if window.visible and window.enabled
                    ),
                )
            ]
        if step.operation is Operation.HOTKEY:
            chord = self._fixture_value(state, step.fixture_reference) if step.fixture_reference else None
            if not chord:
                return []
            focused = next((window for window in snapshot.windows if window.focused), None)
            if focused is None:
                return []
            title = self._redact(focused.title, self._secret_values(state)) or "application window"
            return [
                OpContext(
                    operation=Operation.HOTKEY,
                    candidates=(
                        TargetCandidate(
                            element_id=new_id("cfg"),
                            description=f"Send caller-specified keyboard chord {chord!r} to focused window {title!r}",
                            operation=Operation.HOTKEY,
                            value=chord,
                        ),
                    ),
                    note="The caller explicitly requires this chord in the currently focused application window.",
                )
            ]
        observation = self._observation_for_policy(state, snapshot)
        operations = [step.operation]
        labels: dict[Operation, str] = {}
        if step.fixture_reference:
            value = self._fixture_value(state, step.fixture_reference)
            labels[step.operation] = f"step={step.step_id} fixture={step.fixture_reference}"
            if value is not None and step.operation is Operation.TYPE_TEXT:
                labels[step.operation] += f" text_length={len(value)}"
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
        remaining = state.slice_deadline - self.config.clock()
        if remaining <= 0:
            raise Pause(Reason.BUDGET_EXHAUSTED, self._deadline_budget(state))
        if state.decisions >= state.spec.limits.max_model_decisions:
            raise Pause(Reason.BUDGET_EXHAUSTED, {"budget": "max_model_decisions"})
        state.decisions += 1
        self._persist(state)
        try:
            decision = self.policy.decide(
                goal=state.spec.goal,
                state=with_permitted_operations(summarised, [step.operation]),
                contexts=contexts,
                allow_done=allow_done,
                allow_escalate=True,
                current_step={
                    "step_id": step.step_id,
                    "operation": step.operation.value,
                    "target_description": step.target_description,
                },
                **({"deadline": time.monotonic() + remaining} if isinstance(self.policy, JevPolicy) else {}),
            )
        finally:
            self._record_model_attempts(state)
        state.model_versions.append(decision.model)
        self.journal.append_trace(state.run_id, "decision", decision.to_json())
        return decision

    def _observation_for_policy(self, state: _RunState, snapshot: Snapshot) -> dict[str, Any]:
        secrets = self._secret_values(state)
        elements: list[dict[str, Any]] = []
        enabled_windows = {window.window_ref for window in snapshot.windows if window.enabled}
        for element in snapshot.elements:
            payload = {
                "index": element.index,
                "element_id": element.element_id,
                "window_ref": element.window_ref,
                "text": self._redact(element.text, secrets),
                "role": element.role,
                "name": self._redact(element.name, secrets),
                "value": self._redact(element.value, secrets),
                "enabled": element.enabled,
                "visible": element.visible,
                "editable": element.editable,
                "focused": element.focused,
                "operations": list(element.operations) if element.window_ref in enabled_windows else [],
                "path": [self._redact(part, secrets) for part in element.path],
                "state": dict(element.state),
                "truncation": element.truncation,
            }
            if element.state.get("password"):
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
                "window_titles": [self._redact(window.title, secrets) for window in snapshot.windows],
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
        *,
        task_scroll: Mapping[str, Any] | None = None,
    ) -> Snapshot | RunResult:
        spec = state.spec
        mode = spec.interaction_mode
        operation = decision.operation
        target: TargetCandidate | None = decision.target
        text: str | None = None
        option_label: str | None = None
        hotkey: tuple[str, ...] = ()
        scroll: dict[str, Any] = {}
        window_ref: str | None = None

        if operation is Operation.HOTKEY:
            hotkey = tuple(self._hotkey_for(state, step))
            focused = next((window for window in snapshot.windows if window.focused), None)
            if focused is None:
                return self._pause(state, Reason.NO_APPROPRIATE_TARGET.value, {"step": step.step_id})
            window_ref = focused.window_ref
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
                scroll = dict(task_scroll or {"notches": 3})

        if target is not None and operation is Operation.FOCUS_WINDOW:
            window_ref = target.element_id
        elif target is not None and operation not in CONTROL_OPERATIONS:
            window_ref = snapshot.element(target.element_id).window_ref
        elif window_ref is None and snapshot.windows:
            window_ref = snapshot.windows[0].window_ref

        action_id = new_id("act")
        request = ActionRequest(
            action_id=action_id,
            run_id=state.run_id,
            operation=operation,
            mode=mode,
            element_id=target.element_id
            if target and operation not in CONTROL_OPERATIONS | {Operation.FOCUS_WINDOW}
            else None,
            snapshot_id=snapshot.snapshot_id,
            window_ref=window_ref,
            lease_generation=lease.generation,
            step_id=step.step_id,
            text=text,
            replace_existing=getattr(step, "replace_existing", True),
            option_label=option_label,
            hotkey=hotkey,
            scroll=scroll,
            deadline_s=min(15.0, max(3.0, state.spec.limits.slice_seconds)),
        )
        request = replace(request, request_hash=self._request_hash(state, request))

        def guard() -> None:
            if self.config.clock() >= state.slice_deadline:
                raise Pause(Reason.BUDGET_EXHAUSTED, self._deadline_budget(state))
            if self._human_epoch() != state.observed_epoch:
                # The decision rests on a screen the user has since touched.
                raise Pause(Reason.USER_TAKEOVER, {"reason": "physical input since observation", "human_input": True})
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
                send=lambda: self.driver.execute(request, guard, snapshot),
            )
        except Pause as pause:
            if pause.detail.get("human_input"):
                # Nothing was sent; the loop waits for the user, observes again, and continues.
                self.journal.append_trace(state.run_id, "human_input_before_dispatch", {"action_id": action_id})
                return snapshot
            if pause.reason_value in {Reason.STALE_OBSERVATION.value, Reason.LOW_CONFIDENCE.value}:
                state.stale_retries += 1
                if state.stale_retries > spec.limits.stale_retries:
                    if self.config.capture_failures:
                        self._capture(
                            state, snapshot, checkpoint=step.step_id, description=pause.reason_value, keep=True
                        )
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
        self._record_step(
            state,
            request,
            receipt,
            step,
            target.description if target else operation.value,
            DispatchState.DISPATCHED,
        )
        self._advance_step(state, step, receipt)
        state.summary["pending_checkpoint"] = step.step_id
        # Persist acknowledged input before any fallible observation or evidence work.
        self._persist(state)
        if self._launch_follows(state, step):
            return snapshot
        fresh = self._observe_settled(state, before=snapshot.fingerprint)
        changed = fresh.fingerprint != snapshot.fingerprint
        state.steps[-1] = replace(state.steps[-1], observation_changed=changed)
        if changed:
            state.no_progress = 0
            state.stale_retries = 0
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

    def _launch(self, state: _RunState, request: ActionRequest) -> Receipt:
        from .drivers.windows.win32 import process_creation_time

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
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            creation_time = process_creation_time(process.pid)
        except (OSError, DriverError) as exc:
            raise UncertainEffect(f"launch failed: {exc}", mechanism=DispatchMechanism.NONE) from exc
        self.journal.append_trace(state.run_id, "launch", {"pid": process.pid, "config": request.launch_config_id})
        return Receipt(
            action_id=request.action_id,
            dispatch_state=DispatchState.DISPATCHED,
            mechanism=DispatchMechanism.NONE,
            inserted_events=1,
            started_at=started,
            finished_at=self.config.clock(),
            target={"launch_config_id": request.launch_config_id, "pid": process.pid, "creation_time": creation_time},
            notes=("process launched; identity must be re-verified before accepting results",),
        )

    def _scope(self, state: _RunState):
        # A rebinding clears window references only when the spec had none or a launch replaced its process.
        app_ref = state.summary.get("bound_app_ref")
        return replace(state.spec.scope, app_ref=app_ref, window_refs=()) if app_ref else state.spec.scope

    def _launch_follows(self, state: _RunState, step: Any) -> bool:
        following = self._current_step(state)
        return (
            following is not None
            and following.operation is Operation.LAUNCH_APP
            and not any(item.checkpoint in {step.step_id, "any"} for item in state.spec.assertions)
        )

    def _launch_step(self, state: _RunState, lease: Lease, step: Any) -> None:
        if state.completed_steps and not state.spec.allow_restart:
            raise ContractError("relaunch requires allow_restart in the frozen specification")
        if state.actions >= state.spec.limits.max_actions:
            raise Pause(Reason.BUDGET_EXHAUSTED, {"budget": "max_actions"})
        config_id = state.spec.launch_config_id
        if config_id not in self.config.launch_configs:
            raise ContractError("LAUNCH_APP requires the specification's approved launch_config_id")
        request = ActionRequest(
            action_id=new_id("act"),
            run_id=state.run_id,
            operation=Operation.LAUNCH_APP,
            mode=state.spec.interaction_mode,
            element_id=None,
            snapshot_id=None,
            window_ref=None,
            lease_generation=lease.generation,
            step_id=step.step_id,
            launch_config_id=config_id,
        )
        request = replace(request, request_hash=self._request_hash(state, request))

        def guard() -> None:
            if self.config.clock() >= state.slice_deadline:
                raise Pause(Reason.BUDGET_EXHAUSTED, self._deadline_budget(state))
            self.ownership.checkpoint(
                run_id=state.run_id, lease_id=lease.lease_id, generation=lease.generation, session_id=lease.session_id
            )

        receipt = self.journal.dispatch_once(
            action_id=request.action_id,
            request_hash=request.request_hash,
            run_id=state.run_id,
            guard=guard,
            send=lambda: self._launch(state, request),
        )
        state.actions += 1
        self._record_step(state, request, receipt, step, step.target_description, DispatchState.DISPATCHED)
        self._advance_step(state, step, receipt)
        state.summary["launched_process"] = {
            "pid": receipt.target["pid"],
            "creation_time": receipt.target["creation_time"],
        }
        state.summary["pending_checkpoint"] = step.step_id
        state.identity_checked = state.identity_verified = False
        self._persist(state)

    def _rebind_launched(self, state: _RunState) -> None:
        launched = state.summary.get("launched_process")
        if not launched:
            return
        app_ref = self.driver.bind_process(int(launched["pid"]), float(launched["creation_time"]))
        state.summary["bound_app_ref"] = app_ref
        state.summary.pop("launched_process")
        state.summary.pop("requires_rebind", None)
        state.summary.pop("restore_binding", None)
        self._persist(state)

    def _restore_binding(self, state: _RunState) -> None:
        if not state.summary.get("restore_binding"):
            return
        launched = any(
            step.operation is Operation.LAUNCH_APP and step.step_id in state.completed_steps
            for step in state.spec.steps
        )
        if state.spec.scope.window_refs and not launched:
            # Window references do not survive a restart; rebinding the app alone would widen the frozen scope.
            raise Pause(
                Reason.STALE_OBSERVATION,
                {"detail": "window references expired with the broker restart; inspect again and create a new run"},
            )
        previous = state.summary.get("identity", {}).get("observed", {})
        if not previous:
            raise Pause(
                Reason.INCORRECT_BUILD, {"detail": "broker restarted before identity was recorded; create a new run"}
            )
        matches = [
            app
            for app in self.driver.list_apps()
            if app.process_id == previous.get("process_id")
            and app.process_creation_time == previous.get("process_creation_time")
        ]
        if len(matches) != 1:
            raise Pause(
                Reason.INCORRECT_BUILD, {"detail": "the previously verified process instance is no longer available"}
            )
        state.summary["bound_app_ref"] = matches[0].app_ref
        state.summary.pop("restore_binding")

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
        state.summary["pending_checkpoint"] = checkpoint
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
        state.summary.pop("pending_checkpoint", None)

    def _evaluate_one(self, state: _RunState, spec: AssertionSpec, snapshot: Snapshot, checkpoint: str) -> None:
        """Evaluate an assertion, honouring its deadline.

        A real application can take a moment to expose what the assertion looks for: a browser
        updates its window title after the page loads, not when the click is dispatched. The
        deadline lets the assertion wait for the state rather than testing the state at the
        instant the input landed.
        """
        if (
            spec.evaluator in {Evaluator.MODEL_VISUAL, Evaluator.CALLER_RESULT}
            and spec.assertion_id not in state.supplied_visual
        ):
            boundary = self._visual_boundary(state, snapshot, spec, checkpoint=checkpoint)
            raise Pause(Reason.NEEDS_VISUAL_ASSISTANCE, boundary.detail)
        if spec.assertion_id in state.supplied_visual and self._has_result(state, spec.assertion_id, terminal=True):
            return
        result = self._evaluate_once(state, spec, snapshot, checkpoint)
        deadlines = state.summary.setdefault("assertion_deadlines", {})
        deadline = deadlines.setdefault(
            f"{checkpoint}:{spec.assertion_id}", self.config.clock() + max(0.0, spec.deadline_s)
        )
        while result.status is not AssertionStatus.PASSED and self.config.clock() < deadline:
            if self.config.clock() >= state.slice_deadline:
                raise Pause(Reason.BUDGET_EXHAUSTED, self._deadline_budget(state))
            self.config.sleeper(min(0.25, deadline - self.config.clock(), state.slice_deadline - self.config.clock()))
            try:
                snapshot = self._observe(state)
            except Pause:
                raise
            result = self._evaluate_once(state, spec, snapshot, checkpoint)
        self._merge_assertion(state, result)
        self.journal.append_trace(state.run_id, "assertion", result.to_json())

    def _evaluate_once(
        self, state: _RunState, spec: AssertionSpec, snapshot: Snapshot, checkpoint: str
    ) -> AssertionResult:
        context = EvalContext(
            observation=snapshot,
            identity=IdentityReport.from_json(state.summary["identity"]) if state.summary.get("identity") else None,
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
        # An `any` assertion must hold after every action, so it keeps its worst evaluation.
        every_action = any(
            spec.assertion_id == result.assertion_id and spec.checkpoint == "any" for spec in state.spec.assertions
        )
        rank = worst_rank if every_action else report_rank
        for index, existing in enumerate(state.assertions):
            if existing.assertion_id != result.assertion_id:
                continue
            if existing.status is AssertionStatus.FAILED:
                return  # a proven failure is never erased by a later evaluation
            if rank(result) >= rank(existing):
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
            if spec.assertion_id != state.pending_assertion:
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

    def _pause(
        self, state: _RunState, reason: str, detail: Mapping[str, Any], snapshot: Snapshot | None = None
    ) -> RunResult:
        flags = self.ownership.flags(state.run_id)
        if flags.cancelled:
            return self._stopped(state, Reason.USER_TAKEOVER.value, flags.cancel_reason or "run cancelled")
        state.status = RunStatus.PAUSED.value
        state.pause_reason = reason
        state.pause_detail = dict(detail)
        verdict, _ = self._verdict(state, Execution.PAUSED.value)
        return self._result(state, Execution.PAUSED, verdict, reason, snapshot=snapshot, detail=detail)

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
        verdict, _ = self._verdict(state, Execution.BLOCKED.value)
        return self._result(
            state,
            Execution.BLOCKED,
            verdict,
            reason,
            detail={"message": message, **(dict(detail) if detail else {})},
        )

    def _error(self, state: _RunState, exc: BaseException) -> RunResult:
        if self.ownership.flags(state.run_id).cancelled:
            return self._stopped(state, Reason.USER_TAKEOVER.value, "run cancelled")
        state.status = RunStatus.ERROR.value
        state.pause_reason = "runner_error"
        verdict, _ = self._verdict(state, Execution.ERROR.value)
        return self._result(
            state,
            Execution.ERROR,
            verdict,
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
            return self._pause(
                state,
                Reason.NEEDS_VISUAL_ASSISTANCE.value,
                {
                    "step": step.step_id,
                    "operation": step.operation.value,
                    "truncation": list(snapshot.truncation),
                    "evidence_ref": capture.evidence.evidence_id if capture else None,
                },
                snapshot,
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

    def _visual_boundary(
        self, state: _RunState, snapshot: Snapshot, spec: AssertionSpec | None, *, checkpoint: str | None = None
    ) -> RunResult:
        checkpoint = checkpoint or (spec.checkpoint if spec else state.current_step_id)
        capture = self._capture(
            state,
            snapshot,
            checkpoint=checkpoint,
            description="visual assistance requested",
            keep=True,
        )
        state.pending_assertion = spec.assertion_id if spec else None
        return self._pause(
            state,
            Reason.NEEDS_VISUAL_ASSISTANCE.value,
            {
                "assertion_id": spec.assertion_id if spec else None,
                "checkpoint": checkpoint,
                "snapshot_id": snapshot.snapshot_id,
                "evidence_ref": capture.evidence.evidence_id if capture else None,
                "coordinate_transform": {
                    "source_rect": capture.source_rect.to_json() if capture else None,
                    "scale": capture.scale if capture else None,
                    "geometry_epoch": snapshot.geometry.epoch,
                },
            },
            snapshot,
        )

    # ------------------------------------------------------------------------------
    # Evidence and persistence
    # ------------------------------------------------------------------------------

    def _capture(
        self, state: _RunState, snapshot: Snapshot, *, checkpoint: str | None, description: str, keep: bool = False
    ) -> Any:
        if self._secret_values(state):
            self.journal.append_trace(state.run_id, "capture_withheld", {"reason": "run contains secret fixtures"})
            return None
        try:
            capture = self.driver.capture(
                scope=self._scope(state),
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
            self.evidence.retain(capture.evidence.evidence_id)
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
            "interval_ms": snapshot.interval_ms,
            "windows": [window.to_json() for window in snapshot.windows],
            **caller_view(snapshot, limit=120),
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

    def _deadline_budget(self, state: _RunState) -> dict[str, str]:
        """Name the deadline that expired; only a slice deadline is worth resuming."""
        return {"budget": "run_deadline" if self.config.clock() >= self._run_deadline(state) else "slice_deadline"}

    def _run_deadline(self, state: _RunState) -> float:
        """The frozen run deadline, extended by the time spent waiting for the user."""
        return state.started_at + state.spec.limits.deadline_seconds + float(state.summary.get("hold_seconds", 0.0))

    def _human_epoch(self) -> int:
        return int(self.presence.human_epoch) if self.presence is not None else 0

    def _hold_for_user(self, state: _RunState, lease: Lease) -> bool:
        """Wait while the user is using the desktop; True means observe again before acting.

        Waiting extends the run deadline (up to max_hold_seconds in total) but never the
        per-call limit, which must stay under the client's tool timeout.
        """
        if self.presence is None:
            return False
        grace = self.config.human_grace_seconds
        if self._human_epoch() == state.observed_epoch and self.presence.idle_seconds() >= grace:
            return False
        self.journal.append_trace(state.run_id, "holding_for_user", {"grace_s": grace})
        started = self.config.clock()
        held = float(state.summary.get("hold_seconds", 0.0))
        try:
            while (idle := self.presence.idle_seconds()) < grace:
                now = self.config.clock()
                if now >= state.slice_limit or held + (now - started) >= self.config.max_hold_seconds:
                    raise Pause(Reason.USER_TAKEOVER, {"reason": "the desktop is in use", "resumable": True})
                self.ownership.checkpoint(
                    run_id=state.run_id,
                    lease_id=lease.lease_id,
                    generation=lease.generation,
                    session_id=lease.session_id,
                )
                self.config.sleeper(min(0.25, grace - idle))
        finally:
            state.summary["hold_seconds"] = min(self.config.max_hold_seconds, held + self.config.clock() - started)
            state.slice_deadline = min(state.slice_limit, self._run_deadline(state))
            self._persist(state)
        return True

    def _on_escape(self) -> None:
        """Physical Esc stops the run that holds the desktop. Called off the input hook thread."""
        lease = self.ownership.active_lease()
        if lease is None:
            return
        self.ownership.request_cancel(lease.run_id, "Esc pressed")
        self.journal.append_trace(lease.run_id, "escape", {})

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
        if state.spec.frozen_digest() != spec.frozen_digest():
            raise ContractError("stored runtime specification differs from its frozen specification")
        for reference in self.journal.evidence_for_run(run_id):
            if Path(str(reference.get("path", ""))).is_file():
                self.evidence.register(EvidenceRef.from_json(reference))
        if hasattr(self.driver, "bind_process"):
            state.summary["restore_binding"] = True
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
        if state.spec.purpose is Purpose.TASK or result.execution in {Execution.COMPLETED, Execution.CANCELLED}:
            lease = self.ownership.active_lease()
            if lease is not None and lease.run_id == state.run_id:
                self.ownership.release(lease.lease_id)

    # ------------------------------------------------------------------------------
    # Inputs, fixtures, helpers
    # ------------------------------------------------------------------------------

    def _apply_inputs(self, state: _RunState, inputs: ResumeInputs) -> None:
        # Validate every input before recording any of it, so a rejected resume changes nothing.
        allowed_fixtures = set(state.spec.fixtures) | set(state.spec.secret_refs)
        recorded = state.summary.get("fixture_fingerprints", {})
        fingerprints: dict[str, str] = {}
        for name, value in inputs.fixtures.items():
            if name not in allowed_fixtures:
                raise ContractError(f"resume may not introduce fixture {name!r}")
            current = self._fixture_value(state, name)
            if current is not None and current != value:
                raise ContractError(f"resume may not rewrite fixture {name!r}")
            fingerprint = keyed_fingerprint(self.config.fingerprint_secret, value)
            if name in recorded and recorded[name] != fingerprint:
                raise ContractError(f"resume may not rewrite fixture {name!r} after restart")
            fingerprints[name] = fingerprint

        assertion_ids = {
            spec.assertion_id for spec in state.spec.assertions if spec.evaluator is Evaluator.CALLER_RESULT
        }
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
        results = {**inputs.visual_results, **inputs.verifier_results}
        if any(assertion_id != state.pending_assertion for assertion_id in results):
            raise ContractError("evaluator result was not requested at this checkpoint")

        state.summary.setdefault("fixture_fingerprints", {}).update(fingerprints)
        state.supplied_fixtures.update(inputs.fixtures)
        for assertion_id, payload in results.items():
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
                    checkpoint=str(state.pause_detail.get("checkpoint") or spec.checkpoint),
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
        values.extend(
            literal
            for name, literal in state.spec.fixtures.items()
            if name.startswith("secret:") and isinstance(literal, str) and literal
        )
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
        if spec.limits.deadline_seconds <= 0 or spec.limits.slice_seconds <= 0:
            raise ContractError("run and slice deadlines must be positive")
        if spec.purpose is Purpose.TASK:
            if spec.steps or spec.assertions or spec.secret_refs:
                raise ContractError("tasks use a goal and literal inputs, not test steps or assertions")
            if not spec.scope.window_refs:
                raise ContractError("tasks require explicit window references")
            if len(spec.fixtures) > 24 or any(
                not isinstance(name, str)
                or not name.startswith(("text:", "secret:", "key:"))
                or not isinstance(value, str)
                for name, value in spec.fixtures.items()
            ):
                raise ContractError("invalid task inputs")
        if spec.scope.app_ref != spec.app_ref:
            raise ContractError("observation scope must match the application whose build is verified")
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
            validate_assertion(item)
        for position, step in enumerate(spec.steps):
            if step.operation in {Operation.TYPE_TEXT, Operation.SELECT} and not step.fixture_reference:
                raise ContractError(f"step {step.step_id} needs a fixture_reference")
            for dependency in step.depends_on:
                if dependency not in step_ids[:position]:
                    raise ContractError(f"step {step.step_id} depends on unknown or later step {dependency}")


def report_rank(result: AssertionResult) -> int:
    """Ordering used when several evaluations exist for one assertion."""
    order = {
        AssertionStatus.FAILED: 3,
        AssertionStatus.PASSED: 2,
        AssertionStatus.INCONCLUSIVE: 1,
        AssertionStatus.NOT_EVALUATED: 0,
    }
    return order.get(result.status, 0)


def worst_rank(result: AssertionResult) -> int:
    """Ordering for assertions that must hold at every evaluation."""
    order = {
        AssertionStatus.FAILED: 3,
        AssertionStatus.INCONCLUSIVE: 2,
        AssertionStatus.PASSED: 1,
        AssertionStatus.NOT_EVALUATED: 0,
    }
    return order.get(result.status, 0)
