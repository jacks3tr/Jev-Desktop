"""JSON CLI transport: the same engine and broker, without MCP.

Every subcommand prints one JSON object to stdout and diagnostics to stderr. Exit code 0
means the call succeeded; 2 means the call succeeded but the run is not a pass
(paused/blocked/cancelled/error or a verdict other than passed); 1 means the call failed.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..client import BrokerClient, BrokerError, load_json_argument
from ..contracts import SCHEMA_VERSION
from ..ipc import pipe_name

NON_PASS_EXIT = 2


def _emit(payload: Mapping[str, Any], *, pretty: bool) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2 if pretty else None, default=str))


def _make_client(args: argparse.Namespace, *, name: str) -> BrokerClient:
    return BrokerClient(
        pipe=args.pipe,
        client_name=name,
        autostart=not args.no_autostart,
        timeout_s=args.timeout,
    )


def _fixtures(pairs: list[str] | None) -> dict[str, str]:
    values: dict[str, str] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise SystemExit(f"--fixture expects name=value, got {pair!r}")
        name, value = pair.split("=", 1)
        values[name.strip()] = value
    return values


def _result_envelope(payload: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
    execution = str(payload.get("execution") or "")
    if "completion" in payload:
        ok = execution == "completed"
        return {"ok": ok, **payload}, 0 if ok else NON_PASS_EXIT
    verdict = str(payload.get("verdict") or "")
    ok = execution == "completed" and verdict == "passed"
    envelope = {"ok": ok, "execution": execution or None, "verdict": verdict or None, **payload}
    return envelope, 0 if ok else NON_PASS_EXIT


# --------------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------------


def cmd_inspect(args: argparse.Namespace) -> int:
    params: dict[str, Any] = {
        "screenshot": not args.no_screenshot,
        "inline_image": args.inline_image,
        "scope": {"max_elements": args.max_elements, "window_refs": args.window},
    }
    if args.app_ref:
        params["app_ref"] = args.app_ref
    if args.query:
        params["query"] = args.query
    if args.run_id:
        params.update(run_id=args.run_id, resume_token=args.resume_token)
    with _ClientContext(args, "cli-inspect") as client:
        payload = client.call("inspect", params, timeout_s=args.timeout)
    _emit(payload, pretty=args.pretty)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    params: dict[str, Any] = {"inline_image": args.inline_image, "start_only": args.start_only}
    if args.task:
        params["task"] = load_json_argument(args.task)
    if args.spec:
        params["run"] = load_json_argument(args.spec)
    if args.run_id:
        params["run_id"] = args.run_id
    if args.resume_token:
        params["resume_token"] = args.resume_token
    if args.slice_seconds:
        params["slice_seconds"] = args.slice_seconds
    fixtures = _fixtures(args.fixture)
    visual = load_json_argument(args.visual) if args.visual else {}
    verifier = load_json_argument(args.verifier) if args.verifier else {}
    if fixtures or visual or verifier:
        params["inputs"] = {"fixtures": fixtures, "visual_results": visual, "verifier_results": verifier}
    if "task" not in params and "run" not in params and "run_id" not in params:
        print("run requires --task, --spec, or --run-id", file=sys.stderr)
        return 1
    with _ClientContext(args, "cli-run") as client:
        payload = client.call("run", params, timeout_s=args.timeout)
    envelope, code = _result_envelope(payload)
    _emit(envelope, pretty=args.pretty)
    return code


def cmd_act(args: argparse.Namespace) -> int:
    action = load_json_argument(args.action) if args.action else {}
    for field, value in (
        ("operation", args.operation),
        ("snapshot_id", args.snapshot),
        ("step_id", args.step),
        ("mode", args.mode),
        ("element_id", args.element),
        ("text", args.text),
        ("option_label", args.option),
        ("window_ref", args.window),
    ):
        if value is not None:
            action[field] = value
    if args.hotkey:
        action["hotkey"] = args.hotkey
    params = {
        "run_id": args.run_id,
        "resume_token": args.resume_token,
        "action": action,
        "access_token": args.access_token,
        "target_description": args.target,
        "inline_image": args.inline_image,
    }
    with _ClientContext(args, "cli-act") as client:
        payload = client.call("act", params, timeout_s=args.timeout)
    _emit(payload, pretty=args.pretty)
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    if args.emergency:
        from ..ownership import emergency_clear, emergency_signal

        ok = emergency_clear() if args.clear else emergency_signal()
        _emit({"emergency_stop": "cleared" if args.clear else "set", "ok": ok}, pretty=args.pretty)
        return 0 if ok else 1
    params: dict[str, Any] = {"emergency": args.emergency, "clear": args.clear}
    params["resume_token"] = args.resume_token
    if args.run_id:
        params["run_id"] = args.run_id
    if args.reason:
        params["reason"] = args.reason
    with _ClientContext(args, "cli-stop") as client:
        payload = client.call("stop", params, timeout_s=args.timeout)
    _emit(payload, pretty=args.pretty)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    params = {"run_id": args.run_id} if args.run_id else {}
    params["resume_token"] = args.resume_token
    with _ClientContext(args, "cli-status") as client:
        payload = client.call("status", params, timeout_s=args.timeout)
    _emit(payload, pretty=args.pretty)
    return 0


def cmd_evidence(args: argparse.Namespace) -> int:
    params = {"evidence_id": args.evidence_id, "inline_image": True, "resume_token": args.resume_token}
    with _ClientContext(args, "cli-evidence") as client:
        payload = client.call("evidence", params, timeout_s=args.timeout)
    if args.out:
        encoded = payload.get("base64")
        if not encoded:
            print("evidence has no inline payload; try again with the broker running", file=sys.stderr)
            return 1
        Path(args.out).write_bytes(base64.b64decode(encoded))
        payload = {key: value for key, value in payload.items() if key != "base64"}
        payload["written_to"] = args.out
    _emit(payload, pretty=args.pretty)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Environment check: this is what to paste into a bug report."""
    checks: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "python": sys.version,
        "pipe_name": pipe_name("broker"),
        "session": {},
        "policy_key_env": os.environ.get("TYPESAFE_API_KEY") is not None,
    }
    try:
        from ..ownership import session_description

        checks["session"] = {"description": session_description()}
    except Exception as exc:
        checks["session"] = {"error": str(exc)}
    try:
        with _ClientContext(args, "cli-doctor") as client:
            handshake = client.handshake()
        checks["broker"] = {
            "reachable": True,
            "session_id": handshake.get("session_id"),
            "health": handshake.get("broker"),
            "capabilities": handshake.get("capabilities"),
        }
    except (BrokerError, OSError, TimeoutError) as exc:
        checks["broker"] = {"reachable": False, "error": str(exc)}
    _emit(checks, pretty=True)
    return 0


