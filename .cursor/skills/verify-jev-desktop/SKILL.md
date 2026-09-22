---
name: verify-jev-desktop
description: Verify Jev Desktop's Windows CLI and MCP behavior, including discovery, direct input, task handoffs, and interruption recovery after changes.
---

# Verify Jev Desktop

Run commands from the repository root in PowerShell. Primary surface: CLI/MCP over a Windows named-pipe broker; secondary surface: real Windows applications controlled by that broker. Read [the feature map](features/README.md) before selecting coverage.

## Launch

Requires Windows with an interactive desktop and Python 3.12+. Use the checkout's installed Python environment:

```powershell
$env:PYTHONIOENCODING = 'utf-8'
python -m pip install -e ".[dev]"
python .cursor/skills/verify-jev-desktop/scripts/smoke.py
```

The helper hosts a real `Broker` and Windows driver with temporary journal storage, a UUID pipe, and CLI subprocesses pinned to this checkout through `PYTHONPATH`. A successful doctor handshake is readiness. It shuts down in `finally`, including failed drives. The helper sets UTF-8 for captured CLI output because Unicode window titles can fail under Windows cp1252. It wakes the listener with one final pipe connection after `stop()`, because `stop()` alone does not unblock `ConnectNamedPipe`. No key is required for this smoke check. The helper makes no policy calls and injects no input.

For an interactive drive, use the documented production entry point in a dedicated terminal:

```powershell
python -m jev_desktop.transports.cli broker
```

This uses the default per-logon pipe and local configuration. First establish that no other broker/agent owns that session; never replace or terminate an existing instance. Stop a broker you launched with Ctrl+C in its owning terminal. The CLI's `--pipe` / `JEV_DESKTOP_PIPE` controls clients only: the production broker's `serve_forever` uses the default pipe. Use the smoke helper for isolated discovery, not an assumed server override.

Storage and pipes can be isolated; keyboard, mouse, foreground window, ownership mutex, and emergency stop are session-wide. Never run input drives concurrently or while the user is interacting. Use disposable application windows; the smoke drive can read discovery without taking input ownership.

## Doctor

For a manually launched broker:

```powershell
python -m jev_desktop.transports.cli --no-autostart --timeout 20 doctor
```

This performs a diagnostic handshake without launching an absent broker or injecting input (the handshake does create a transient client session). Inspect `broker.reachable`, `broker.health.pid`, `broker.health.driver`, `broker.health.journal.healthy`, `broker.health.lease`, and `broker.health.emergency_stop`. Confirm the PID belongs to your launched instance and Python imports this checkout. Doctor's exit code is zero even when unreachable. `pipe_name` reports the default name even when a client pipe override is supplied; it is not ownership proof. The helper additionally asserts the broker PID equals its own host PID. A key-presence flag does not prove credentials are valid.

## Drive

Run the helper above for discovery plus an empty query result through real CLI commands. It records commands, stdout, stderr, and exit codes. Use [discovery](features/discovery.md), [direct input](features/direct-input.md), [tasks](features/tasks.md), and [recovery](features/recovery.md) for targeted behavior.

For MCP, launch `python -m jev_desktop.transports.mcp_stdio` from an MCP client configured with this checkout's environment. Use `desktop_inspect`, `desktop_act`, `desktop_run`, and `desktop_stop` as mapped. CLI proof alone does not prove MCP serialization; include the MCP entry point when modifying that transport. `tests/unit/test_mcp_transport.py` provides complementary transport coverage with doubles.

## Evidence

The helper prints `.artifacts/verification/<UUID>/` and retains `doctor.json`, `transcript.json`, and `cleanup.json`. This is local ignored storage; discovery can contain private window titles and executable paths. Never commit these records or bearer tokens.

Exercise public CLI/MCP paths, capture the action and resulting observation, and verify side effects such as saved file contents and reopen behavior. A successful input acknowledgement or `completion: model_reported` is not proof. Preserve before/after observations and any required screenshot. Use `evidence --evidence-id ... --resume-token ... --out ...` to fetch broker images before shutdown. Mocks are supporting unit coverage, not live proof. Do not call internal setters or invent test-only production endpoints.

This smoke is discovery coverage only, not proof of input, screenshots, policy selection, or MCP. It writes a temporary broker journal and retained proof files; it does not invoke task mode or a dry-run flag. For other supposedly safe modes, check actual filesystem/network/browser effects rather than trusting the mode name.

## Cleanup

The helper stops and joins only its own pipe server, closes its broker/driver, removes its temporary home, and verifies retained transcript bytes after cleanup. Evidence survives both passing and failing runs. Never kill by process name. For live drives close only windows/processes created by the run; stop only the broker you launched. Preserve saved test files as evidence or copy them into the evidence directory before removing scratch state. Do not clear a user-set emergency stop automatically.

## Helpers

`python .cursor/skills/verify-jev-desktop/scripts/smoke.py` is the executable, self-cleaning discovery workflow; it needs no arguments. It fails if no interactive windows exist, since an empty desktop cannot prove positive discovery. It never launches a visible application.

For supporting code checks use the repository commands (`ruff format --check`, `ruff check`, `mypy`, and `pytest tests/unit -q`). Existing opt-in real Notepad tests are described in the task map. Maintain this skill with `/maintain-verification-skill` when commands or behavior change.

