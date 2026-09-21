"""JSON CLI transport: identical engine, identical verdicts, no MCP involved.

Uses the in-memory driver double, so no desktop windows are created.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

from jev_desktop.broker import Broker, BrokerConfig
from jev_desktop.contracts import Limits, Operation
from jev_desktop.ipc import PipeServer
from jev_desktop.policy import PolicyConfig

from .fakes import FakeApp, FakeDriver, FakeElement, ScriptedDecision, ScriptedPolicy

APP_REF = "app:" + "a" * 24


def spec_payload() -> dict:
    return {
        "goal": "regression: save the document",
        "purpose": "regression",
        "interaction_mode": "user_path",
        "app_ref": APP_REF,
        "expected_identity": {"mode": "exe_hash"},
        "launch_config_id": None,
        "steps": [
            {"step_id": "save", "operation": "CLICK", "target_description": "the Save button", "checkpoint": True}
        ],
        "assertions": [
            {
                "assertion_id": "saved-flag",
                "evaluator": "uia_property",
                "target": {"role": "text", "name": "Saved"},
                "property": "value",
                "expected": {"equals": "yes"},
                "checkpoint": "save",
            }
        ],
        "fixtures": {},
        "secret_refs": {},
        "limits": Limits.defaults().to_json(),
        "scope": {"app_ref": APP_REF, "max_elements": 60},
        "allow_restart": False,
    }


@pytest.fixture()
def cli_env(tmp_path: Path):
    pipe = f"\\\\.\\pipe\\jev-cli-{uuid.uuid4().hex[:12]}"
    app = FakeApp(
        app_ref=APP_REF,
        window_ref="win:" + "b" * 24,
        elements=[
            FakeElement("button", "Save", operations=("CLICK",)),
            FakeElement("edit", "Name", value="", editable=True, operations=("TYPE_TEXT", "CLICK")),
            FakeElement("text", "Saved", value="no", text="no", operations=()),
        ],
    )
    driver = FakeDriver(app, evidence_dir=tmp_path / "evidence")
    broker = Broker(
        BrokerConfig(
            home=tmp_path,
            evidence_dir=tmp_path / "evidence",
            journal_path=tmp_path / "journal.sqlite",
            approved_roots=(str(tmp_path),),
            policy=PolicyConfig(),
        ),
        driver=driver,
    )
    broker.policy = ScriptedPolicy([ScriptedDecision(Operation.CLICK, "Save")] * 4)
    broker.runtime.policy = broker.policy
    broker.start()
    server = PipeServer(name=pipe, handler=broker.handle, on_disconnect=broker.on_disconnect)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield {"pipe": pipe, "broker": broker, "driver": driver, "tmp": tmp_path}
    server.stop()
    broker.close()


def run_cli(cli_env, *args: str) -> tuple[int, dict]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT / "src")
    environment["JEV_DESKTOP_PIPE"] = cli_env["pipe"]
    process = subprocess.run(
        [sys.executable, "-m", "jev_desktop.transports.cli", *args],
        capture_output=True,
        text=True,
        env=environment,
        cwd=str(ROOT),
        timeout=120,
    )
    stdout = process.stdout.strip()
    payload = json.loads(stdout) if stdout.startswith("{") else {"raw": stdout}
    return process.returncode, payload


def test_cli_runs_the_same_engine_and_verdict(cli_env, tmp_path):
    code, doctor = run_cli(cli_env, "--pretty", "doctor")
    assert code == 0 and doctor["broker"]["reachable"] is True
    assert doctor["broker"]["capabilities"]["transport"] == "named_pipe"

    code, listing = run_cli(cli_env, "inspect")
    assert code == 0 and listing["applications"][0]["app_ref"] == APP_REF

    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps(spec_payload()), encoding="utf-8")
    code, result = run_cli(cli_env, "run", "--spec", str(spec_file))
    assert code == 0, result
    assert result["ok"] is True
    assert result["execution"] == "completed" and result["verdict"] == "passed"
    assert result["assertions"][0]["status"] == "passed"

    code, status = run_cli(cli_env, "status", "--run-id", result["run_id"])
    assert code == 1, "a new CLI connection must not read a run using its ID alone"
    code, status = run_cli(cli_env, "status", "--run-id", result["run_id"], "--resume-token", result["resume_token"])
    assert code == 0 and status["run"]["completed_steps"] == ["save"]

    evidence_id = result["evidence"][0]["evidence_id"]
    out_file = tmp_path / "evidence.bin"
    code, fetched = run_cli(
        cli_env,
        "evidence",
        "--evidence-id",
        evidence_id,
        "--resume-token",
        result["resume_token"],
        "--out",
        str(out_file),
    )
    assert code == 0 and fetched["written_to"] == str(out_file) and out_file.stat().st_size > 0


def test_cli_reports_a_paused_run_with_a_non_zero_status(cli_env, tmp_path):
    spec = spec_payload()
    spec["steps"][0] = {
        "step_id": "type-name",
        "operation": "TYPE_TEXT",
        "target_description": "the Name field",
        "fixture_reference": "name_value",
    }
    spec["fixtures"] = {"name_value": None}
    spec["assertions"][0]["checkpoint"] = "type-name"
    spec_file = tmp_path / "spec-paused.json"
    spec_file.write_text(json.dumps(spec), encoding="utf-8")
    code, result = run_cli(cli_env, "run", "--spec", str(spec_file))
    assert code == 2, f"a non-pass run must be distinguishable from a failure and from success: {result}"
    assert result["execution"] == "paused" and result["reason"] == "needs_text"
    assert result["resume_token"], "the caller receives a resumable checkpoint"

    # The resumed slice needs a decision for the typing step it can now perform.
    cli_env["broker"].policy.script = [ScriptedDecision(Operation.TYPE_TEXT, "Name")]
    code, resumed = run_cli(
        cli_env,
        "run",
        "--run-id",
        result["run_id"],
        "--resume-token",
        result["resume_token"],
        "--fixture",
        "name_value=Ada Lovelace",
    )
    assert resumed["execution"] in {"completed", "paused"}
    assert cli_env["driver"].executed, "the fixture reached the native boundary"
