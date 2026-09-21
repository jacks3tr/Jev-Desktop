"""Acceptance tests: live Jev chooses controls in an installed, unmodified Notepad.

No scripted decisions, fake driver, window-message input, or fake clock. Artifacts and
screenshots remain in pytest's temporary directory on success and failure.
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from pathlib import Path

import pytest

from jev_desktop.broker import Broker, BrokerConfig
from jev_desktop.contracts import Envelope, Limits, RunSpec
from jev_desktop.drivers.windows import WindowsDriver
from jev_desktop.ipc import PipeClient, PipeServer

from .real_app import RealApp, run_spec

pytestmark = [pytest.mark.windows, pytest.mark.live]


def test_jev_notepad_save_and_reopen(tmp_path, jev_policy):
    target = tmp_path / "saved-by-jev.txt"
    text = f"Jev desktop acceptance {tmp_path.name}"
    with WindowsDriver(evidence_dir=tmp_path / "evidence") as driver:
        app = RealApp(driver, "notepad")
        try:
            spec = RunSpec.from_json(
                {
                    "goal": "Type the fixture document and save it through File > Save As.",
                    "purpose": "regression",
                    "interaction_mode": "user_path",
                    "app_ref": app.app_ref,
                    "expected_identity": {
                        "mode": "exe_hash",
                        "expect_exe": app.executable,
                        "expect_sha256": hashlib.sha256(Path(app.executable).read_bytes()).hexdigest(),
                    },
                    "steps": [
                        {
                            "step_id": "type",
                            "operation": "TYPE_TEXT",
                            "target_description": "document text area",
                            "fixture_reference": "text",
                        },
                        {"step_id": "file", "operation": "CLICK", "target_description": "File menu"},
                        {"step_id": "save-as", "operation": "CLICK", "target_description": "Save As in the File menu"},
                        {
                            "step_id": "filename",
                            "operation": "TYPE_TEXT",
                            "target_description": "File name field in Save As dialog",
                            "fixture_reference": "path",
                        },
                        {
                            "step_id": "save",
                            "operation": "CLICK",
                            "target_description": "Save button in Save As dialog",
                            "checkpoint": True,
                        },
                    ],
                    "fixtures": {"text": text, "path": str(target)},
                    "assertions": [
                        {
                            "assertion_id": "saved-content",
                            "evaluator": "artifact",
                            "property": "content_regex",
                            "target": {"path": str(target)},
                            "expected": {"regex": text},
                            "checkpoint": "save",
                            "deadline_s": 2,
                        }
                    ],
                    # Finishing exactly at the action budget must still run verification.
                    "limits": {
                        **Limits.defaults().to_json(),
                        "max_actions": 5,
                        "max_model_decisions": 12,
                        "slice_seconds": 90,
                    },
                    "scope": {"app_ref": app.app_ref, "max_elements": 240},
                }
            )
            outcome = run_spec(driver, jev_policy, spec, tmp_path)
            (tmp_path / "result.json").write_text(json.dumps(outcome, indent=2), encoding="utf-8")
            assert outcome["execution"] == "completed", outcome
            assert outcome["verdict"] == "passed", outcome
            assert outcome["actions"] == 5
            assert outcome["model_decisions"] >= 5
            assert jev_policy.resolved_models and set(jev_policy.resolved_models) == {jev_policy.config.model_id}
            assert outcome["evidence_bytes"] > 0
            assert target.read_text(encoding="utf-8-sig") == text
        finally:
            app.close()

        reopened = RealApp(driver, "notepad", args=[str(target)])
        try:
            snapshot = driver.observe(reopened.scope)
            assert any(text in (element.value or element.text or "") for element in snapshot.elements), (
                "saved text was not observed after reopening the file",
                snapshot.to_json(),
            )
        finally:
            reopened.close()


def test_real_pipe_retry_does_not_repeat_jev_or_native_input(tmp_path, jev_policy):
    """Retry the exact wire request against a real broker, live Jev, and real Notepad."""
    with WindowsDriver(evidence_dir=tmp_path / "evidence") as driver:
        app = RealApp(driver, "notepad")
        broker = Broker(BrokerConfig(home=tmp_path, approved_roots=(str(tmp_path),)), driver=driver)
        broker.policy = broker.runtime.policy = jev_policy
        pipe = rf"\\.\pipe\jev-acceptance-{uuid.uuid4().hex}"
        server = PipeServer(name=pipe, handler=broker.handle, on_disconnect=broker.on_disconnect)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = PipeClient(name=pipe, timeout_s=30)
        try:
            hello = client.request(Envelope.request("hello", {"client": "real-desktop-acceptance"}))
            assert hello.ok
            session_id = hello.result["session_id"]
            text = "A retried request must not append this twice"
            spec = {
                "goal": "Type the fixture text once",
                "app_ref": app.app_ref,
                "purpose": "regression",
                "interaction_mode": "user_path",
                "expected_identity": {
                    "mode": "exe_hash",
                    "expect_exe": app.executable,
                    "expect_sha256": hashlib.sha256(Path(app.executable).read_bytes()).hexdigest(),
                },
                "steps": [
                    {
                        "step_id": "type",
                        "operation": "TYPE_TEXT",
                        "target_description": "document text area",
                        "fixture_reference": "text",
                        "replace_existing": False,
                        "checkpoint": True,
                    }
                ],
                "fixtures": {"text": text},
                "assertions": [
                    {
                        "assertion_id": "text",
                        "evaluator": "uia_property",
                        "target": {"role": "edit"},
                        "property": "value",
                        "expected": {"equals": text},
                        "checkpoint": "type",
                        "deadline_s": 2,
                    }
                ],
            }
            request = Envelope.request("run", {"session_id": session_id, "spec": spec})
            first = client.request(request)
            assert first.ok, first.to_json()
            assert first.result["verdict"] == "passed", first.to_json()
            second = client.request(request)
            assert second.to_json() == first.to_json()
            assert len(jev_policy.resolved_models) == 1
            assert len(broker.journal.list_runs()) == 1
            assert len(broker.journal.actions_for_run(first.result["run_id"])) == 1
            snapshot = driver.observe(app.scope)
            assert any(element.value == text for element in snapshot.elements if element.role == "edit")
            (tmp_path / "result.json").write_text(json.dumps(first.to_json(), indent=2), encoding="utf-8")
        finally:
            client.close()
            server.stop()
            broker.ownership.force_release()
            broker.journal.close()
            app.close()
