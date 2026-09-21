"""Bounded runtime behaviour: checkpoints, budgets, resume rules, and no unattended replay."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from jev_desktop.contracts import (
    ContractError,
    Execution,
    InputMode,
    Limits,
    Operation,
    Reason,
    RunResult,
    RunSpec,
    Verdict,
)
from jev_desktop.evidence import EvidenceStore
from jev_desktop.journal import DispatchJournal, DispatchState
from jev_desktop.ownership import Ownership
from jev_desktop.runtime import ResumeInputs, Runtime, RuntimeConfig

from .fakes import Clock, FakeApp, FakeDriver, FakeElement, ScriptedDecision, ScriptedPolicy

APP_REF = "app:" + "a" * 24
_OWNERS: list[Ownership] = []


@pytest.fixture(autouse=True)
def release_test_leases():
    yield
    for owner in _OWNERS:
        owner.force_release()
    _OWNERS.clear()


def build(
    tmp_path: Path,
    *,
    elements: list[FakeElement],
    steps: list[dict],
    assertions: list[dict] | None = None,
    fixtures: dict | None = None,
    limits: dict | None = None,
    purpose: str = "regression",
    mode: str = "user_path",
    script: list[ScriptedDecision] | None = None,
    identity_status: str | None = None,
):
    app = FakeApp(app_ref=APP_REF, window_ref="win:" + "b" * 24, elements=elements)
    driver = FakeDriver(app, evidence_dir=tmp_path / "evidence")
    if identity_status:
        from jev_desktop.contracts import IdentityStatus

        driver.identity_status = IdentityStatus(identity_status)
    clock = Clock()
    journal = DispatchJournal(str(tmp_path / "journal.sqlite"))
    ownership = Ownership(clock=clock)
    _OWNERS.append(ownership)
    evidence = EvidenceStore(root=tmp_path / "evidence", approved_roots=[str(tmp_path)])
    config = RuntimeConfig(
        evidence_dir=tmp_path / "evidence",
        approved_roots=(str(tmp_path),),
        sleeper=clock.sleep,
        clock=clock,
    )
    policy = ScriptedPolicy(script or [])
    runtime = Runtime(
        driver=driver, journal=journal, ownership=ownership, evidence=evidence, config=config, policy=policy
    )
    spec = RunSpec.from_json(
        {
            "goal": "regression: save the document",
            "purpose": purpose,
            "interaction_mode": mode,
            "app_ref": APP_REF,
            "expected_identity": {"mode": "exe_hash", "expect_sha256": None},
            "launch_config_id": None,
            "steps": steps,
            "assertions": assertions or [],
            "fixtures": fixtures or {},
            "secret_refs": {},
            "limits": limits or Limits.defaults().to_json(),
            "scope": {"app_ref": APP_REF, "window_refs": [app.window_ref], "max_elements": 60},
            "allow_restart": False,
        }
    )
    session = ownership.create_session("test")
    created = runtime.create_run(spec, session_id=session.session_id)
    ownership.acquire(session.session_id, created["run_id"])
    return runtime, driver, app, clock, journal, ownership, session, created


def slice_once(runtime, ownership, session, created, *, resume_token=None, inputs=None, seconds=5.0) -> RunResult:
    return runtime.slice(
        run_id=created["run_id"],
        session_id=session.session_id,
        resume_token=resume_token or created["resume_token"],
        inputs=inputs,
        slice_seconds=seconds,
    )


SAVE_ELEMENTS = [
    FakeElement("button", "Save", operations=("CLICK",)),
    FakeElement("text", "Saved", value="no", text="no", operations=()),
]


def test_happy_path_completes_with_a_pass(tmp_path):
    runtime, driver, _app, _clock, journal, ownership, session, created = build(
        tmp_path,
        elements=SAVE_ELEMENTS,
        steps=[{"step_id": "save", "operation": "CLICK", "target_description": "the Save button", "checkpoint": True}],
        assertions=[
            {
                "assertion_id": "saved-flag",
                "evaluator": "uia_property",
                "target": {"role": "text", "name": "Saved"},
                "property": "value",
                "expected": {"equals": "yes"},
                "checkpoint": "save",
            }
        ],
        script=[ScriptedDecision(Operation.CLICK, "Save")],
    )
    result = slice_once(runtime, ownership, session, created)
    assert result.execution is Execution.COMPLETED
    assert result.verdict is Verdict.PASSED
    assert [step.operation for step in result.steps] == [Operation.CLICK]
    assert result.steps[0].observation_changed is True
    assert len(driver.executed) == 1
    assert result.budgets["actions"] == 1
    journal.close()


def test_missing_fixture_pauses_then_resume_supplies_it(tmp_path):
    elements = [FakeElement("edit", "Name", value="", editable=True, operations=("TYPE_TEXT", "CLICK"))]
    runtime, driver, _app, _clock, journal, ownership, session, created = build(
        tmp_path,
        elements=elements,
        steps=[
            {
                "step_id": "type-name",
                "operation": "TYPE_TEXT",
                "target_description": "the Name field",
                "fixture_reference": "name_value",
            }
        ],
        fixtures={"name_value": None},
        script=[ScriptedDecision(Operation.TYPE_TEXT, "Name")],
    )
    paused = slice_once(runtime, ownership, session, created)
    assert paused.execution is Execution.PAUSED and paused.reason == Reason.NEEDS_TEXT.value
    assert not driver.executed, "nothing may be typed without a caller fixture"

    resumed = slice_once(
        runtime,
        ownership,
        session,
        created,
        resume_token=paused.resume_token,
        inputs=ResumeInputs(fixtures={"name_value": "Ada Lovelace"}),
    )
    assert resumed.execution is Execution.COMPLETED
    assert driver.executed[0].text == "Ada Lovelace"
    journal.close()


def test_resume_may_not_introduce_a_new_fixture(tmp_path):
    runtime, _driver, _app, _clock, journal, ownership, session, created = build(
        tmp_path,
        elements=SAVE_ELEMENTS,
        steps=[{"step_id": "save", "operation": "CLICK", "target_description": "Save"}],
        fixtures={"declared": "value"},
        script=[ScriptedDecision(Operation.CLICK, "Save")],
    )
    with pytest.raises(ContractError):
        slice_once(runtime, ownership, session, created, inputs=ResumeInputs(fixtures={"undeclared": "x"}))
    journal.close()


def test_resume_rejects_visual_results_for_a_non_visual_assertion(tmp_path):
    runtime, _driver, _app, _clock, journal, ownership, session, created = build(
        tmp_path,
        elements=SAVE_ELEMENTS,
        steps=[{"step_id": "save", "operation": "CLICK", "target_description": "Save"}],
        assertions=[
            {
                "assertion_id": "saved-flag",
                "evaluator": "uia_property",
                "target": {"role": "text", "name": "Saved"},
                "property": "value",
                "expected": {"equals": "yes"},
                "checkpoint": "save",
            }
        ],
        script=[ScriptedDecision(Operation.CLICK, "Save")],
    )
    with pytest.raises(ContractError):
        slice_once(
            runtime,
            ownership,
            session,
            created,
            inputs=ResumeInputs(visual_results={"saved-flag": {"status": "passed"}}),
        )
    journal.close()


def test_model_cannot_declare_done_while_required_steps_remain(tmp_path):
    runtime, driver, _app, _clock, journal, ownership, session, created = build(
        tmp_path,
        elements=SAVE_ELEMENTS,
        steps=[{"step_id": "save", "operation": "CLICK", "target_description": "Save"}],
        script=[ScriptedDecision(Operation.DONE)],
    )
    result = slice_once(runtime, ownership, session, created)
    assert result.execution is Execution.PAUSED and result.reason == Reason.STEP_UNRESOLVED.value
    assert result.verdict is Verdict.INCONCLUSIVE
    assert not driver.executed
    journal.close()


def test_action_budget_stops_the_slice_with_a_resumable_checkpoint(tmp_path):
    elements = [
        FakeElement("button", "One", operations=("CLICK",)),
        FakeElement("button", "Two", operations=("CLICK",)),
    ]
    runtime, driver, _app, _clock, journal, ownership, session, created = build(
        tmp_path,
        elements=elements,
        steps=[
            {"step_id": "one", "operation": "CLICK", "target_description": "One"},
            {"step_id": "two", "operation": "CLICK", "target_description": "Two"},
        ],
        limits={**Limits.defaults().to_json(), "max_actions": 1},
        script=[ScriptedDecision(Operation.CLICK, "One"), ScriptedDecision(Operation.CLICK, "Two")],
    )
    result = slice_once(runtime, ownership, session, created)
    assert result.execution is Execution.PAUSED
    assert result.reason == Reason.BUDGET_EXHAUSTED.value
    assert result.detail["budget"] == "max_actions"
    assert result.resume_token and len(driver.executed) == 1
    journal.close()


def test_no_progress_is_bounded_and_pauses(tmp_path):
    elements = [
        FakeElement("button", "Inert one", operations=("CLICK",)),
        FakeElement("button", "Inert two", operations=("CLICK",)),
    ]
    runtime, _driver, _app, _clock, journal, ownership, session, created = build(
        tmp_path,
        elements=elements,
        steps=[
            {"step_id": "noop-1", "operation": "CLICK", "target_description": "Inert one"},
            {"step_id": "noop-2", "operation": "CLICK", "target_description": "Inert two"},
        ],
        limits={**Limits.defaults().to_json(), "no_progress_retries": 1},
        script=[ScriptedDecision(Operation.CLICK, "Inert one"), ScriptedDecision(Operation.CLICK, "Inert two")],
    )
    result = slice_once(runtime, ownership, session, created)
    assert result.execution is Execution.PAUSED and result.reason == Reason.STEP_UNRESOLVED.value
    assert result.detail["attempts"] == 2
    journal.close()


def test_uncertain_dispatch_pauses_and_is_never_replayed(tmp_path):
    runtime, driver, _app, _clock, journal, ownership, session, created = build(
        tmp_path,
        elements=SAVE_ELEMENTS,
        steps=[{"step_id": "save", "operation": "CLICK", "target_description": "Save"}],
        script=[ScriptedDecision(Operation.CLICK, "Save"), ScriptedDecision(Operation.CLICK, "Save")],
    )
    driver.fail_next = "uncertain"
    first = slice_once(runtime, ownership, session, created)
    assert first.execution is Execution.PAUSED and first.reason == Reason.UNCERTAIN_EFFECT.value
    assert first.verdict is Verdict.INCONCLUSIVE

    unfinished = [
        record
        for record in journal.actions_for_run(created["run_id"])
        if record.state in {DispatchState.UNCERTAIN, DispatchState.DISPATCHING}
    ]
    assert len(unfinished) == 1

    second = slice_once(runtime, ownership, session, created, resume_token=first.resume_token)
    # Neither changed nor unchanged pixels establish whether an effect landed.
    reconciled = journal.lookup(unfinished[0].action_id)
    assert reconciled.state is DispatchState.UNCERTAIN
    assert second.reason == Reason.UNCERTAIN_EFFECT.value
    assert len(journal.actions_for_run(created["run_id"])) == 1
    assert second.verdict is Verdict.INCONCLUSIVE, "an unreceipted effect can never be a pass"
    journal.close()


def test_guard_failure_before_input_records_not_dispatched(tmp_path):
    runtime, driver, _app, _clock, journal, ownership, session, created = build(
        tmp_path,
        elements=SAVE_ELEMENTS,
        steps=[{"step_id": "save", "operation": "CLICK", "target_description": "Save"}],
        script=[ScriptedDecision(Operation.CLICK, "Save")],
    )
    driver.fail_next = "before"
    result = slice_once(runtime, ownership, session, created)
    assert result.execution is Execution.ERROR
    assert result.verdict is Verdict.INCONCLUSIVE
    assert journal.actions_for_run(created["run_id"])[0].state is DispatchState.NOT_DISPATCHED
    journal.close()


def test_interaction_mode_is_never_switched_by_the_runner(tmp_path):
    elements = [FakeElement("button", "Save", operations=("CLICK",))]
    runtime, driver, _app, _clock, journal, ownership, session, created = build(
        tmp_path,
        elements=elements,
        steps=[{"step_id": "save", "operation": "CLICK", "target_description": "Save"}],
        mode="user_path",
        script=[ScriptedDecision(Operation.CLICK, "Save")],
    )
    slice_once(runtime, ownership, session, created)
    assert driver.executed and all(action.mode is InputMode.USER_PATH for action in driver.executed)
    journal.close()


def test_incorrect_build_blocks_the_run_before_any_action(tmp_path):
    runtime, driver, _app, _clock, journal, ownership, session, created = build(
        tmp_path,
        elements=SAVE_ELEMENTS,
        steps=[{"step_id": "save", "operation": "CLICK", "target_description": "Save"}],
        script=[ScriptedDecision(Operation.CLICK, "Save")],
        identity_status="mismatch",
    )
    result = slice_once(runtime, ownership, session, created)
    assert result.execution is Execution.BLOCKED and result.reason == Reason.INCORRECT_BUILD.value
    assert result.verdict is Verdict.INCONCLUSIVE
    assert not driver.executed
    journal.close()


def test_a_run_without_assertions_can_never_pass(tmp_path):
    runtime, _driver, _app, _clock, journal, ownership, session, created = build(
        tmp_path,
        elements=SAVE_ELEMENTS,
        steps=[{"step_id": "save", "operation": "CLICK", "target_description": "Save"}],
        script=[ScriptedDecision(Operation.CLICK, "Save")],
    )
    result = slice_once(runtime, ownership, session, created)
    assert result.execution is Execution.COMPLETED
    assert result.verdict is Verdict.INCONCLUSIVE
    journal.close()


def test_cancelled_run_releases_and_reports_cancelled(tmp_path):
    runtime, driver, _app, _clock, journal, ownership, session, created = build(
        tmp_path,
        elements=SAVE_ELEMENTS,
        steps=[{"step_id": "save", "operation": "CLICK", "target_description": "Save"}],
        script=[ScriptedDecision(Operation.CLICK, "Save")],
    )
    ownership.request_cancel(created["run_id"], "user pressed stop")
    result = slice_once(runtime, ownership, session, created)
    assert result.execution is Execution.CANCELLED
    assert not driver.executed
    journal.close()


def test_launch_passes_the_run_id_to_the_approved_configuration(tmp_path):
    """A launched application must be able to echo the run id into its own artifacts."""
    from dataclasses import replace as dataclass_replace

    marker = tmp_path / "launched-run-id.txt"
    script = "import os, pathlib, sys; pathlib.Path(sys.argv[1]).write_text(os.environ.get('JEV_DESKTOP_RUN_ID', ''))"
    runtime, _driver, _app, _clock, journal, _ownership, _session, created = build(
        tmp_path,
        elements=SAVE_ELEMENTS,
        steps=[{"step_id": "launch", "operation": "LAUNCH_APP", "target_description": "the application"}],
        script=[ScriptedDecision(Operation.LAUNCH_APP, None)],
    )
    runtime.config = dataclass_replace(
        runtime.config,
        launch_configs={"echo": {"executable": sys.executable, "args": ["-c", script, str(marker)]}},
    )
    from jev_desktop.contracts import ActionRequest, new_id

    request = ActionRequest(
        action_id=new_id("act"),
        run_id=created["run_id"],
        operation=Operation.LAUNCH_APP,
        mode=InputMode.USER_PATH,
        element_id=None,
        snapshot_id=None,
        window_ref=None,
        lease_generation=1,
        launch_config_id="echo",
    )
    receipt = runtime._launch(runtime._load(created["run_id"]), request)
    assert receipt.target["pid"] > 0
    deadline = time.monotonic() + 10.0
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert marker.exists(), "the launch step never ran the approved configuration"
    assert marker.read_text(encoding="utf-8") == created["run_id"]
    journal.close()


def test_visual_assertion_pauses_with_a_scoped_screenshot(tmp_path):
    runtime, driver, _app, _clock, journal, ownership, session, created = build(
        tmp_path,
        elements=SAVE_ELEMENTS,
        steps=[{"step_id": "save", "operation": "CLICK", "target_description": "Save"}],
        assertions=[
            {
                "assertion_id": "looks-right",
                "evaluator": "model_visual",
                "target": {},
                "expected": {},
                "oracle": "caller",
                "checkpoint": "any",
            }
        ],
        script=[ScriptedDecision(Operation.CLICK, "Save")],
    )
    result = slice_once(runtime, ownership, session, created)
    assert result.execution is Execution.PAUSED
    assert result.reason == Reason.NEEDS_VISUAL_ASSISTANCE.value
    assert result.detail["assertion_id"] == "looks-right"
    assert result.detail["coordinate_transform"]["scale"]
    assert any(reference.kind == "screenshot" for reference in result.evidence)
    assert len(driver.executed) == 1, "visual assistance must observe the specified checkpoint after acting"

    supplied = slice_once(
        runtime,
        ownership,
        session,
        created,
        resume_token=result.resume_token,
        inputs=ResumeInputs(
            visual_results={
                "looks-right": {
                    "status": "passed",
                    "observed": {"note": "the saved banner is visible"},
                    "evidence_refs": [result.evidence[0].evidence_id],
                }
            }
        ),
    )
    assert supplied.execution is Execution.COMPLETED
    # The caller explicitly selected a visual oracle in the immutable specification, so its
    # judgment can count, but only as a model-assessed result, never as a deterministic one.
    assert supplied.verdict is Verdict.PASSED
    assessed = [result for result in supplied.assertions if result.assertion_id == "looks-right"]
    assert assessed and assessed[0].label == "model_assessed"
    assert all(
        result.label == "deterministic" for result in supplied.assertions if result.assertion_id != "looks-right"
    )
    journal.close()


def test_state_survives_a_broker_restart(tmp_path):
    runtime, _driver, _app, clock, journal, ownership, session, created = build(
        tmp_path,
        elements=SAVE_ELEMENTS,
        steps=[{"step_id": "save", "operation": "CLICK", "target_description": "Save"}],
        limits={**Limits.defaults().to_json(), "slice_seconds": 0.5},
        script=[ScriptedDecision(Operation.WAIT), ScriptedDecision(Operation.WAIT)],
    )
    first = slice_once(runtime, ownership, session, created, seconds=None)
    assert first.execution is Execution.PAUSED and first.reason == Reason.BUDGET_EXHAUSTED.value
    journal.close()

    ownership.force_release()  # process exit releases its OS handle
    # New journal handle and runtime, same run id and resume token.
    app2 = FakeApp(app_ref=APP_REF, window_ref="win:" + "b" * 24, elements=SAVE_ELEMENTS)
    driver2 = FakeDriver(app2, evidence_dir=tmp_path / "evidence")
    journal2 = DispatchJournal(str(tmp_path / "journal.sqlite"))
    ownership2 = Ownership(clock=clock)
    evidence2 = EvidenceStore(root=tmp_path / "evidence", approved_roots=[str(tmp_path)])
    runtime2 = Runtime(
        driver=driver2,
        journal=journal2,
        ownership=ownership2,
        evidence=evidence2,
        config=RuntimeConfig(
            evidence_dir=tmp_path / "evidence", approved_roots=(str(tmp_path),), sleeper=clock.sleep, clock=clock
        ),
        policy=ScriptedPolicy([ScriptedDecision(Operation.CLICK, "Save")]),
    )
    session2 = ownership2.create_session("test-after-restart")
    ownership2.acquire(session2.session_id, created["run_id"])
    second = runtime2.slice(
        run_id=created["run_id"], session_id=session2.session_id, resume_token=first.resume_token, slice_seconds=5.0
    )
    assert second.execution is Execution.COMPLETED
    row = json.loads(str(journal2.get_run(created["run_id"])["state_json"]))
    assert row["completed_steps"] == ["save"]
    journal2.close()


@pytest.mark.parametrize("uncertain", [False, True], ids=["local-loop", "uncertain-input-stops"])
def test_goal_task_runs_locally_and_never_replays_uncertain_input(tmp_path, uncertain):
    runtime, driver, app, _clock, journal, ownership, session, created = build(
        tmp_path,
        elements=[FakeElement("edit", "Name", editable=True, operations=("TYPE_TEXT",)), *SAVE_ELEMENTS],
        steps=[],
        purpose="task",
        fixtures={"text:name": "Ada"},
        script=[
            ScriptedDecision(Operation.TYPE_TEXT, "Name"),
            ScriptedDecision(Operation.CLICK, "Save"),
            ScriptedDecision(Operation.DONE),
        ],
    )
    if uncertain:
        driver.fail_next = "uncertain"
    result = slice_once(runtime, ownership, session, created)
    assert ownership.active_lease() is None
    if uncertain:
        assert result.reason == "uncertain_effect"
        assert len(driver.executed) == 1
        ownership.acquire(session.session_id, created["run_id"])
        resumed = slice_once(runtime, ownership, session, created, resume_token=result.resume_token)
        assert resumed.reason == "uncertain_effect"
        assert len(driver.executed) == 1
    else:
        assert result.execution is Execution.COMPLETED
        assert len(driver.executed) == 2
        assert app.find("Name").value == "Ada"
        assert app.find("Saved").value == "yes"
        assert result.budgets["decisions"] == 3
        assert result.detail["completion"] == "model_reported"
        assert driver.identity_checked == 0
        assert driver.capture_calls == 0
        calls = runtime.policy.calls
        assert calls[0]["state"]["supplied_text"] == {"name": "Ada"}
        assert not any(context.operation is Operation.TYPE_TEXT for context in calls[1]["contexts"])
    journal.close()


def test_task_reobserves_after_stale_target_refusal(tmp_path):
    runtime, driver, _app, _clock, journal, ownership, session, created = build(
        tmp_path,
        elements=SAVE_ELEMENTS,
        steps=[],
        purpose="task",
        script=[
            ScriptedDecision(Operation.CLICK, "Save"),
            ScriptedDecision(Operation.CLICK, "Save"),
            ScriptedDecision(Operation.DONE),
        ],
    )
    driver.fail_next = "pause:stale_observation"
    result = slice_once(runtime, ownership, session, created)
    assert result.execution is Execution.COMPLETED
    calls = runtime.policy.calls
    first = calls[0]["contexts"][0].candidates[0].element_id
    second = calls[1]["contexts"][0].candidates[0].element_id
    assert first != second, "a stale refusal must get fresh observed targets before retrying"
    assert result.budgets["actions"] == 1
    journal.close()


@pytest.mark.parametrize("complete", [True, False])
def test_task_checks_completion_after_low_confidence_without_more_input(tmp_path, complete):
    from jev_desktop.contracts import Pause, Reason

    runtime, driver, _app, _clock, journal, ownership, session, created = build(
        tmp_path,
        elements=SAVE_ELEMENTS,
        steps=[],
        purpose="task",
        script=[
            ScriptedDecision(Operation.CLICK, "Save"),
            ScriptedDecision(Operation.DONE if complete else Operation.ESCALATE),
        ],
    )
    decide = runtime.policy.decide
    calls = 0

    def uncertain_transition(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise Pause(Reason.LOW_CONFIDENCE, {"selected": "WAIT"})
        if calls == 3:
            assert kwargs["contexts"] == []
        return decide(**kwargs)

    runtime.policy.decide = uncertain_transition
    result = slice_once(runtime, ownership, session, created)
    assert len(driver.executed) == 1
    assert calls == 3
    assert result.execution is (Execution.COMPLETED if complete else Execution.PAUSED)
    if not complete:
        assert result.reason == "needs_visual_assistance"
    assert ownership.active_lease() is None
    journal.close()
