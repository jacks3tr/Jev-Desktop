"""MCP stdio transport: real subprocess, real protocol, real tool results.

No desktop windows are involved: the broker runs against the in-memory driver double, so
this suite is safe to run at any time.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import uuid
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parents[2]

from jev_desktop.broker import Broker, BrokerConfig
from jev_desktop.contracts import Limits, Operation
from jev_desktop.ipc import PipeServer
from jev_desktop.policy import NONE, HttpTransport, JevPolicy, PolicyConfig

from .fakes import FakeApp, FakeDriver, FakeElement, ScriptedDecision, ScriptedPolicy, choice_answer, fake_response
from .local_server import LocalTypeSafeServer

APP_REF = "app:" + "a" * 24
ELEMENTS = [
    FakeElement("button", "Save", operations=("CLICK",)),
    FakeElement("text", "Saved", value="no", text="no", operations=()),
]


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
def live_broker(tmp_path: Path):
    pipe = f"\\\\.\\pipe\\jev-mcp-{uuid.uuid4().hex[:12]}"
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
    broker.policy = ScriptedPolicy(
        [
            ScriptedDecision(Operation.CLICK, "Save"),
            ScriptedDecision(Operation.CLICK, "Save"),
        ]
    )
    broker.runtime.policy = broker.policy
    broker.start()
    server = PipeServer(name=pipe, handler=broker.handle, on_disconnect=broker.on_disconnect)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield {"pipe": pipe, "broker": broker, "driver": driver, "app": app, "tmp": tmp_path}
    server.stop()
    broker.close()


def _server_params(broker_env, tmp_path: Path) -> StdioServerParameters:
    environment = dict(os.environ)
    environment["JEV_DESKTOP_PIPE"] = broker_env["pipe"]
    environment["PYTHONPATH"] = str(ROOT / "src")
    environment["PYTHONIOENCODING"] = "utf-8"
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "jev_desktop.transports.mcp_stdio"],
        env=environment,
        cwd=str(ROOT),
    )


def test_mcp_tools_expose_the_same_engine(live_broker, tmp_path):
    log = (tmp_path / "mcp-stderr.log").open("w", encoding="utf-8")
    params = _server_params(live_broker, tmp_path)

    async def scenario() -> dict:
        async with stdio_client(params, errlog=log) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = sorted(tool.name for tool in tools.tools)
            observed = await session.call_tool("desktop_inspect", {"app_ref": APP_REF, "inline_image": True})
            text_blocks = [block for block in observed.content if getattr(block, "type", "") == "text"]
            image_blocks = [block for block in observed.content if getattr(block, "type", "") == "image"]
            payload = json.loads(text_blocks[0].text) if text_blocks else {}
            run = await session.call_tool("desktop_run", {"run": spec_payload(), "inline_image": True})
            run_text = [block for block in run.content if getattr(block, "type", "") == "text"]
            run_payload = json.loads(run_text[0].text) if run_text else {}
            status = await session.call_tool(
                "desktop_stop", {"run_id": run_payload.get("run_id"), "reason": "test finished"}
            )
            stop_text = [block for block in status.content if getattr(block, "type", "") == "text"]
            stop_payload = json.loads(stop_text[0].text) if stop_text else {}
        return {
            "tools": names,
            "inspect": payload,
            "images": len(image_blocks),
            "run": run_payload,
            "stop": stop_payload,
        }

    try:
        result = asyncio.run(scenario())
    finally:
        log.close()

    assert result["tools"] == ["desktop_act", "desktop_inspect", "desktop_run", "desktop_stop"]
    assert {element["name"] for element in result["inspect"]["elements"]} == {"Save", "Saved"}
    assert result["images"] >= 1, "the screenshot must arrive as MCP image content"
    assert result["run"]["execution"] == "completed"
    assert result["run"]["verdict"] == "passed"
    assert result["run"]["assertions"][0]["status"] == "passed"
    assert result["stop"]["status"] == "completed", "stopping must not rewrite a completed result"
    assert live_broker["driver"].executed, "the same engine executed the action"


def test_mcp_standalone_action_needs_no_test_and_consumes_inspection(live_broker, tmp_path):
    """Exercise the real stdio and pipe path; the driver boundary stays offline."""
    params = _server_params(live_broker, tmp_path)

    async def scenario():
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()

            async def call(name, args):
                result = await session.call_tool(name, args)
                return json.loads(next(block.text for block in result.content if block.type == "text"))

            inspected = await call(
                "desktop_inspect",
                {
                    "app_ref": APP_REF,
                    "window_refs": [live_broker["app"].window_ref],
                    "include_screenshot": False,
                },
            )
            action = {
                "operation": "CLICK",
                "snapshot_id": inspected["snapshot_id"],
                "access_token": inspected["access_token"],
                "window_ref": live_broker["app"].window_ref,
                "element_id": next(e["element_id"] for e in inspected["elements"] if e["name"] == "Save"),
            }
            denied = await call("desktop_act", {**action, "access_token": "wrong"})
            assert denied["error"]["code"] == "unauthorized"
            assert not live_broker["driver"].executed
            result = await call("desktop_act", action)
            assert "receipt" in result, result
            assert len(live_broker["driver"].executed) == 1
            repeated = await call("desktop_act", action)
            assert repeated["error"]["code"] == "invalid_request"
            assert len(live_broker["driver"].executed) == 1
            assert live_broker["broker"].ownership.active_lease() is None
            assert live_broker["broker"].journal.list_runs() == []
            inspected = await call("desktop_inspect", {"app_ref": APP_REF, "include_screenshot": False})
            selected = next(e["element_id"] for e in inspected["elements"] if e["name"] == "Save")
            with LocalTypeSafeServer() as provider:
                transport = HttpTransport()
                try:
                    live_broker["broker"].policy = JevPolicy(
                        transport=transport, config=PolicyConfig(endpoint=provider.endpoint), api_key="local-test-key"
                    )
                    provider.queue(
                        200,
                        fake_response(
                            "jev-1.13.0",
                            {
                                "operation": choice_answer("CLICK", ["CLICK", "WAIT", "ESCALATE"]),
                                "CLICK_target": choice_answer(selected, [selected, NONE]),
                            },
                        ),
                    )
                    selected_action = {
                        **action,
                        "snapshot_id": inspected["snapshot_id"],
                        "access_token": inspected["access_token"],
                        "target_description": "Save button",
                    }
                    selected_action.pop("element_id")
                    result = await call("desktop_act", selected_action)
                    assert "receipt" in result, result
                    assert len(provider.requests) == 1
                    assert len(live_broker["driver"].executed) == 2
                    assert live_broker["driver"].executed[-1].element_id == selected
                finally:
                    transport.close()

    asyncio.run(scenario())


def test_mcp_goal_handoff_returns_one_summary(live_broker, tmp_path):
    live_broker["broker"].policy = live_broker["broker"].runtime.policy = ScriptedPolicy(
        [
            ScriptedDecision(Operation.CLICK, "Save"),
            ScriptedDecision(Operation.DONE),
        ]
    )

    async def scenario():
        async with (
            stdio_client(_server_params(live_broker, tmp_path)) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.call_tool(
                "desktop_run",
                {
                    "task": {
                        "goal": "Save the document",
                        "app_ref": APP_REF,
                        "window_refs": [live_broker["app"].window_ref],
                        "max_actions": 1,
                    }
                },
            )
            return json.loads(next(block.text for block in result.content if block.type == "text"))

    result = asyncio.run(scenario())
    assert result["execution"] == "completed", result
    assert result["completion"] == "model_reported"
    assert result["metrics"]["actions"] == 1
    assert result["metrics"]["decisions"] == 2
    assert result["metrics"]["slices"] == 1
    assert "verdict" not in result and "assertions" not in result
    assert live_broker["broker"].ownership.active_lease() is None
