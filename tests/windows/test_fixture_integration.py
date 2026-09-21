"""Live Windows integration: the plugin against the controlled fixture application.

Marked `windows` and `live`: these tests move the real mouse and keyboard, but only inside
windows the fixture itself created. The driver refuses to act when the target window is
not the foreground window, and every action re-validates the hit target under the click
point. Run with `python -m pytest tests/windows -m live -q`.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

from launcher import start_fixture

from jev_desktop.contracts import (
    ActionRequest,
    AssertionSpec,
    AssertionStatus,
    Evaluator,
    Execution,
    ExpectedIdentity,
    IdentityStatus,
    InputMode,
    Limits,
    Operation,
    Pause,
    Reason,
    RunSpec,
    ScopeSpec,
    Verdict,
    new_id,
)
from jev_desktop.drivers.windows import WindowsDriver, win32
from jev_desktop.evidence import EvidenceStore
from jev_desktop.journal import DispatchJournal
from jev_desktop.ownership import Ownership
from jev_desktop.runtime import Runtime, RuntimeConfig
from jev_desktop.verification import EvalContext, evaluate
from tests.unit.fakes import Clock, ScriptedDecision, ScriptedPolicy

pytestmark = [pytest.mark.windows, pytest.mark.live]

RUN_ID = "run:" + "1" * 24


@pytest.fixture()
def driver(tmp_path):
    instance = WindowsDriver(evidence_dir=tmp_path / "evidence")
    instance.start()
    try:
        yield instance
    finally:
        instance.close()


class Session:
    """Driver convenience wrapper for mechanism-level assertions."""

    def __init__(self, driver: WindowsDriver, fixture) -> None:
        self.driver = driver
        self.fixture = fixture
        self.app = next(app for app in driver.list_apps() if app.process_id == fixture.pid)
        self.scope = ScopeSpec(app_ref=self.app.app_ref, max_elements=200)
        self.snapshot = driver.observe(self.scope)

    def refresh(self):
        self.snapshot = self.driver.observe(self.scope)
        return self.snapshot

    def settle(self, *, timeout: float = 2.0, fingerprint: str | None = None):
        """Wait for the application to react to the last input, then re-observe."""
        deadline = time.monotonic() + timeout
        baseline = fingerprint or self.snapshot.fingerprint
        current = self.refresh()
        while current.fingerprint == baseline and time.monotonic() < deadline:
            time.sleep(0.08)
            current = self.refresh()
        return current

    def element(self, name: str, role: str | None = None):
        for element in self.snapshot.elements:
            if element.name == name and (role is None or element.role == role):
                return element
        names = sorted({element.name for element in self.snapshot.elements if element.name})
        raise AssertionError(f"element {name!r} was not observed; saw {names}")

    def status_text(self) -> str:
        matches = [
            element.text or "" for element in self.snapshot.elements if (element.text or "").startswith("Status:")
        ]
        return matches[0] if matches else ""

    def is_foreground(self) -> bool:
        return win32.root_window(win32.foreground_window()) == win32.root_window(self.fixture.hwnd)

    def ensure_foreground(self, *, timeout: float = 3.0) -> str:
        """Bring the fixture forward the way a user would, and report which route worked.

        Windows refuses cross-process focus stealing, so `FOCUS_WINDOW` is allowed to fail
        honestly; a real mouse click on the window is what actually activates it.
        """
        if self.is_foreground():
            return "already_foreground"
        try:
            self.focus_window()
            if self.is_foreground():
                return "focus_window"
        except Pause as failure:
            if failure.reason_value != Reason.PERMISSION_BOUNDARY.value:
                raise
        # A benign clickable control: selection of the first list row has no side effect here.
        self.act(Operation.CLICK, name="Item 01")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.is_foreground():
                return "activation_click"
            time.sleep(0.05)
        raise AssertionError("the fixture window never became foreground")

    def focus_window(self):
        request = ActionRequest(
            action_id=new_id("act"),
            run_id=RUN_ID,
            operation=Operation.FOCUS_WINDOW,
            mode=InputMode.USER_PATH,
            element_id=None,
            snapshot_id=self.snapshot.snapshot_id,
            window_ref=self.snapshot.windows[0].window_ref,
            lease_generation=1,
        )
        return self.driver.execute(request, lambda: None, self.snapshot)

    def act(
        self,
        operation: Operation,
        *,
        name: str | None = None,
        role: str | None = None,
        mode=InputMode.USER_PATH,
        settle: bool = True,
        **kwargs,
    ):
        snapshot = self.refresh()
        element = self.element(name, role) if name else None
        request = ActionRequest(
            action_id=new_id("act"),
            run_id=RUN_ID,
            operation=operation,
            mode=mode,
            element_id=element.element_id if element else None,
            snapshot_id=snapshot.snapshot_id,
            window_ref=(element.window_ref if element else snapshot.windows[0].window_ref),
            lease_generation=1,
            **kwargs,
        )
        receipt = self.driver.execute(request, lambda: None, snapshot)
        if settle and operation in {
            Operation.CLICK,
            Operation.TOGGLE,
            Operation.TYPE_TEXT,
            Operation.SELECT,
            Operation.SCROLL,
            Operation.HOTKEY,
        }:
            self.settle(fingerprint=snapshot.fingerprint)
        return receipt, snapshot


# --------------------------------------------------------------------------------------
# Mechanism-level behaviour
# --------------------------------------------------------------------------------------


def test_observation_finds_fixture_controls(driver, tmp_path):
    fixture = start_fixture("basic", build_id="obs-1", state_dir=tmp_path / "obs")
    try:
        session = Session(driver, fixture)
        names = {element.name for element in session.snapshot.elements}
        assert {"Name", "Save", "Open dialog", "Export", "Enable feature", "Item 01"} <= names
        # The fixture's 40-item list shows 9 rows: skipped offscreen rows are reported, so the
        # observation is honestly partial rather than silently incomplete.
        assert session.snapshot.coverage.value in {"partial", "complete"}
        if session.snapshot.coverage.value == "partial":
            assert any("offscreen" in note for note in session.snapshot.truncation)
        assert session.snapshot.context["modal_windows"] == []
    finally:
        fixture.stop()


def test_user_path_click_saves_and_assertion_passes(driver, tmp_path):
    fixture = start_fixture("basic", build_id="click-1", run_id=RUN_ID, state_dir=tmp_path / "click")
    try:
        session = Session(driver, fixture)
        session.ensure_foreground()
        receipt, _snapshot = session.act(Operation.CLICK, name="Save")
        assert receipt.dispatch_state.value == "dispatched"
        assert receipt.mechanism.value == "send_input_mouse"
        session.refresh()
        assert session.status_text() == "Status: saved"
        state = fixture.read_state()
        assert state and state["run_id"] == RUN_ID

        spec = AssertionSpec(
            assertion_id="saved-status",
            evaluator=Evaluator.UIA_PROPERTY,
            target={"role": "text", "name_regex": "^Status:"},
            expected={"contains": "saved"},
            property="text",
        )
        result = evaluate(
            spec,
            EvalContext(
                observation=session.snapshot,
                run_id=RUN_ID,
                checkpoint="save",
                evidence=EvidenceStore(root=tmp_path / "evidence", approved_roots=[str(tmp_path)]),
                approved_roots=[str(tmp_path)],
            ),
        )
        assert result.status is AssertionStatus.PASSED
    finally:
        fixture.stop()


def test_typed_text_persists_across_a_restart(driver, tmp_path):
    directory = tmp_path / "persist"
    fixture = start_fixture("basic", build_id="persist-1", run_id=RUN_ID, state_dir=directory)
    try:
        session = Session(driver, fixture)
        session.ensure_foreground()
        session.act(Operation.TYPE_TEXT, name="Name", role="edit", text="Ada Lovelace")
        session.act(Operation.HOTKEY, hotkey=("ctrl", "s"))
        state = fixture.read_state()
        assert state and state["name"] == "Ada Lovelace"
    finally:
        fixture.stop()

    restarted = start_fixture("basic", build_id="persist-1", run_id=RUN_ID, state_dir=directory)
    try:
        session = Session(driver, restarted)
        name_field = (
            session.element("Name", role="edit")
            if any(element.role == "edit" for element in session.snapshot.elements)
            else None
        )
        values = [element.value for element in session.snapshot.elements if element.role == "edit"]
        assert "Ada Lovelace" in values, f"restored values were {values}"
        assert name_field is not None
    finally:
        restarted.stop()


def test_dead_save_button_fails_while_the_shortcut_works(driver, tmp_path):
    fixture = start_fixture("dead-save", build_id="dead-1", run_id=RUN_ID, state_dir=tmp_path / "dead")
    try:
        session = Session(driver, fixture)
        session.ensure_foreground()
        session.act(Operation.CLICK, name="Save")
        session.refresh()
        assert session.status_text() == "Status: save-ignored"
        assert fixture.read_state() is None, "the dead button must not persist anything"

        spec = AssertionSpec(
            assertion_id="persisted",
            evaluator=Evaluator.ARTIFACT,
            target={"path": str(fixture.state_dir / "state.json")},
            expected={"is_true": True},
            property="exists",
        )
        result = evaluate(
            spec,
            EvalContext(
                observation=session.snapshot,
                run_id=RUN_ID,
                checkpoint="save",
                approved_roots=[str(tmp_path)],
            ),
        )
        assert result.status is AssertionStatus.FAILED, "a dead button must not pass"

        # The keyboard shortcut works, so the failure is the click path, not the feature.
        session.act(Operation.HOTKEY, hotkey=("ctrl", "s"))
        deadline = time.monotonic() + 2.0
        while fixture.read_state() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert fixture.read_state() is not None
    finally:
        fixture.stop()


def test_semantic_invoke_succeeds_where_the_mouse_path_does_not(driver, tmp_path):
    fixture = start_fixture("semantic-only", build_id="sem-1", run_id=RUN_ID, state_dir=tmp_path / "sem")
    try:
        session = Session(driver, fixture)
        session.ensure_foreground()
        receipt, _ = session.act(Operation.CLICK, name="Save", mode=InputMode.USER_PATH)
        assert receipt.mechanism.value == "send_input_mouse"
        session.refresh()
        assert fixture.read_state() is None, "the mouse path is deliberately inert in this scenario"

        semantic, _ = session.act(Operation.CLICK, name="Save", mode=InputMode.SEMANTIC)
        assert semantic.mechanism.value == "uia_pattern"
        assert fixture.read_state() is not None, "the semantic path does save"
    finally:
        fixture.stop()


def test_modal_dialog_blocks_further_input(driver, tmp_path):
    fixture = start_fixture("modal-block", build_id="modal-1", run_id=RUN_ID, state_dir=tmp_path / "modal")
    try:
        session = Session(driver, fixture)
        session.ensure_foreground()
        session.act(Operation.CLICK, name="Open dialog")
        session.settle()
        assert session.snapshot.windows, "the dialog should be observable"
        assert any(window.modal for window in session.snapshot.windows) or not session.snapshot.windows[0].enabled

        with pytest.raises(Pause) as failure:
            session.act(Operation.CLICK, name="Save")
        assert failure.value.reason_value in {Reason.PERMISSION_BOUNDARY.value, Reason.USER_TAKEOVER.value}
        assert fixture.read_state() is None, "no input may be dispatched to a disabled window"
    finally:
        fixture.stop()


def test_stale_export_fails_a_run_scoped_assertion(driver, tmp_path):
    fixture = start_fixture("stale-artifact", build_id="stale-1", run_id=RUN_ID, state_dir=tmp_path / "stale")
    try:
        session = Session(driver, fixture)
        session.ensure_foreground()
        session.act(Operation.CLICK, name="Export")
        session.refresh()
        exported = fixture.read_export()
        assert exported is not None and exported["run_id"] != RUN_ID

        spec = AssertionSpec(
            assertion_id="export-fresh",
            evaluator=Evaluator.ARTIFACT,
            target={"path": str(fixture.state_dir / "export.json"), "run_id_field": "run_id"},
            expected={},
            property="run_scoped",
        )
        result = evaluate(
            spec,
            EvalContext(
                observation=session.snapshot,
                run_id=RUN_ID,
                checkpoint="export",
                approved_roots=[str(tmp_path)],
            ),
        )
        assert result.status is AssertionStatus.FAILED
    finally:
        fixture.stop()


def test_green_status_without_persistence_is_not_a_pass(driver, tmp_path):
    fixture = start_fixture("false-done", build_id="false-1", run_id=RUN_ID, state_dir=tmp_path / "false")
    try:
        session = Session(driver, fixture)
        session.ensure_foreground()
        session.act(Operation.CLICK, name="Save")
        session.refresh()
        assert session.status_text() == "Status: saved", "the UI claims success"
        assert fixture.read_state() is None, "nothing was persisted"

        spec = AssertionSpec(
            assertion_id="state-written",
            evaluator=Evaluator.ARTIFACT,
            target={"path": str(fixture.state_dir / "state.json")},
            expected={"is_true": True},
            property="exists",
        )
        result = evaluate(
            spec,
            EvalContext(
                observation=session.snapshot,
                run_id=RUN_ID,
                checkpoint="save",
                approved_roots=[str(tmp_path)],
            ),
        )
        assert result.status is AssertionStatus.FAILED
    finally:
        fixture.stop()


def test_focus_window_reports_honestly_when_activation_is_denied(driver, tmp_path):
    fixture = start_fixture("basic", build_id="focus-1", state_dir=tmp_path / "focus")
    try:
        session = Session(driver, fixture)
        denied = False
        try:
            session.focus_window()
        except Pause as failure:
            denied = True
            assert failure.reason_value == Reason.PERMISSION_BOUNDARY.value
        if not denied:
            assert session.is_foreground(), "a successful FOCUS_WINDOW must really be foreground"
        assert session.ensure_foreground() in {"already_foreground", "focus_window", "activation_click"}
    finally:
        fixture.stop()


def test_wrong_build_is_detected_before_any_action(driver, tmp_path):
    fixture = start_fixture("wrong-build", build_id="build-7", state_dir=tmp_path / "wrong")
    try:
        app = next(app for app in driver.list_apps() if app.process_id == fixture.pid)
        report = driver.identity(
            app.app_ref,
            ExpectedIdentity(
                mode="file_marker",
                marker_path=str(fixture.state_dir / "build_marker.json"),
                expect_marker="build-7",
            ),
        )
        assert report.status is IdentityStatus.MISMATCH
        assert "marker" in " ".join(report.notes).lower()
    finally:
        fixture.stop()


# --------------------------------------------------------------------------------------
# End-to-end runtime on the real fixture
# --------------------------------------------------------------------------------------


def test_runtime_end_to_end_passes_with_evidence(driver, tmp_path):
    fixture = start_fixture("basic", build_id="e2e-1", run_id=None, state_dir=tmp_path / "e2e")
    try:
        app = next(app for app in driver.list_apps() if app.process_id == fixture.pid)
        clock = Clock()
        journal = DispatchJournal(str(tmp_path / "journal.sqlite"))
        ownership = Ownership(clock=clock)
        evidence = EvidenceStore(root=tmp_path / "evidence", approved_roots=[str(tmp_path)])
        runtime = Runtime(
            driver=driver,
            journal=journal,
            ownership=ownership,
            evidence=evidence,
            config=RuntimeConfig(
                evidence_dir=tmp_path / "evidence",
                approved_roots=(str(tmp_path),),
                sleeper=clock.sleep,
                clock=clock,
            ),
            policy=ScriptedPolicy(
                [
                    ScriptedDecision(Operation.CLICK, "Enable feature"),
                    ScriptedDecision(Operation.CLICK, "Save"),
                ]
            ),
        )
        spec = RunSpec.from_json(
            {
                "goal": "regression: the Save button persists the document",
                "purpose": "regression",
                "interaction_mode": "user_path",
                "app_ref": app.app_ref,
                "expected_identity": {
                    "mode": "file_marker",
                    "marker_path": str(fixture.state_dir / "build_marker.json"),
                    "expect_marker": "e2e-1",
                },
                "launch_config_id": None,
                "steps": [
                    {
                        "step_id": "activate",
                        "operation": "CLICK",
                        "target_description": "the Enable feature checkbox (activates the window)",
                    },
                    {
                        "step_id": "save",
                        "operation": "CLICK",
                        "target_description": "the Save button",
                        "checkpoint": True,
                    },
                ],
                "assertions": [
                    {
                        "assertion_id": "saved-status",
                        "evaluator": "uia_property",
                        "target": {"role": "text", "name_regex": "^Status:"},
                        "property": "text",
                        "expected": {"contains": "saved"},
                        "checkpoint": "save",
                    },
                    {
                        "assertion_id": "state-artifact",
                        "evaluator": "artifact",
                        "target": {"path": str(fixture.state_dir / "state.json")},
                        "expected": {"is_true": True},
                        "property": "exists",
                        "checkpoint": "save",
                    },
                ],
                "fixtures": {},
                "secret_refs": {},
                "limits": Limits.defaults().to_json(),
                "scope": {"app_ref": app.app_ref, "max_elements": 120},
                "allow_restart": False,
            }
        )
        session = ownership.create_session("live-e2e")
        created = runtime.create_run(spec, session_id=session.session_id)
        ownership.acquire(session.session_id, created["run_id"])
        result = runtime.slice(
            run_id=created["run_id"],
            session_id=session.session_id,
            resume_token=created["resume_token"],
            slice_seconds=30.0,
        )
        assert result.execution is Execution.COMPLETED, result.detail
        assert result.verdict is Verdict.PASSED, [a.to_json() for a in result.assertions]
        assert [step.step_id for step in result.steps] == ["activate", "save"]
        assert all(step.dispatch_state.value == "dispatched" for step in result.steps)
        assert fixture.read_state() is not None

        screenshots = [reference for reference in result.evidence if reference.kind == "screenshot"]
        assert screenshots, "checkpoints produce evidence"
        for reference in screenshots:
            payload = Path(reference.path).read_bytes()
            assert payload[:8] == b"\x89PNG\r\n\x1a\n"
        assert json.loads(str(journal.get_run(created["run_id"])["state_json"]))["completed_steps"] == [
            "activate",
            "save",
        ]
        journal.close()
    finally:
        fixture.stop()
