"""Bounded runtime behaviour: checkpoints, budgets, resume rules, and no unattended replay."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jev_desktop.contracts import (
    ActionRequest,
    ContractError,
    Coverage,
    Execution,
    InputMode,
    Limits,
    Operation,
    Pause,
    Reason,
    RunResult,
    RunSpec,
    Verdict,
    new_id,
)
from jev_desktop.evidence import EvidenceStore
from jev_desktop.journal import DispatchJournal, DispatchState
from jev_desktop.ownership import Ownership
from jev_desktop.policy import JevPolicy
from jev_desktop.runtime import ResumeInputs, Runtime, RuntimeConfig

from .fakes import (
    Clock,
    FakeApp,
    FakeDriver,
    FakeElement,
    ScriptedDecision,
    ScriptedPolicy,
    choice_answer,
    fake_response,
)

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
    presence=None,
):
    app = FakeApp(app_ref=APP_REF, window_ref="win:" + "b" * 24, elements=elements)
    driver = FakeDriver(app, evidence_dir=tmp_path / "evidence")
    if presence is not None:
        driver.presence = presence
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


@pytest.mark.parametrize("focusable", [True, False], ids=["focuses-then-clicks", "focus-refused-once"])
def test_task_focuses_the_one_approved_window_before_asking_the_model(tmp_path, focusable):
    runtime, driver, app, _clock, journal, ownership, session, created = build(
        tmp_path,
        elements=SAVE_ELEMENTS,
        steps=[],
        purpose="task",
        script=[ScriptedDecision(Operation.CLICK, "Save"), ScriptedDecision(Operation.DONE)]
        if focusable
        else [ScriptedDecision(Operation.ESCALATE)],
    )
    app.focused, app.focusable = False, focusable
    result = slice_once(runtime, ownership, session, created)
    operations = [request.operation for request in driver.executed]
    if focusable:
        assert result.execution is Execution.COMPLETED
        assert operations == [Operation.FOCUS_WINDOW, Operation.CLICK]
        assert any(context.operation is Operation.CLICK for context in runtime.policy.calls[0]["contexts"])
    else:
        assert result.reason == "needs_visual_assistance"
        assert operations == [Operation.FOCUS_WINDOW], "a refused focus is not retried in a loop"
    journal.close()


def test_task_done_pauses_when_goal_is_not_visible_in_final_observation(tmp_path):
    runtime, driver, _app, _clock, journal, ownership, session, created = build(
        tmp_path,
        elements=[FakeElement("button", "Waiting for you", operations=("CLICK",))],
        steps=[],
        purpose="task",
        script=[ScriptedDecision(Operation.CLICK, "Waiting for you"), ScriptedDecision(Operation.DONE)],
    )
    runtime.policy.done_visible = False
    result = slice_once(runtime, ownership, session, created)
    assert result.execution is Execution.PAUSED
    assert result.reason == "needs_visual_assistance"
    assert len(driver.executed) == 1
    assert len(runtime.policy.done_checks) == 1
    journal.close()


def test_task_reobserves_after_stale_target_refusal(tmp_path):
    runtime, driver, app, _clock, journal, ownership, session, created = build(
        tmp_path,
        elements=[
            FakeElement("button", "Save", operations=("CLICK",)),
            FakeElement("text", "Saved", value="no", text="no", operations=()),
        ],
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
    assert len(calls) == 3
    assert calls[0]["goal"] == calls[1]["goal"]
    assert driver.executed[0].snapshot_id != driver.executed[1].snapshot_id
    records = journal.actions_for_run(created["run_id"])
    assert [record.state for record in records] == [DispatchState.NOT_DISPATCHED, DispatchState.DISPATCHED]
    assert records[0].receipt is None
    assert app.find("Saved").value == "yes"
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
            raise Pause(Reason.LOW_CONFIDENCE, {"selected": "WAIT", "operation": "WAIT"})
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


class TurnTransport:
    """Fake TypeSafe endpoint: each request takes the next turn, {question: (option, confidence)}.

    An option that is not a literal choice names the control whose description carries it.
    """

    def __init__(self, *turns):
        self.turns = list(turns)
        self.sent = []

    def post_json(self, url, *, headers, payload, timeout_s):
        self.sent.append(payload)
        answers = {}
        for key, (option, confidence) in self.turns.pop(0).items():
            criteria = payload["questions"][key]["criteria"]
            if option not in criteria:
                option = next(alias for alias, text in criteria.items() if f'name="{option}"' in text)
            answers[key] = choice_answer(option, list(criteria), confidence=confidence)
        return 200, fake_response("jev-1.13.0", answers)


SEARCH_ELEMENTS = [
    FakeElement("button", "Save", operations=("CLICK",)),
    FakeElement("edit", "Search", editable=True, operations=("TYPE_TEXT", "CLICK")),
    FakeElement("text", "Saved", value="no", text="no", operations=()),
]


def task_with_turns(tmp_path, *turns, elements=SEARCH_ELEMENTS, fixtures=None):
    runtime, driver, app, _clock, journal, ownership, session, created = build(
        tmp_path,
        elements=elements,
        steps=[],
        purpose="task",
        fixtures={"text:query": "UI Navigator"} if fixtures is None else fixtures,
    )
    transport = TurnTransport(*turns)
    runtime.policy = JevPolicy(transport=transport, api_key="test-key")
    return runtime, driver, app, journal, transport, slice_once(runtime, ownership, session, created)


@pytest.mark.parametrize("settles", [True, False], ids=["recheck-acts", "doubt-persists"])
def test_task_next_step_doubt_rechecks_with_actions_still_offered(tmp_path, settles):
    recheck = (
        [
            {"operation": ("CLICK", 0.9), "CLICK_target": ("Search", 0.9)},
            {"operation": ("DONE", 0.9)},
            {"done": ("YES", 0.9)},
        ]
        if settles
        else [{"operation": ("TYPE_TEXT", 0.25)}]
    )
    _runtime, driver, _app, journal, transport, result = task_with_turns(
        tmp_path,
        {"operation": ("CLICK", 0.9), "CLICK_target": ("Save", 0.9)},
        {"operation": ("TYPE_TEXT", 0.3)},
        *recheck,
    )
    rechecked = transport.sent[2]["questions"]
    assert "TYPE_TEXT_target" in rechecked and "CLICK_target" in rechecked
    if settles:
        assert result.execution is Execution.COMPLETED
        assert [request.operation for request in driver.executed] == [Operation.CLICK, Operation.CLICK]
    else:
        assert result.execution is Execution.PAUSED
        assert result.reason == "low_confidence"
        assert dict(result.detail) == {
            "selected": "TYPE_TEXT",
            "confidence": 0.3,
            "margin": 0.125,
            "floor": 0.35,
            "operation": "TYPE_TEXT",
        }
        assert len(transport.sent) == 3
        assert len(driver.executed) == 1
    journal.close()


def test_task_completion_doubt_probe_keeps_the_low_confidence_detail(tmp_path):
    _runtime, driver, _app, journal, transport, result = task_with_turns(
        tmp_path,
        {"operation": ("CLICK", 0.9), "CLICK_target": ("Save", 0.9)},
        {"operation": ("DONE", 0.3)},
        {"operation": ("ESCALATE", 0.9)},
    )
    probe = transport.sent[2]["questions"]
    assert list(probe) == ["operation"]
    assert set(probe["operation"]["criteria"]) == {"WAIT", "DONE", "ESCALATE"}
    assert result.reason == "needs_visual_assistance"
    assert dict(result.detail) == {
        "detail": "completion could not be established; inspect the final window",
        "low_confidence": {"selected": "DONE", "confidence": 0.3, "margin": 0.125, "floor": 0.35, "operation": "DONE"},
    }
    assert len(driver.executed) == 1
    journal.close()


SIDEBAR_ELEMENTS = [
    FakeElement("button", "PSTACK Development Team 7", operations=("CLICK",), state={"expanded": "expanded"}),
    FakeElement("button", "Dispatch", operations=("CLICK", "TOGGLE"), state={"checked": "off"}),
    FakeElement("listitem", "Plugins", operations=("CLICK", "SELECT")),
    FakeElement("button", "Archived", enabled=False, operations=("CLICK",)),
    FakeElement("document", "Unbound", operations=("SCROLL",)),
    FakeElement("text", "Agents", operations=()),
]


def test_task_operation_choice_sees_which_offered_operations_each_control_supports(tmp_path):
    _runtime, _driver, _app, journal, transport, result = task_with_turns(
        tmp_path,
        {"operation": ("DONE", 0.9)},
        {"done": ("YES", 0.9)},
        elements=SIDEBAR_ELEMENTS,
        fixtures={},
    )
    assert result.execution is Execution.COMPLETED

    names = [item.name for item in SIDEBAR_ELEMENTS]

    def operations(request):
        return {
            name: element.get("operations")
            for element in request["state"]["elements"]
            for name in names
            if f'name="{name}"' in element["description"]
        }

    assert operations(transport.sent[0]) == {
        "PSTACK Development Team 7": ["CLICK"],
        "Dispatch": ["CLICK", "TOGGLE"],
        "Plugins": ["CLICK"],
        "Archived": None,
        "Unbound": ["SCROLL"],
        "Agents": None,
    }
    assert set(operations(transport.sent[1]).values()) == {None}, "the completion question offers no operations"
    journal.close()


def test_task_selects_field_then_value_without_cartesian_candidates(tmp_path):
    runtime, driver, app, _clock, journal, ownership, session, created = build(
        tmp_path,
        elements=[FakeElement("edit", f"Field {i}", editable=True, operations=("TYPE_TEXT",)) for i in range(16)],
        steps=[],
        purpose="task",
        fixtures={f"text:value{i}": f"Value {i}" for i in range(16)},
        script=[
            ScriptedDecision(Operation.TYPE_TEXT, "Field 15"),
            ScriptedDecision(Operation.TYPE_TEXT),
            ScriptedDecision(Operation.DONE),
        ],
    )
    from dataclasses import replace

    decide = runtime.policy.decide

    def select_value(**kwargs):
        decision = decide(**kwargs)
        if decision.operation is Operation.TYPE_TEXT and decision.target is None:
            target = next(
                c for group in kwargs["contexts"] for c in group.candidates if c.fixture_ref == "text:value15"
            )
            return replace(decision, target=target)
        return decision

    runtime.policy.decide = select_value
    result = slice_once(runtime, ownership, session, created)
    assert result.execution is Execution.COMPLETED
    assert app.find("Field 15").value == "Value 15"
    assert len(driver.executed) == 1
    assert all(len(context.candidates) <= 16 for call in runtime.policy.calls for context in call["contexts"])
    journal.close()


def test_task_secret_text_is_typed_but_never_sent_to_the_model(tmp_path):
    secret = "correct-horse-battery"
    runtime, _driver, app, _clock, journal, ownership, session, created = build(
        tmp_path,
        elements=[
            FakeElement("edit", "User", editable=True, operations=("TYPE_TEXT",)),
            FakeElement("edit", "Password", editable=True, operations=("TYPE_TEXT",)),
        ],
        steps=[],
        purpose="task",
        fixtures={"text:user": "ada", "secret:password": secret},
        script=[
            ScriptedDecision(Operation.TYPE_TEXT, "Password"),
            ScriptedDecision(Operation.TYPE_TEXT),
            ScriptedDecision(Operation.DONE),
        ],
    )
    from dataclasses import replace

    from jev_desktop.contracts import canonical_json

    decide = runtime.policy.decide

    def select_secret(**kwargs):
        decision = decide(**kwargs)
        if decision.operation is Operation.TYPE_TEXT and decision.target is None:
            target = next(
                c for group in kwargs["contexts"] for c in group.candidates if c.fixture_ref == "secret:password"
            )
            return replace(decision, target=target)
        return decision

    runtime.policy.decide = select_secret
    result = slice_once(runtime, ownership, session, created)
    assert result.execution is Execution.COMPLETED
    assert app.find("Password").value == secret
    calls = runtime.policy.calls
    assert calls[0]["state"]["supplied_text"] == {"user": "ada"}
    assert calls[0]["state"]["secret_text"] == {"password": f"withheld, {len(secret)} characters"}
    for call in calls:
        sent = canonical_json({"state": call["state"], "contexts": [c.to_json() for c in call["contexts"]]})
        assert secret not in sent
    journal.close()


def fresh_save_elements() -> list[FakeElement]:
    # The fake driver mutates elements it acts on, so each test needs its own copies.
    return [
        FakeElement("button", "Save", operations=("CLICK",)),
        FakeElement("text", "Saved", value="no", text="no", operations=()),
    ]


def _saved_check(checkpoint: str = "run_start") -> dict:
    return {
        "assertion_id": "saved-flag",
        "evaluator": "uia_property",
        "target": {"role": "text", "name": "Saved"},
        "property": "value",
        "expected": {"equals": "yes"},
        "checkpoint": checkpoint,
    }


def _click_save(runtime, driver, app, created) -> ActionRequest:
    snapshot = driver.observe(runtime._scope(runtime._state[created["run_id"]]))
    element = next(item for item in snapshot.elements if item.name == "Save")
    return ActionRequest(
        action_id=new_id("act"),
        run_id=created["run_id"],
        operation=Operation.CLICK,
        mode=InputMode.USER_PATH,
        element_id=element.element_id,
        snapshot_id=snapshot.snapshot_id,
        window_ref=app.window_ref,
        lease_generation=0,
        step_id="save",
    )


SAVE_STEP = [{"step_id": "save", "operation": "CLICK", "target_description": "Save"}]


def test_restart_pauses_instead_of_widening_explicit_window_scope(tmp_path):
    runtime, driver, _app, _clock, journal, ownership, session, created = build(
        tmp_path,
        elements=fresh_save_elements(),
        steps=SAVE_STEP,
        limits={**Limits.defaults().to_json(), "slice_seconds": 0.5},
        script=[ScriptedDecision(Operation.WAIT), ScriptedDecision(Operation.CLICK, "Save")],
    )
    first = slice_once(runtime, ownership, session, created, seconds=None)
    assert first.reason == Reason.BUDGET_EXHAUSTED.value
    driver.bind_process = lambda pid, creation_time: APP_REF  # a driver that can rebind after restart
    runtime._state.clear()  # simulate a broker restart: state reloads from the journal
    second = slice_once(runtime, ownership, session, created, resume_token=first.resume_token)
    assert second.execution is Execution.PAUSED and second.reason == Reason.STALE_OBSERVATION.value
    assert "bound_app_ref" not in runtime._state[created["run_id"]].summary
    assert not driver.executed
    journal.close()


@pytest.mark.parametrize("boundary", ["unsupported", "visual"])
def test_visual_and_unsupported_pauses_keep_a_proven_failure(tmp_path, monkeypatch, boundary):
    from dataclasses import replace

    runtime, driver, _app, _clock, journal, ownership, session, created = build(
        tmp_path,
        elements=fresh_save_elements(),
        steps=[{**SAVE_STEP[0], "operation": "TOGGLE" if boundary == "unsupported" else "CLICK"}],
        assertions=[{**_saved_check(), "deadline_s": 0}],
        script=[ScriptedDecision(Operation.CLICK, "Save")],
    )
    if boundary == "unsupported":
        observe = driver.observe
        monkeypatch.setattr(driver, "observe", lambda scope: replace(observe(scope), coverage=Coverage.PARTIAL))
    else:
        driver.fail_next = "pause:needs_visual_assistance"
    result = slice_once(runtime, ownership, session, created)
    assert result.execution is Execution.PAUSED and result.reason == Reason.NEEDS_VISUAL_ASSISTANCE.value
    assert result.verdict is Verdict.FAILED
    assert runtime._state[created["run_id"]].status == "paused"
    journal.close()


def test_visual_boundary_honours_cancellation(tmp_path):
    runtime, driver, _app, _clock, journal, ownership, _session, created = build(
        tmp_path, elements=fresh_save_elements(), steps=SAVE_STEP
    )
    state = runtime._state[created["run_id"]]
    ownership.request_cancel(created["run_id"], "user pressed stop")
    result = runtime._visual_boundary(state, driver.observe(state.spec.scope), None)
    assert result.execution is Execution.CANCELLED
    journal.close()


def test_every_action_assertion_keeps_its_worst_result(tmp_path):
    from jev_desktop.contracts import AssertionResult, AssertionStatus, Evaluator

    runtime, _driver, _app, _clock, journal, _ownership, _session, created = build(
        tmp_path,
        elements=fresh_save_elements(),
        steps=SAVE_STEP,
        assertions=[_saved_check("any"), {**_saved_check("save"), "assertion_id": "at-save"}],
    )
    state = runtime._state[created["run_id"]]

    def merge(assertion_id: str, status: AssertionStatus) -> AssertionStatus:
        result = AssertionResult(assertion_id, status, Evaluator.UIA_PROPERTY, "application", {}, {}, "save", 0.0)
        runtime._merge_assertion(state, result)
        return next(item.status for item in state.assertions if item.assertion_id == assertion_id)

    assert merge("saved-flag", AssertionStatus.PASSED) is AssertionStatus.PASSED
    assert merge("saved-flag", AssertionStatus.INCONCLUSIVE) is AssertionStatus.INCONCLUSIVE
    assert merge("saved-flag", AssertionStatus.PASSED) is AssertionStatus.INCONCLUSIVE
    assert merge("saved-flag", AssertionStatus.FAILED) is AssertionStatus.FAILED
    assert merge("at-save", AssertionStatus.PASSED) is AssertionStatus.PASSED
    assert merge("at-save", AssertionStatus.INCONCLUSIVE) is AssertionStatus.PASSED
    journal.close()


def test_resume_values_are_validated_at_the_edge():
    with pytest.raises(ContractError):
        ResumeInputs(fixtures={"name_value": None})
    with pytest.raises(ContractError):
        ResumeInputs(verifier_results={"check": "passed"})


def test_rejected_resume_records_nothing(tmp_path):
    runtime, driver, _app, _clock, journal, ownership, session, created = build(
        tmp_path, elements=fresh_save_elements(), steps=SAVE_STEP, fixtures={"name_value": None}
    )
    inputs = ResumeInputs(fixtures={"name_value": "Ada"}, verifier_results={"unknown": {}})
    with pytest.raises(ContractError):
        slice_once(runtime, ownership, session, created, inputs=inputs)
    state = runtime._state[created["run_id"]]
    assert "fixture_fingerprints" not in state.summary and not state.supplied_fixtures
    assert not driver.executed
    journal.close()


@pytest.mark.parametrize(("deadline", "budget"), [(0.5, "run_deadline"), (600.0, "slice_deadline")])
def test_deadline_pause_names_the_expired_deadline(tmp_path, deadline, budget):
    runtime, _driver, _app, _clock, journal, ownership, session, created = build(
        tmp_path,
        elements=fresh_save_elements(),
        steps=SAVE_STEP,
        limits={**Limits.defaults().to_json(), "deadline_seconds": deadline, "slice_seconds": 0.5},
        script=[ScriptedDecision(Operation.WAIT)],
    )
    result = slice_once(runtime, ownership, session, created, seconds=None)
    assert result.reason == Reason.BUDGET_EXHAUSTED.value and result.detail["budget"] == budget
    journal.close()


def test_act_rotates_the_token_when_it_pauses_after_input(tmp_path):
    runtime, driver, app, _clock, journal, _ownership, session, created = build(
        tmp_path,
        elements=fresh_save_elements(),
        steps=SAVE_STEP,
        assertions=[
            {"assertion_id": "looks-right", "evaluator": "model_visual", "oracle": "caller", "checkpoint": "save"}
        ],
    )
    request = _click_save(runtime, driver, app, created)
    act = {"run_id": created["run_id"], "session_id": session.session_id, "request": request}
    with pytest.raises(Pause) as paused:
        runtime.act(resume_token=created["resume_token"], **act)
    assert paused.value.reason is Reason.NEEDS_VISUAL_ASSISTANCE and len(driver.executed) == 1
    token = paused.value.detail["resume_token"]
    assert token != created["resume_token"] and token == runtime._state[created["run_id"]].resume_token
    with pytest.raises(ContractError, match="resume token"):
        runtime.act(resume_token=created["resume_token"], **act)
    journal.close()


def test_act_evaluates_run_start_assertions_before_input(tmp_path):
    runtime, driver, app, _clock, journal, _ownership, session, created = build(
        tmp_path,
        elements=fresh_save_elements(),
        steps=SAVE_STEP,
        assertions=[{**_saved_check(), "expected": {"equals": "no"}}],
    )
    runtime.act(
        run_id=created["run_id"],
        session_id=session.session_id,
        resume_token=created["resume_token"],
        request=_click_save(runtime, driver, app, created),
    )
    results = runtime._state[created["run_id"]].assertions
    assert [(item.checkpoint, item.status.value) for item in results] == [("run_start", "passed")]
    assert len(driver.executed) == 1
    journal.close()


def test_stale_retries_reset_after_progress(tmp_path):
    elements = [FakeElement("edit", "Name", value="", editable=True, operations=("TYPE_TEXT",)), *fresh_save_elements()]
    runtime, driver, app, _clock, journal, ownership, session, created = build(
        tmp_path,
        elements=elements,
        steps=[
            *SAVE_STEP,
            {"step_id": "name", "operation": "TYPE_TEXT", "target_description": "Name", "fixture_reference": "name"},
        ],
        fixtures={"name": "Ada"},
        limits={**Limits.defaults().to_json(), "stale_retries": 1},
        script=[
            ScriptedDecision(Operation.CLICK, "Save"),
            ScriptedDecision(Operation.CLICK, "Save"),
            ScriptedDecision(Operation.TYPE_TEXT, "Name"),
            ScriptedDecision(Operation.TYPE_TEXT, "Name"),
        ],
    )
    execute = driver.execute
    attempts = []

    def stale_before_each_step(request, guard, snapshot):
        attempts.append(request)
        if len(attempts) in {1, 3}:
            raise Pause(Reason.STALE_OBSERVATION, {"injected": True})
        return execute(request, guard, snapshot)

    driver.execute = stale_before_each_step
    result = slice_once(runtime, ownership, session, created)
    assert result.execution is Execution.COMPLETED
    assert app.find("Name").value == "Ada"
    journal.close()


ARTIFACT = {"evaluator": "artifact", "target": {"path": "state.json"}}


@pytest.mark.parametrize(
    ("steps", "assertion"),
    [
        (SAVE_STEP, {"expected": {"gte": "3"}}),
        (SAVE_STEP, {"expected": {"in": 5}}),
        (SAVE_STEP, {"target": {"name_regex": "("}}),
        (SAVE_STEP, {**ARTIFACT, "property": "size_at_least", "expected": {"bytes": "x"}}),
        (SAVE_STEP, {**ARTIFACT, "property": "mtime_after", "expected": {"after": "x"}}),
        ([{"step_id": "type", "operation": "TYPE_TEXT", "target_description": "Name"}], None),
        ([{**SAVE_STEP[0], "depends_on": ["later"]}, {**SAVE_STEP[0], "step_id": "later"}], None),
    ],
)
def test_malformed_specs_are_rejected_before_any_input(tmp_path, steps, assertion):
    with pytest.raises(ContractError):
        build(
            tmp_path,
            elements=fresh_save_elements(),
            steps=steps,
            assertions=[{**_saved_check("save"), **assertion}] if assertion else None,
        )


class FakePresence:
    """Physical input, simulated: `touch` is a human click or key, `busy` a user who keeps going."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.human_epoch = 0
        self.last_input: float | None = None
        self.busy = False
        self.active: list[bool] = []
        self.on_escape = None

    def touch(self) -> None:
        self.human_epoch += 1
        self.last_input = self.clock()

    def idle_seconds(self) -> float:
        if self.busy:
            return 0.0
        return float("inf") if self.last_input is None else self.clock() - self.last_input

    def set_active(self, active: bool) -> None:
        self.active.append(active)


