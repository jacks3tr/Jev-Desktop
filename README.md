# jev-desktop

[![CI](https://github.com/jacks3tr/jev-desktop/actions/workflows/ci.yml/badge.svg)](https://github.com/jacks3tr/jev-desktop/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](pyproject.toml)
[![Platform: Windows](https://img.shields.io/badge/platform-Windows-0078d4.svg)](#requirements)

Test software by driving the real Windows desktop. Your coding model defines the test. Jev
policy picks the next routine interaction. A native driver performs it once. Independent
assertions decide the verdict from evidence.

You get three things out of a run: what happened step by step, what was actually asserted,
and screenshots plus artifacts to look at. You do not get a green check because a model said
the word done.

## Why this exists

Coding agents can write a test and run it, but they cannot click a button. Unit tests,
HTTP calls, and headless browsers cover a lot, and they cover nothing that only exists in a
real window. The remaining gap is small, annoying, and exactly where regressions hide: a save
button that stopped persisting, an export that writes a stale file, a setting that quietly
does nothing after a restart.

A coding model is bad at this job and good at describing it. So the model writes down what to
test, and this plugin does the clicking, with guards, a journal, and assertions that do not
care what the model believes.

## What it is not

No cloud service and no remote network listener. No bot VM and no hosted computer. No
browser automation endpoint. It runs on the machine it is testing, in your session, and
that is the point.

## Quick start

```bash
python -m pip install -e .          # Python 3.12+ on Windows
setx TYPESAFE_API_KEY "..."         # needed for policy decisions

jev-desktop doctor                   # environment, broker, driver, evidence
jev-desktop inspect --pretty         # list observable applications
jev-desktop run --spec my-spec.json  # start a bounded test
```

MCP clients get the same engine:

```json
{
  "mcpServers": {
    "jev-desktop": {
      "command": "python",
      "args": ["-m", "jev_desktop.transports.mcp_stdio"],
      "env": { "TYPESAFE_API_KEY": "..." }
    }
  }
}
```

Four tools are exposed over both transports:

| Tool | Use it for |
| --- | --- |
| `desktop_inspect` | Find applications, then observe one and get opaque references plus coverage limits |
| `desktop_run` | Start or resume a bounded test from an immutable specification |
| `desktop_act` | One caller-directed interaction through the same guards, journal, and receipts |
| `desktop_stop` | Cancel your run, release control, or work the local emergency stop |

## How a run reads back

Execution and verdict are separate on purpose:

```
execution: completed | paused | blocked | cancelled | error
verdict:   passed | failed | inconclusive
```

`completed` means the bounded sequence finished. It does not mean the software works. A pass
needs a verified build, at least one required assertion, every required assertion passed, the
required path completed, and no uncertain effect. No assertions means no pass, because nobody
said what success looks like.

A pause carries a reason and a resume token: `needs_text` when a fixture is missing,
`needs_visual_assistance` with a scoped screenshot when the accessibility tree cannot resolve
the step, `permission_boundary` when a modal dialog or privilege boundary stops input,
`uncertain_effect` when input may have landed without a receipt. The full list is in
[docs/test-specification.md](docs/test-specification.md).

## Safety

This tool controls a live desktop, so the boring parts matter more than the clever parts.

One broker per interactive user session, reachable only over a named pipe with an explicit
DACL. An exclusive desktop lease, so two harnesses cannot interleave input. A local emergency
stop that is a named kernel event, checked before every dispatch and independent of the
model, the queue, and screenshot capture. Input that is single-use, journaled before it
happens, and never replayed when the outcome is unknown. Observation that is scoped,
redacted, and authorized like input.

No exactly-once GUI effects are claimed. What the journal gives you is single-use dispatch
identities plus conservative recovery, and a run that refuses to pass when an effect is
unknown. Details, including the risks we accept, are in
[docs/security-model.md](docs/security-model.md).

## Documentation

| Document | Read it for |
| --- | --- |
| [docs/test-specification.md](docs/test-specification.md) | Every field, evaluator, comparator, limit, and pause reason |
| [docs/architecture.md](docs/architecture.md) | Who decides what, the runtime loop, and the journal states |
| [docs/security-model.md](docs/security-model.md) | Trust boundaries, redaction, and accepted risks |
| [docs/troubleshooting.md](docs/troubleshooting.md) | Doctor output, stuck states, and what each reason means |
| [skills/host-desktop-testing/SKILL.md](skills/host-desktop-testing/SKILL.md) | Instructions for the calling model |
| [examples/README.md](examples/README.md) | Runnable specs against the fixture application |

## Repository layout

```
src/jev_desktop/            the plugin
  contracts.py              every type that crosses a boundary
  ownership.py journal.py   lease, cancellation, emergency stop, dispatch identities
  policy.py                 Jev questions and strict answer validation
  runtime.py                bounded loop, budgets, checkpoints, resume
  verification.py evidence.py   assertions, verdicts, screenshots, artifacts
  broker.py ipc.py client.py    session broker and its local pipe
  transports/               MCP stdio and the JSON CLI
  drivers/windows/          UIA observation, guarded input, capture, identity
skills/host-desktop-testing/  the skill that tells a model how to use this
tests/unit/                  offline suite, in-memory driver, no desktop
tests/windows/               live suite, gated behind JEV_DESKTOP_LIVE=1
tests/fixtures/              controlled Win32 app with deliberately broken behaviours
scripts/                     self-tests for the driver and the fixture contract
docs/ examples/              reference material
```

## Requirements

Windows 10 or 11, Python 3.12 or newer, an interactive desktop session, and a TypeSafe API
key for runs that need decisions. Inspection, directed actions, and stop work without a key.

## Testing

| Command | Touches your desktop |
| --- | --- |
| `python -m pytest tests/unit -q` | No. In-memory driver, and the fixture refuses to start. |
| `JEV_DESKTOP_LIVE=1 python -m pytest tests/windows -m live -q` | Yes. Creates windows and injects real input. |
| `JEV_DESKTOP_LIVE=1 python scripts/native_selftest.py` | Yes. Its own window, real mouse, under a second. |
| `JEV_DESKTOP_LIVE=1 python scripts/fixture_selftest.py` | Yes. Starts windowed fixtures, hidden, driven by window messages. |

The gate is enforced in three places rather than by discipline: the live test directory skips
collection without the opt-in, the launcher refuses to start the fixture, and the fixture
exits with code 3 before creating a window. `tests/unit/test_repo_hygiene.py` watches that
the first of those keeps holding, so "live tests never run by accident" is itself tested.

## Status

Verified on this workstation, Windows 10 Pro, session 1, 1920x1080:

| Area | Evidence |
| --- | --- |
| Offline engine | `pytest tests/unit` -> 74 passed: policy validation, journal idempotency and recovery, verdict aggregation, artifact path safety, runtime budgets and resume, launch handling with the run id passed to the child, nested state assertions, repository hygiene, secret scanning, shutdown safety, broker authorization, CLI and MCP transports |
| Live integration | `pytest tests/windows -m live` -> 12 passed, three consecutive runs, covering dead button versus working shortcut, semantic invocation that is not user-path evidence, persistence across a restart, modal blocking, stale export, false success, wrong build, and a full run ending `completed`/`passed` with checkpoint evidence |
| Driver | `scripts/native_selftest.py` -> observes its own window, captures PNG evidence, clicks, types, toggles semantically, refuses a stale reference, binds identity |
| Fixture contract | `scripts/fixture_selftest.py` -> 11 scenarios including save, restart, and save again |

Known gaps, stated rather than implied: the driver is Windows-only in this release;
multi-monitor, mixed-DPI, elevated targets, and the secure desktop are untested; vision
oracles are caller-supplied by default; thresholds were not calibrated against a live TypeSafe
key here, so the HTTP call is the untested seam even though the request and answer validation
are covered by tests.

## Contributing

[CONTRIBUTING.md](CONTRIBUTING.md) covers setup, the two test tiers, the quality gates, and
the desktop rules. Community expectations are in [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md), and
security reports go through [SECURITY.md](SECURITY.md).

## License

MIT, see [LICENSE](LICENSE). Mechanisms adapted from `browser-use/jev-ultrafast` are recorded
in [NOTICE](NOTICE).
