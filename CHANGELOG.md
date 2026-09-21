# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-09-21

First working release: a harness-independent host-desktop testing plugin.

### Added

* Broker. One on-demand broker per interactive user session, in `src/jev_desktop/broker.py`,
  serving `desktop_inspect`, `desktop_run`, `desktop_act`, and `desktop_stop` over a local
  named pipe with an explicit DACL.
* Bounded runtime. In `runtime.py`: immutable digest-frozen specifications, required steps,
  budgets for actions, model decisions, wall-clock, and slice length, bounded stale and
  no-progress retries, resume tokens rotated per slice, and assertion scheduling by checkpoint.
* Durable dispatch journal. In `journal.py`: SQLite in WAL with `synchronous=FULL`, single-use
  action identities, the four dispatch states, observation-based reconciliation on recovery,
  and no automatic replay.
* Ownership and authorization. In `ownership.py` and `security.py`: session authorization, an
  exclusive desktop lease with generations, cancellation, a human-takeover pause, and a local
  emergency stop that is a named kernel event independent of the broker.
* Jev policy. In `policy.py`: operation and per-operation target questions, strict Choice
  validation covering shape, distribution, argmax, and confidence floors, a pinned model id,
  bounded retries, and caller-supplied fixtures instead of generated field text.
* Independent verification. In `verification.py`: UI Automation property checks with dotted
  paths such as `state.checked`, presence and absence
  checks, window state, artifacts, process identity, caller-supplied verifier results, and
  explicitly selected visual oracles, with conservative verdict aggregation that no model
  completion choice can override.
* Launch handling. A `LAUNCH_APP` step runs an approved configuration with
  `JEV_DESKTOP_RUN_ID` in its environment, so the application can echo the run id into its
  own artifacts and run-scoped assertions can verify them.
* Evidence. In `evidence.py`: scoped screenshots at checkpoints and on failures, artifact
  copies, sha256 references, retention pruning that prefers failure evidence, and approved
  artifact roots with reparse points rejected.
* Windows driver. In `drivers/windows/`: UI Automation observation on a dedicated MTA thread
  with cached traversal, guarded `SendInput` mouse and keyboard dispatch, control-pattern
  operations, GDI capture with recorded geometry and scale, and process and build identity
  binding.
* Transports. A JSON CLI installed as the `jev-desktop` console script, and an MCP stdio
  server exposing the same four tools with screenshots delivered as image content.
* Skill. `skills/host-desktop-testing/SKILL.md` covers tool selection, specification authoring,
  result interpretation, and pause-reason handling for calling models.
* Test fixtures. A controlled Win32 application with deliberately broken behaviours: `dead-save`,
  `semantic-only`, `stale-artifact`, `nonpersistent`, `wrong-build`, `false-done`,
  `modal-block`, `slow-transition`, and `crash-after-save`, plus a launcher and a contract
  self-test.
* Safety gates. Live tests, the driver self-test, and the fixture contract test all refuse to
  run without `JEV_DESKTOP_LIVE=1`, so nothing creates a window by accident.
* Package identity for packaged applications. `ExpectedIdentity.mode = "package_family"`
  verifies a UWP app by package family name, with an optional launch timestamp, because the
  process that owns its window is a frame host that started earlier.
* Assertion deadlines are now enforced. An assertion re-evaluates against fresh observations
  until its `deadline_s`, so a browser that updates its window title after the page loads is
  not reported as a failure.
* Actionable elements are collected before content rows during observation, so a dialog's own
  buttons are never crowded out by a file list earlier in the tree.
* Policy tests drive the real HTTP transport. The hand-written response stub is gone; canned
  provider responses are now served by a real localhost HTTP server (`tests/unit/local_server.py`),
  so those tests exercise sockets, headers, status codes, retries, and connection failures.
* Real-application calibration, in `docs/calibration.md` and `scripts/calibrate_real_apps.py`.
  A Notepad save flow and a UWP Calculator run execute end to end with live model decisions,
  and every shipped threshold was measured. Fixes that came out of it: permitted operations
  are stated in the decision state, typed text requires a text-entry role rather than a Value
  pattern, observation collects interactive chrome before content rows, oversized states are
  trimmed to a provider budget with an adaptive retry, and `TYPE_TEXT` replaces prefilled
  content by default.
* Secret and hygiene guards. `tests/unit/test_no_secrets.py` fails the build on credential
  shapes, local user paths, or runtime state that would otherwise be committed, and
  `tests/unit/test_repo_hygiene.py` keeps documentation links and the desktop gate honest.

[Unreleased]: https://github.com/jacks3tr/jev-desktop/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/jacks3tr/jev-desktop/releases/tag/v0.1.0
