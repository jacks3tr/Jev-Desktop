"""Live TypeSafe tests: the policy decision path, exercised against the real API.

These are the tests that prove the seam the offline suite cannot reach. They are skipped
unless both of these hold:

* `JEV_TYPESAFE_LIVE=1` is set, the same explicit opt-in used by the desktop tests.
* a key is available, either in `TYPESAFE_API_KEY` or in the dotenv file named by
  `JEV_TYPESAFE_ENV_FILE`.

Run them with:

    set JEV_TYPESAFE_LIVE=1
    set JEV_TYPESAFE_ENV_FILE=D:\\path\\to\\.env
    python -m pytest tests/live -m live -q

Each decision costs one request, roughly 1.9k input tokens and 100 output tokens.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

from jev_desktop.contracts import (
    Execution,
    InputMode,
    Limits,
    Operation,
    Purpose,
    RunSpec,
    Verdict,
)
from jev_desktop.evidence import EvidenceStore
from jev_desktop.journal import DispatchJournal
from jev_desktop.ownership import Ownership
from jev_desktop.policy import HttpTransport, JevPolicy, PolicyConfig, build_contexts
from jev_desktop.runtime import Runtime, RuntimeConfig

pytestmark = [pytest.mark.live]


def read_dotenv(path: Path) -> str | None:
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("TYPESAFE_API_KEY="):
            value = line.split("=", 1)[1].strip().strip('"').strip("'")
            return value or None
    return None


def api_key() -> str | None:
    if os.environ.get("JEV_TYPESAFE_LIVE") != "1":
        return None
    key = os.environ.get("TYPESAFE_API_KEY")
    if key:
        return key.strip()
    env_file = os.environ.get("JEV_TYPESAFE_ENV_FILE")
    return read_dotenv(Path(env_file)) if env_file else None


@pytest.fixture()
def policy() -> JevPolicy:
    key = api_key()
    if not key:
        pytest.skip("live TypeSafe tests need JEV_TYPESAFE_LIVE=1 and a key, see the module docstring")
    return JevPolicy(transport=HttpTransport(), config=PolicyConfig(), api_key=key)


def fixture_state() -> dict:
    """Element payload shaped exactly like the one the runtime sends."""

    def element(index: int, key: str, role: str, name: str, **extra) -> dict:
        return {
            "index": index,
            "element_id": f"el:{key * 23}{'a' if index < 10 else 'b'}",
            "role": role,
            "name": name,
            "value": extra.get("value"),
            "enabled": extra.get("enabled", True),
            "visible": True,
            "editable": extra.get("editable", False),
            "focused": False,
            "operations": extra.get("operations", ["CLICK"]),
            "path": ["Jev Fixture Window"],
            "state": extra.get("state", {}),
            "truncation": None,
        }

    elements = [
        element(1, "1", "text", "Name", operations=[]),
        element(2, "2", "edit", "Name", editable=True, operations=["TYPE_TEXT", "CLICK"]),
        element(3, "3", "checkbox", "Enable feature", state={"checked": "off"}, operations=["CLICK", "TOGGLE"]),
        element(4, "4", "button", "Save", operations=["CLICK"]),
        element(5, "5", "button", "Open dialog", operations=["CLICK"]),
        element(6, "6", "button", "Export", operations=["CLICK"]),
        element(7, "7", "text", "Status: ready", operations=[]),
    ]
    return {
        "elements": elements,
        "context": {
            "window_titles": ["Jev Fixture - basic"],
            "modal_windows": [],
            "texts": [],
            "coverage": "partial",
            "truncation": ["4 offscreen elements were not observed"],
            "focused_element_id": None,
        },
    }


def test_live_decision_selects_the_required_control(policy: JevPolicy):
    """One real request: does the answer name the control the step asks for, and validate?"""
    state = fixture_state()
    contexts = build_contexts(observation=state, operations=[Operation.CLICK])
    assert contexts, "the fixture state should offer clickable candidates"

    decision = policy.decide(
        goal="regression: the Save button must persist the document",
        state=state,
        contexts=contexts,
        allow_done=False,
        current_step={"step_id": "save", "operation": "CLICK", "target_description": "the Save button"},
    )

    assert decision.model == PolicyConfig().model_id, "the pinned model id is what resolved"
    assert decision.operation is Operation.CLICK
    assert decision.target is not None and 'name="Save"' in decision.target.description, decision.target
    assert decision.operation_confidence >= PolicyConfig().operation_floor
    assert decision.target_confidence is not None and decision.target_confidence >= PolicyConfig().target_floor
    assert decision.latency_ms > 0 and decision.usage.get("input_tokens", 0) > 0


def test_live_run_against_the_fixture_application(policy: JevPolicy, tmp_path):
    """The whole chain with real decisions: observe, decide, dispatch, verify, verdict."""
    if os.environ.get("JEV_DESKTOP_LIVE") != "1":
        pytest.skip("set JEV_DESKTOP_LIVE=1 as well to run the desktop half")

    from launcher import start_fixture

    from jev_desktop.drivers.windows import WindowsDriver

    fixture = start_fixture("basic", build_id="live-policy", hidden=False, allow_desktop=True)
    driver = WindowsDriver(evidence_dir=tmp_path / "evidence")
    driver.start()
    journal = DispatchJournal(str(tmp_path / "journal.sqlite"))
    ownership = Ownership()
    evidence = EvidenceStore(root=tmp_path / "evidence", approved_roots=[str(tmp_path)])
    runtime = Runtime(
        driver=driver,
        journal=journal,
        ownership=ownership,
        evidence=evidence,
        config=RuntimeConfig(evidence_dir=tmp_path / "evidence", approved_roots=(str(tmp_path),)),
        policy=policy,
    )
    try:
        app = next(app for app in driver.list_apps() if app.process_id == fixture.pid)
        spec = RunSpec.from_json(
            {
                "goal": "regression: clicking Save persists the document and reports that it saved",
                "purpose": Purpose.REGRESSION.value,
                "interaction_mode": InputMode.USER_PATH.value,
                "app_ref": app.app_ref,
                "expected_identity": {
                    "mode": "file_marker",
                    "marker_path": str(fixture.state_dir / "build_marker.json"),
                    "expect_marker": "live-policy",
                },
                "launch_config_id": None,
                "steps": [
                    {
                        "step_id": "save",
                        "operation": "CLICK",
                        "target_description": "the Save button in the fixture window",
                        "checkpoint": True,
                    }
                ],
                "assertions": [
                    {
                        "assertion_id": "status-text",
                        "evaluator": "uia_property",
                        "target": {"role": "text", "name_regex": "^Status:"},
                        "property": "text",
                        "expected": {"contains": "saved"},
                        "checkpoint": "save",
                    },
                    {
                        "assertion_id": "state-written",
                        "evaluator": "artifact",
                        "target": {"path": str(fixture.state_dir / "state.json")},
                        "expected": {"is_true": True},
                        "property": "exists",
                        "checkpoint": "save",
                    },
                ],
                "fixtures": {},
                "secret_refs": {},
                "limits": {**Limits.defaults().to_json(), "max_model_decisions": 4},
                "scope": {"app_ref": app.app_ref, "max_elements": 160},
                "allow_restart": False,
            }
        )
        session = ownership.create_session("live-policy-test")
        created = runtime.create_run(spec, session_id=session.session_id)
        ownership.acquire(session.session_id, created["run_id"])
        result = runtime.slice(
            run_id=created["run_id"],
            session_id=session.session_id,
            resume_token=created["resume_token"],
            slice_seconds=60.0,
        )
        assert result.execution is Execution.COMPLETED, result.detail
        assert result.verdict is Verdict.PASSED, [a.to_json() for a in result.assertions]
        assert [step.step_id for step in result.steps] == ["save"]
        assert result.steps[0].dispatch_state.value == "dispatched"
        assert fixture.read_state() is not None
        assert policy.resolved_models and policy.resolved_models[-1] == PolicyConfig().model_id
    finally:
        driver.close()
        fixture.stop()
        journal.close()