def cmd_broker(args: argparse.Namespace) -> int:
    from ..broker import Broker, BrokerConfig
    from ..broker import main as broker_main

    if args.print_config:
        return broker_main(["--print-config"] + (["--config", args.config] if args.config else []))
    config = BrokerConfig.load(args.config)
    broker = Broker(config)
    broker.serve_forever()
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .mcp_stdio import main as mcp_main

    return mcp_main()


# --------------------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------------------


class _ClientContext:
    """Small context manager so each command can reuse the shared client settings."""

    def __init__(self, args: argparse.Namespace, name: str) -> None:
        self.args = args
        self.name = name
        self.client: BrokerClient | None = None

    def __enter__(self) -> BrokerClient:
        self.client = _make_client(self.args, name=self.name)
        return self.client

    def __exit__(self, *_exc: object) -> None:
        if self.client is not None:
            self.client.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jev-desktop", description="Use Windows applications with Jev Desktop")
    parser.add_argument("--pipe", help="broker pipe name (default: per-user, per-logon session)")
    parser.add_argument("--timeout", type=float, default=3600.0, help="client timeout in seconds")
    parser.add_argument("--no-autostart", action="store_true", help="fail instead of starting a broker")
    parser.add_argument("--pretty", action="store_true", help="pretty-print JSON output")
    sub = parser.add_subparsers(dest="command", required=True)

    inspect = sub.add_parser("inspect", help="discover applications or observe one application")
    inspect.add_argument("--app-ref")
    inspect.add_argument("--query")
    inspect.add_argument("--window", action="append", default=[])
    inspect.add_argument("--run-id")
    inspect.add_argument("--resume-token")
    inspect.add_argument("--max-elements", type=int, default=240)
    inspect.add_argument("--no-screenshot", action="store_true")
    inspect.add_argument("--inline-image", action="store_true")
    inspect.set_defaults(func=cmd_inspect)

    run = sub.add_parser("run", help="hand off a desktop task or execute a predefined workflow")
    run.add_argument("--task", help="task JSON: goal, app_ref, window_refs, texts, hotkeys, and limits")
    run.add_argument("--spec", help="specification file, '-' for stdin, or inline JSON")
    run.add_argument(
        "--start-only", action="store_true", help="create a run for caller-directed actions without invoking Jev"
    )
    run.add_argument("--run-id")
    run.add_argument("--resume-token")
    run.add_argument("--slice-seconds", type=float)
    run.add_argument("--fixture", action="append", help="name=value fixture (repeatable)")
    run.add_argument("--visual", help="visual assistance results (file or inline JSON)")
    run.add_argument("--verifier", help="caller verifier results (file or inline JSON)")
    run.add_argument("--inline-image", action="store_true")
    run.set_defaults(func=cmd_run)

    act = sub.add_parser("act", help="execute one caller-directed interaction")
    act.add_argument("--run-id")
    act.add_argument("--resume-token")
    act.add_argument("--access-token")
    act.add_argument("--target", help="ask Jev to select a control by description")
    act.add_argument("--snapshot")
    act.add_argument("--step")
    act.add_argument("--action", help="action JSON (file, '-' or inline)")
    act.add_argument("--operation")
    act.add_argument("--mode")
    act.add_argument("--element")
    act.add_argument("--window")
    act.add_argument("--text")
    act.add_argument("--option")
    act.add_argument("--hotkey", nargs="*")
    act.add_argument("--inline-image", action="store_true")
    act.set_defaults(func=cmd_act)

    stop = sub.add_parser("stop", help="cancel a run or operate the emergency stop")
    stop.add_argument("--run-id")
    stop.add_argument("--resume-token")
    stop.add_argument("--reason")
    stop.add_argument("--emergency", action="store_true")
    stop.add_argument("--clear", action="store_true")
    stop.set_defaults(func=cmd_stop)

    status = sub.add_parser("status", help="broker and run status")
    status.add_argument("--run-id")
    status.add_argument("--resume-token")
    status.set_defaults(func=cmd_status)

    evidence = sub.add_parser("evidence", help="fetch an evidence reference")
    evidence.add_argument("--evidence-id", required=True)
    evidence.add_argument("--resume-token", help="run resume token or inspection access token")
    evidence.add_argument("--out")
    evidence.set_defaults(func=cmd_evidence)

    doctor = sub.add_parser("doctor", help="environment and broker diagnostics")
    doctor.set_defaults(func=cmd_doctor)

    broker = sub.add_parser("broker", help="run the broker in the foreground")
    broker.add_argument("--config")
    broker.add_argument("--print-config", action="store_true")
    broker.set_defaults(func=cmd_broker)

    serve = sub.add_parser("serve", help="run the MCP stdio server")
    serve.set_defaults(func=cmd_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except BrokerError as exc:
        _emit(
            {"ok": False, "error": {"code": exc.code, "message": exc.message, "detail": exc.detail}}, pretty=args.pretty
        )
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
