"""Broker over the real named pipe: authorization, tool methods, evidence, cross-client locking."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path

import pytest

import jev_desktop
from jev_desktop import client as client_module
from jev_desktop.broker import Broker, BrokerConfig, fit_frame
from jev_desktop.client import BrokerClient, BrokerError
from jev_desktop.contracts import Envelope, Limits, Operation, new_id
from jev_desktop.ipc import MAX_MESSAGE_BYTES, PipeClient, PipeServer
from jev_desktop.policy import PolicyConfig

from .fakes import FakeApp, FakeDriver, FakeElement, ScriptedDecision, ScriptedPolicy

APP_REF = "app:" + "a" * 24
ELEMENTS = [
    FakeElement("button", "Save", operations=("CLICK",)),
    FakeElement("text", "Saved", value="no", text="no", operations=()),
]


def test_config_timeout_load_and_save(tmp_path: Path):
    path = tmp_path / "config.json"
    path.write_text("{}", encoding="utf-8")
    config = BrokerConfig.load(path)
    assert config.policy.timeout_s is None
    config.save(path)
    assert BrokerConfig.load(path).policy.timeout_s is None
    config.policy = PolicyConfig(timeout_s=12.5)
    config.save(path)
    assert BrokerConfig.load(path).policy.timeout_s == 12.5


def spec_payload(*, goal: str = "regression: save the document") -> dict:
    return {
        "goal": goal,
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
def broker_env(tmp_path: Path):
    pipe = f"\\\\.\\pipe\\jev-test-{uuid.uuid4().hex[:12]}"
    app = FakeApp(app_ref=APP_REF, window_ref="win:" + "b" * 24, elements=list(ELEMENTS))
    driver = FakeDriver(app, evidence_dir=tmp_path / "evidence")
    config = BrokerConfig(
        home=tmp_path,
        evidence_dir=tmp_path / "evidence",
        journal_path=tmp_path / "journal.sqlite",
        approved_roots=(str(tmp_path),),
        policy=PolicyConfig(),
    )
    broker = Broker(config, driver=driver)
    broker.policy = ScriptedPolicy([ScriptedDecision(Operation.CLICK, "Save")])
    broker.runtime.policy = broker.policy
    broker.start()
    server = PipeServer(name=pipe, handler=broker.handle, on_disconnect=broker.on_disconnect)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="test-pipe-server")
    thread.start()
    clients: list[BrokerClient] = []

    def make(name: str = "test-client") -> BrokerClient:
        client = BrokerClient(pipe=pipe, client_name=name, autostart=False, timeout_s=30.0)
        clients.append(client)
        return client

    yield {
        "broker": broker,
        "driver": driver,
        "app": app,
        "make": make,
        "pipe": pipe,
        "server": server,
        "thread": thread,
    }

    for client in clients:
        client.close()
    server.stop()
    broker.close()


def test_run_completes_and_returns_verdict_with_evidence(broker_env):
    client = broker_env["make"]()
    payload = client.call("run", {"run": spec_payload(), "inline_image": True}, timeout_s=60)
    assert payload["execution"] == "completed"
    assert payload["verdict"] == "passed"
    assert payload["steps"][0]["dispatch_state"] == "dispatched"
    assert payload["assertions"][0]["status"] == "passed"
    assert payload["images"], "checkpoint screenshots are delivered inline when requested"

    status = client.call("status", {"run_id": payload["run_id"]})
    assert status["run"]["completed_steps"] == ["save"]

    evidence = client.call("evidence", {"evidence_id": payload["evidence"][0]["evidence_id"]})
    assert evidence["base64"], "evidence is fetchable by reference"


def test_unauthorized_session_and_unknown_method_are_refused(broker_env):
    raw = PipeClient(name=broker_env["pipe"], timeout_s=10.0)
    raw.connect()
    forged = Envelope.request("status", {"session_id": "sess:" + "f" * 24})
    response = raw.request(forged, timeout_s=10.0)
    assert response.ok is False and response.error["code"] == "unauthorized"

    unknown = Envelope.request("delete_everything", {})
    response = raw.request(unknown, timeout_s=10.0)
    assert response.ok is False and response.error["code"] == "unknown_method"
    raw.close()


def test_queued_decision_cannot_replay_after_a_pause(broker_env):
    """A paused run keeps its state but never resumes input on its own."""
    client = broker_env["make"]()
    payload = client.call("run", {"run": spec_payload()}, timeout_s=60)
    driver = broker_env["driver"]
    assert payload["execution"] == "completed"
    assert len(driver.executed) == 1

    # Resuming with a stale resume token must be refused, and must not dispatch anything.
    with pytest.raises(BrokerError) as failure:
        client.call("run", {"run_id": payload["run_id"], "resume_token": "resume:" + "0" * 24})
    assert failure.value.code == "invalid_request"
    assert len(driver.executed) == 1


def test_second_client_cannot_take_over_a_leased_desktop(broker_env):
    first = broker_env["make"]("client-a")
    broker = broker_env["broker"]
    # Park a run that holds the lease: request a WAIT-only slice so it pauses on budget.
    broker.policy.script = [ScriptedDecision(Operation.CLICK, "Save"), ScriptedDecision(Operation.CLICK, "Save")]
    created = first.call("run", {"run": spec_payload(goal="regression: two saves"), "slice_seconds": 0.001})["run_id"]
    lease = broker.ownership.active_lease()
    assert lease is not None and lease.run_id == created

    second = broker_env["make"]("client-b")
    with pytest.raises(BrokerError) as failure:
        second.call("run", {"run": spec_payload(goal="regression: competing client")})
    assert failure.value.code in {"unauthorized", "invalid_request"}


def test_stop_releases_the_lease(broker_env):
    client = broker_env["make"]()
    broker = broker_env["broker"]
    broker.policy.script = [ScriptedDecision(Operation.CLICK, "Save"), ScriptedDecision(Operation.CLICK, "Save")]
    run_id = client.call("run", {"run": spec_payload(), "slice_seconds": 0.001})["run_id"]
    broker.policy.script.append(ScriptedDecision(Operation.CLICK, "Save"))
    stopped = client.call("stop", {"run_id": run_id, "reason": "test"})
    assert stopped["status"] == "cancelled"
    assert broker.ownership.active_lease() is None
    assert broker.ownership.flags(run_id).cancelled is True


def test_optional_capture_failure_preserves_inspection(broker_env, monkeypatch):
    from jev_desktop.contracts import DriverError

    def unavailable(**kwargs):
        raise DriverError("window is covered")

    monkeypatch.setattr(broker_env["driver"], "capture", unavailable)
    observed = broker_env["make"]().call("inspect", {"app_ref": APP_REF, "screenshot": True})
    assert observed["elements"] and observed["access_token"]
    assert observed["screenshot"] is None
    assert observed["screenshot_error"]["reason"] == "DriverError"


def test_stop_does_not_wait_behind_an_in_flight_run(broker_env):
    client = broker_env["make"]()
    broker = broker_env["broker"]
    entered, release = threading.Event(), threading.Event()
    decide = broker.policy.decide

    def gated(**kwargs):
        entered.set()
        release.wait(30)
        return decide(**kwargs)

    broker.policy.decide = gated
    outcome: dict = {}

    def run() -> None:
        try:
            outcome["run"] = client.call("run", {"run": spec_payload()}, timeout_s=60)
        except BrokerError as exc:
            outcome["error"] = exc

    runner = threading.Thread(target=run, daemon=True)
    runner.start()
    try:
        assert entered.wait(10)
        started = time.monotonic()
        stopped = client.call("stop", {"reason": "halt"}, timeout_s=10)
        assert time.monotonic() - started < 5, "stop queued behind the running slice"
    finally:
        release.set()
        runner.join(30)
    assert stopped["status"] == "cancelled"
    assert not broker_env["driver"].executed, "a stopped run dispatches nothing"


def test_shutdown_request_stops_the_server_from_its_own_handler(broker_env):
    broker = broker_env["broker"]
    broker._server = broker_env["server"]
    assert broker_env["make"]().call("shutdown") == {"stopping": True}
    broker_env["thread"].join(5)
    assert not broker_env["thread"].is_alive()


def test_second_broker_exits_before_starting_its_driver(tmp_path: Path, monkeypatch):
    pipe = f"\\\\.\\pipe\\jev-test-{uuid.uuid4().hex[:12]}"
    owner = PipeServer(name=pipe, handler=lambda envelope, info: None)
    owner.claim()
    owner_thread = threading.Thread(target=owner.serve_forever, daemon=True)
    owner_thread.start()
    monkeypatch.setenv("JEV_DESKTOP_PIPE", pipe)
    app = FakeApp(app_ref=APP_REF, window_ref="win:" + "b" * 24, elements=list(ELEMENTS))
    driver = FakeDriver(app, evidence_dir=tmp_path / "evidence")
    started: list[bool] = []
    monkeypatch.setattr(driver, "start", lambda: started.append(True))
    broker = Broker(BrokerConfig(home=tmp_path, evidence_dir=tmp_path / "evidence"), driver=driver)
    try:
        with pytest.raises(OSError):
            broker.serve_forever()
        assert not started, "the driver and journal recovery must not run without the pipe"
    finally:
        broker.journal.close()
        owner.stop()
        owner_thread.join(5)


def test_autostarted_broker_serves_the_pipe_the_client_dials(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(client_module.subprocess, "Popen", lambda args, **kwargs: captured.update(kwargs))
    BrokerClient(pipe="\\\\.\\pipe\\jev-custom", autostart=True)._spawn_broker()
    environment = captured["env"]
    assert environment["JEV_DESKTOP_PIPE"] == "\\\\.\\pipe\\jev-custom"
    source = str(Path(jev_desktop.__file__).resolve().parents[1])
    assert source in environment["PYTHONPATH"].split(os.pathsep)


def test_oversized_response_drops_inline_images_but_keeps_the_resume_token():
    image = "A" * (3 * 1024 * 1024)
    images = [{"evidence_id": f"ev:{index}", "base64": image} for index in range(3)]
    response = Envelope.success(new_id("req"), {"run_id": "run:x", "resume_token": "resume:y", "images": images})
    fitted = fit_frame(response)
    assert fitted.ok and fitted.result is not None
    assert fitted.result["resume_token"] == "resume:y"
    assert [item["evidence_id"] for item in fitted.result["images"]] == ["ev:0", "ev:1", "ev:2"]
    assert sum("base64" in item for item in fitted.result["images"]) == 2, "drop only what is needed"
    assert len(json.dumps(fitted.to_json()).encode("utf-8")) < MAX_MESSAGE_BYTES

    bulky = Envelope.success(new_id("req"), {"run_id": "run:x", "resume_token": "resume:y", "observation": "x" * 5000})
    fallback = fit_frame(bulky, limit=1000)
    assert fallback.ok is False and fallback.error is not None
    assert fallback.error["code"] == "response_too_large"
    assert fallback.error["detail"] == {"run_id": "run:x", "resume_token": "resume:y"}


def test_inspect_query_filters_elements_not_the_application(broker_env):
    broker_env["app"].elements[:0] = [FakeElement("button", name) for name in ("Minimize", "Maximize", "Close")]
    observed = broker_env["make"]().call(
        "inspect", {"app_ref": APP_REF, "query": "save", "screenshot": False, "scope": {"max_elements": 1}}
    )
    assert [element["name"] for element in observed["elements"]] == ["Save"]
    assert observed["application"]["app_ref"] == APP_REF
    assert any("query" in note for note in observed["truncation"])
