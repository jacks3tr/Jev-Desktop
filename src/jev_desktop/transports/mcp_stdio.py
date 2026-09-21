"""MCP stdio transport: four tools over a client-launched subprocess.

stdout carries protocol frames only; every diagnostic goes to stderr. The adapter is a thin
client of the session broker, so an MCP client and the JSON CLI share one authorization,
journaling, and verification path. Nothing here depends on optional MCP background tasks,
sampling, or elicitation for baseline functionality.
"""

from __future__ import annotations

import base64
import json
import sys
from collections.abc import Mapping
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ImageContent, TextContent

from ..client import BrokerClient, BrokerError
from ..contracts import SCHEMA_VERSION

INSTRUCTIONS = """Host-desktop testing tools for the machine this session runs on.

Workflow: inspect an application to get opaque references, create a run with an immutable
test specification, then interpret the returned execution state and verdict. Execution and
verdict are separate: `completed` means the bounded sequence finished, not that the test
passed; `inconclusive` means the runner or environment could not establish a result.

Never treat a model-declared DONE, a toast, or a screenshot as proof. Evidence is returned
as references that can be fetched; screenshots are delivered as image content when the
caller can use it. Paused runs resume only through an explicit call with the run's current
resume token and may only supply fixture values, scoped visual assistance, or an explicitly
requested verifier result.
"""

server = MCPServer(name="jev-desktop", version="0.1.0", instructions=INSTRUCTIONS)
_client: BrokerClient | None = None


def client() -> BrokerClient:
    global _client
    if _client is None:
        _client = BrokerClient(client_name="mcp-stdio", autostart=True, timeout_s=3600.0)
    return _client


def _json_text(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def _images_from(payload: Mapping[str, Any]) -> list[ImageContent]:
    images: list[ImageContent] = []
    candidates: list[Mapping[str, Any]] = []
    if isinstance(payload.get("screenshot"), Mapping):
        candidates.append(payload["screenshot"])
    for image in payload.get("images") or []:
        if isinstance(image, Mapping):
            candidates.append(image)
    for reference in payload.get("evidence") or []:
        if isinstance(reference, Mapping) and reference.get("kind") == "screenshot":
            candidates.append(reference)
    seen: set[str] = set()
    for candidate in candidates:
        encoded = candidate.get("base64")
        evidence_id = str(candidate.get("evidence_id") or "")
        if not encoded or evidence_id in seen:
            continue
        seen.add(evidence_id)
        try:
            data = base64.b64decode(encoded)
        except (ValueError, TypeError):
            continue
        images.append(ImageContent(type="image", data=base64.b64encode(data).decode("ascii"), mime_type="image/png"))
    return images


def _content(payload: Mapping[str, Any]) -> list[Any]:
    content: list[Any] = [TextContent(type="text", text=_json_text(payload))]
    content.extend(_images_from(payload))
    return content


def _error_payload(exc: BrokerError) -> dict[str, Any]:
    return {
        "ok": False,
        "error": {"code": exc.code, "message": exc.message, "detail": exc.detail},
        "schema_version": SCHEMA_VERSION,
    }


@server.tool(
    description=(
        "Discover observable applications and windows, or inspect one application and return a "
        "structured observation with an opaque app_ref, window refs, indexed elements, coverage "
        "limitations, and an optional scoped screenshot. Observation is authorized like input."
    )
)
def desktop_inspect(
    app_ref: str | None = None,
    query: str | None = None,
    max_elements: int = 240,
    include_screenshot: bool = True,
    inline_image: bool = True,
) -> list[Any]:
    params: dict[str, Any] = {
        "screenshot": include_screenshot,
        "inline_image": inline_image,
        "scope": {"max_elements": max_elements},
    }
    if app_ref:
        params["app_ref"] = app_ref
    if query:
        params["query"] = query
    try:
        payload = client().call("inspect", params, timeout_s=120.0)
    except BrokerError as exc:
        payload = _error_payload(exc)
    return _content(payload)


@server.tool(
    description=(
        "Start or resume a bounded host-desktop test. A new run takes an immutable specification "
        "(application identity, required steps, assertions, fixtures, limits) and returns execution "
        "state, verdict, step records, assertion results, and evidence references. Resume requires the "
        "run_id and current resume_token and may only supply fixture values, scoped visual assistance, "
        "or an explicitly requested verifier result."
    )
)
def desktop_run(
    run: dict[str, Any] | None = None,
    run_id: str | None = None,
    resume_token: str | None = None,
    slice_seconds: float | None = None,
    fixtures: dict[str, str] | None = None,
    visual_results: dict[str, Any] | None = None,
    verifier_results: dict[str, Any] | None = None,
    inline_image: bool = True,
) -> list[Any]:
    params: dict[str, Any] = {"inline_image": inline_image}
    if run is not None:
        params["run"] = run
    if run_id:
        params["run_id"] = run_id
    if resume_token:
        params["resume_token"] = resume_token
    if slice_seconds:
        params["slice_seconds"] = slice_seconds
    if fixtures or visual_results or verifier_results:
        params["inputs"] = {
            "fixtures": fixtures or {},
            "visual_results": visual_results or {},
            "verifier_results": verifier_results or {},
        }
    try:
        payload = client().call("run", params, timeout_s=3600.0)
    except BrokerError as exc:
        payload = _error_payload(exc)
    return _content(payload)


@server.tool(
    description=(
        "Execute one caller-directed interaction through the same authorization, freshness, journaling, "
        "and receipt path as a run. Use it primarily for visual fallback after desktop_inspect returned "
        "needs_visual_assistance. The action references an element from the current snapshot and the "
        "run's resume_token."
    )
)
def desktop_act(
    run_id: str,
    resume_token: str,
    operation: str,
    element_id: str | None = None,
    mode: str = "user_path",
    text: str | None = None,
    option_label: str | None = None,
    hotkey: list[str] | None = None,
    window_ref: str | None = None,
    scroll: dict[str, Any] | None = None,
    inline_image: bool = False,
) -> list[Any]:
    action: dict[str, Any] = {
        "operation": operation,
        "mode": mode,
        "element_id": element_id,
        "window_ref": window_ref,
        "text": text,
        "option_label": option_label,
        "hotkey": hotkey or [],
        "scroll": scroll or {},
    }
    params = {"run_id": run_id, "resume_token": resume_token, "action": action, "inline_image": inline_image}
    try:
        payload = client().call("act", params, timeout_s=600.0)
    except BrokerError as exc:
        payload = _error_payload(exc)
    return _content(payload)


@server.tool(
    description=(
        "Cancel the caller's run, release desktop control, or operate the local emergency stop. The "
        "emergency stop is independent of any run, model, or capture work: it blocks all further input "
        "until it is explicitly cleared."
    )
)
def desktop_stop(
    run_id: str | None = None,
    emergency: bool = False,
    clear_emergency: bool = False,
    reason: str | None = None,
) -> list[Any]:
    params: dict[str, Any] = {"emergency": emergency, "clear": clear_emergency}
    if run_id:
        params["run_id"] = run_id
    if reason:
        params["reason"] = reason
    try:
        payload = client().call("stop", params, timeout_s=60.0)
    except BrokerError as exc:
        payload = _error_payload(exc)
    return _content(payload)


def main() -> int:
    print("jev-desktop MCP server on stdio", file=sys.stderr, flush=True)
    try:
        server.run("stdio")
    finally:
        if _client is not None:
            _client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