def _before_first_decision(runtime, action) -> None:
    decide = runtime.policy.decide
    calls = 0

    def wrapped(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            action()
        return decide(**kwargs)

    runtime.policy.decide = wrapped


@pytest.mark.parametrize("purpose", ["task", "regression"])
def test_user_input_before_dispatch_waits_then_reobserves_and_continues(tmp_path, purpose):
    presence = FakePresence(Clock())
    runtime, driver, app, clock, journal, ownership, session, created = build(
        tmp_path,
        elements=fresh_save_elements(),
        steps=[] if purpose == "task" else SAVE_STEP,
        purpose=purpose,
        script=[ScriptedDecision(Operation.CLICK, "Save"), ScriptedDecision(Operation.CLICK, "Save")]
        + ([ScriptedDecision(Operation.DONE)] if purpose == "task" else []),
        presence=presence,
    )
    presence.clock = clock
    _before_first_decision(runtime, presence.touch)  # the user clicks while Jev is deciding
    result = slice_once(runtime, ownership, session, created, seconds=30.0)
    assert result.execution is Execution.COMPLETED
    assert len(driver.executed) == 1, "the stale decision must not be sent"
    assert app.find("Saved").value == "yes"
    records = journal.actions_for_run(created["run_id"])
    assert [record.state for record in records] == [DispatchState.NOT_DISPATCHED, DispatchState.DISPATCHED]
    state = runtime._load(created["run_id"])
    held = state.summary["hold_seconds"]
    assert held >= runtime.config.human_grace_seconds
    assert runtime._run_deadline(state) == state.started_at + state.spec.limits.deadline_seconds + held
    assert presence.active == [True, False]
    journal.close()


def test_user_who_keeps_working_gets_the_desktop_back_as_a_resumable_takeover(tmp_path):
    presence = FakePresence(Clock())
    runtime, driver, _app, clock, journal, ownership, session, created = build(
        tmp_path,
        elements=fresh_save_elements(),
        steps=[],
        purpose="task",
        script=[ScriptedDecision(Operation.CLICK, "Save")],
        presence=presence,
    )
    presence.clock = clock
    presence.busy = True
    result = slice_once(runtime, ownership, session, created, seconds=5.0)
    assert result.execution is Execution.PAUSED
    assert result.reason == Reason.USER_TAKEOVER.value
    assert result.detail["resumable"] is True
    assert driver.executed == []
    assert runtime._load(created["run_id"]).summary["hold_seconds"] == pytest.approx(5.0, abs=0.3)
    journal.close()


def test_hold_credit_is_capped(tmp_path):
    presence = FakePresence(Clock())
    runtime, _driver, _app, clock, journal, ownership, session, created = build(
        tmp_path, elements=fresh_save_elements(), steps=[], purpose="task", presence=presence
    )
    presence.clock = clock
    presence.busy = True
    runtime.config.max_hold_seconds = 2.0
    result = slice_once(runtime, ownership, session, created, seconds=30.0)
    assert result.reason == Reason.USER_TAKEOVER.value
    assert runtime._load(created["run_id"]).summary["hold_seconds"] == 2.0
    journal.close()


def test_escape_stops_the_run_before_any_input(tmp_path):
    presence = FakePresence(Clock())
    runtime, driver, _app, _clock, journal, ownership, session, created = build(
        tmp_path,
        elements=fresh_save_elements(),
        steps=[],
        purpose="task",
        script=[ScriptedDecision(Operation.CLICK, "Save")],
        presence=presence,
    )
    assert presence.on_escape == runtime._on_escape
    _before_first_decision(runtime, presence.on_escape)
    result = slice_once(runtime, ownership, session, created)
    assert result.execution is Execution.CANCELLED
    assert result.detail["message"] == "Esc pressed"
    assert driver.executed == []
    assert ownership.active_lease() is None
    journal.close()


def test_caller_view_drops_structural_rows_but_the_model_still_sees_them(tmp_path):
    runtime, driver, _app, _clock, journal, _ownership, _session, created = build(
        tmp_path, elements=[FakeElement("pane", "", operations=()), *SAVE_ELEMENTS], steps=[], purpose="task"
    )
    state = runtime._load(created["run_id"])
    snapshot = driver.observe(runtime._scope(state))
    model = runtime._observation_for_policy(state, snapshot)
    caller = runtime._observation_summary(snapshot)
    assert [element["role"] for element in model["elements"]] == ["pane", "button", "text"]
    assert [element["role"] for element in caller["elements"]] == ["button", "text"]
    assert model["context"]["texts"] and caller["context"]["texts"] == []
    journal.close()
