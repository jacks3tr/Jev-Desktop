# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Jev Desktop lets AI agents drive Windows applications (UI Automation + native input) through
MCP or a JSON CLI. A local per-logon-session broker owns the desktop lease, a durable dispatch
journal, and the single native driver instance; MCP and CLI clients are thin, interchangeable
transports over a Windows named pipe. Windows-only; Python 3.12+.

## Commands

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"

python -m ruff format --check .      # formatting
python -m ruff format .              # apply formatting
python -m ruff check .               # lint
python -m mypy                       # type check (src/jev_desktop only, strict-ish)
python -m pytest tests/unit -q       # unit suite (default; excludes windows/live markers)
python -m pytest tests/unit/test_broker.py::test_name -q   # single test
```

- `jev-desktop doctor` — diagnostic handshake with the broker (autostarts one if absent).
- `python -m jev_desktop.transports.cli broker` — run the production broker in the foreground.
- `tests/windows/` needs an interactive Windows desktop session (`pytest -m windows`); real
  app tests live under `tests/windows/test_jev_real_apps.py`. `live` tests perform real input
  and are gated behind `JEV_DESKTOP_LIVE=1` — never run unattended (`addopts = "-m 'not live'"`
  in `pyproject.toml` excludes them by default).
- `python .cursor/skills/verify-jev-desktop/scripts/smoke.py` — self-cleaning discovery smoke
  check: hosts a real `Broker` + Windows driver with temp journal/evidence storage and a UUID
  pipe, drives it through real CLI subprocesses, and asserts a doctor handshake. No key needed,
  injects no input. See `.cursor/skills/verify-jev-desktop/SKILL.md` and its `features/*.md`
  for targeted coverage (discovery, direct input, tasks, recovery) when verifying a change by hand.
- `JEV_DESKTOP_RECORD=<dir>` makes the broker record Jev request/response bodies;
  `python scripts/export_decisions.py <labeled.json> <recordings...>` turns them into rows to
  label, and `python scripts/evaluate_thresholds.py <labeled.json>` sweeps `PolicyConfig`'s
  operation floor, target floor, and target margin offline (no requests, no input).
- CI (`.github/workflows/ci.yml`) runs on `windows-latest` for Python 3.12/3.13: ruff
  format/check + mypy (3.12 only) + `pytest tests/unit -q`, plus a separate Linux packaging job
  (`python -m build` + `twine check`).
- Releases: follow `.claude/skills/release-jev-desktop/SKILL.md` (version bump in four places,
  PR, tag, publish, update the local Claude Code plugin), and add what you had to look up to
  its Tips.
- `.artifacts/` is ignored scratch/local-record storage (verification transcripts, decision
  logs, private application content) — never commit anything from it.

## Architecture

### Process topology

```
MCP client / CLI  --(named pipe, versioned envelopes)-->  Broker  --(multiprocessing, private protocol)-->  driver worker (UI Automation, native input)
```

- **Transports** (`transports/cli.py`, `transports/mcp_stdio.py`) never touch the driver or
  engine directly. Both go through `client.py`'s `BrokerClient`, which speaks the pipe protocol
  (`ipc.py`) and will autostart a broker if `--no-autostart` isn't set. This keeps CLI and MCP
  behaviorally identical — the same authorization, journaling, and verification path.
- **`broker.py`** (`Broker`) is the single per-user-session server: one named pipe
  (`ipc.pipe_name`), one `Ownership` (desktop lease/session authorization), one
  `DispatchJournal`, one driver instance. It dispatches `hello/bye/inspect/run/act/stop/
  status/evidence/health/shutdown` envelopes. There is no transport-level response cache:
  double dispatch is prevented per action by `DispatchJournal.dispatch_once`, and a client that
  loses a response must inspect or query status rather than resend.
- **`drivers/windows/`** does the actual UI Automation/COM work in a **separate child process**
  (`worker.py`, spawned via `multiprocessing`) so native references never cross into the broker
  process; `win32.py`/`uia.py`/`input.py`/`capture.py`/`identity.py` are the driver internals
  (win32 primitives, UIA tree walking, native input dispatch, screenshot capture, build-identity
  checks). A watchdog thread kills input if the parent process disappears.
- **`security.py`** / **`ownership.py`** give the pipe and the emergency-stop kernel event
  explicit DACLs (current user + SYSTEM only, not Windows' permissive named-object defaults),
  and enforce a single active desktop lease with cancellation/checkpoint/takeover semantics
  independent of any in-flight model call.

### Contracts are the seam

`contracts.py` defines every type that crosses a module/process/transport boundary — enums
(`Operation`, `Reason`, `Execution`, `Verdict`, `DispatchState`...), `RunSpec`/`Limits`/
`ActionRequest`/`Receipt`/`RunResult`, and their `to_json`/`from_json`. Nothing else in the
package may widen these; a malformed payload should fail at this edge, not corrupt engine
state. When adding a field or operation, start here.

### Runtime: bounded, resumable, never-replay

`runtime.py` (`Runtime`) is the state machine executed inside the broker. Key invariants,
enforced deliberately (not incidentally):

- A `RunSpec` is frozen at creation (`spec.frozen_digest()`); resuming can only supply
  fixtures, visual results, or verifier results — never rewrite the goal, scope, or limits.
- Work happens in **slices**: each `slice()` call runs until a step/slice/run deadline, then
  returns a `resume_token` that rotates every slice. A dead client loses the lease but the
  paused run state survives (`Broker.on_disconnect`).
- Two loop shapes share the same dispatch/journal/verification machinery:
  - `_task_loop` — `desktop_run(task=...)`: a bounded goal-handoff loop where the policy
    (Jev) picks operations/targets from *closed* choices (only caller-supplied `texts`/
    `hotkeys`), used for routine agent handoffs.
  - `_loop` — `desktop_run(run=...)`: a predefined step/assertion spec (`purpose: regression`
    or `exploratory`) with build-identity verification and evaluators, for repeatable tests.
- `journal.py`'s `DispatchJournal.dispatch_once` records dispatch intent *before* input is
  sent and marks unfinished/interrupted dispatches `UNCERTAIN` on recovery — the runtime never
  auto-replays an action whose effect is unknown (`_reconcile_unfinished` raises
  `Pause(Reason.UNCERTAIN_EFFECT, ...)` instead of guessing).
- `policy.py` (`JevPolicy`) calls the TypeSafe API for operation/target decisions against
  separate confidence floors (`PolicyConfig.operation_floor`/`target_floor`); `NO_APPROPRIATE_TARGET`
  / `LOW_CONFIDENCE` / `NEEDS_VISUAL_ASSISTANCE` etc. (the `Reason` enum) are how control
  returns to the caller instead of the engine guessing.
- `verification.py` evaluates assertions (`uia_property`, `uia_presence/absence`,
  `window_state`, `artifact`, `process_identity`, `model_visual`, `caller_result`) against
  observations and aggregates a `Verdict`; only meaningful for the predefined-spec run mode.

### Direct actions vs. task handoff vs. spec runs

Three distinct ways a client can move the mouse/keyboard, all funneled through the same
journal/ownership checks — see `docs/reference.md` and `skills/desktop-use/SKILL.md` for the
full field-level contract:

1. `desktop_inspect` + `desktop_act` — one caller-directed action per inspection; each
   inspection authorizes at most one dispatch attempt.
2. `desktop_run(task=...)` — bounded goal handoff; Jev chains routine decisions inside the
   broker without a caller turn per click.
3. `desktop_run(run=...)` — a frozen, resumable test specification with assertions and build
   identity, for regression/exploratory verification.

## Repo layout notes

- `src/jev_desktop/` is the only importable package (`packages = ["src/jev_desktop"]` in
  `pyproject.toml`); `tests/`, `scripts/`, `skills/`, `docs/`, `examples/` ship in the sdist too
  (see `[tool.hatch.build.targets.sdist]`).
- Keep `skills/desktop-use/SKILL.md` current in the same change as any behavior agents rely on:
  tool arguments, returned fields or reasons, pause and recovery behavior, limits, focus and
  capture rules. It is the only guide agents using Jev read; describe what they must do
  differently, briefly, in its existing sections, and drop guidance the change made wrong.
- `skills/desktop-use/SKILL.md` is the agent-facing operating skill bundled into the
  `.codex-plugin/` and `.claude-plugin/` plugin manifests alongside `.mcp.json` — this repo is
  itself distributed as a Codex/Claude Code plugin (see README's "Install the plugin" section).
- `examples/*.json` and `docs/reference.md` document the full predefined-run spec schema
  (steps, assertions, evaluators, comparators, limits, resume inputs) — read `docs/reference.md`
  before changing anything under the spec/assertion path.
- `.env.example` is reference-only; the broker/CLI **never** auto-load dotenv files —
  `TYPESAFE_API_KEY` (and any test `secret_refs`) must be set in the actual environment.
