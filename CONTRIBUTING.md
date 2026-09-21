# Contributing

Thanks for taking the time to improve `jev-desktop`. This project drives a **real desktop**,
so the contribution rules below are stricter than usual about what runs where.

## Ground rules

1. **Never run a window-creating command on a machine someone is using.** The live suites
   move the real mouse and keyboard. They are gated behind `JEV_DESKTOP_LIVE=1`; do not
   bypass the gate, and do not "just quickly" run the fixture application on a shared box.
2. **Do not weaken a guard to make a test pass.** Guards exist because the alternative is
   unattended input on a live host. If a guard is wrong, fix the guard and say why.
3. **Evidence beats assertion.** PRs that change behaviour must include the command you ran
   and its output.
4. **Small, focused changes.** One subject per PR; unrelated refactors belong in their own.

## Development setup

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
python -m pip install -e ".[dev]"
```

Required: Python 3.12 or newer, on Windows. The engine (everything except the native driver)
is portable, but the driver and its live tests are Windows-only in this release.

## The two test tiers

| Tier | Command | Desktop impact |
| --- | --- | --- |
| Offline (default) | `python -m pytest tests/unit -q` | none: in-memory driver doubles, gated fixture |
| Live (opt-in) | `JEV_DESKTOP_LIVE=1 python -m pytest tests/windows -m live -q` | creates windows, injects input |

Live entry points, all gated:

```bash
JEV_DESKTOP_LIVE=1 python scripts/native_selftest.py     # driver self-test on its own window
JEV_DESKTOP_LIVE=1 python scripts/fixture_selftest.py    # fixture contract, hidden windows
```

The gate is enforced in three places, not by convention:

* `tests/windows/conftest.py` skips the directory and marks items without the opt-in,
* `tests/fixtures/launcher.py::start_fixture` refuses to start the fixture,
* `tests/fixtures/jev_fixture_app.py` exits with code 3 before creating any window.

`tests/unit/test_repo_hygiene.py` asserts that the first of those keeps holding, so the
"live tests never run by accident" property is itself tested.

## Quality gates

```bash
python -m ruff format --check .
python -m ruff check .
python -m mypy
python -m pytest tests/unit -q
```

CI runs exactly these on Windows for Python 3.12 and 3.13, plus a packaging job that builds
the sdist and wheel.

## What good looks like here

* Contracts first. Cross-module and cross-process types live in
  `src/jev_desktop/contracts.py`. Add a field there with validation and serialisation rather
  than passing loose dictionaries around.
* No hidden fallbacks. The driver uses explicit `ctypes` and `comtypes` calls. Do not route
  input through a helper that silently retries with a different mechanism or a different
  privilege level.
* Honest failure modes. If an operation cannot be completed safely, raise
  `Pause(reason)` or `UncertainEffect`. Never swallow it and never push past a guard.
* Tests that can fail. A test earns its place if a plausible bug makes it fail. Assert
  observable behaviour and boundaries, not implementation details.
* Docs move with code. User-visible behaviour changes need a matching change in
  `README.md` or `docs/`.

## Adding a driver operation

1. Add the operation to `Operation` in `contracts.py`.
2. Map it in `src/jev_desktop/drivers/windows/uia.py::_operations_for` so observation
   advertises it only where it is genuinely supported.
3. Implement the dispatch in `src/jev_desktop/drivers/windows/input.py` with the existing
   pre-dispatch guards, and decide explicitly whether it is mouse-routed (hit-test proven) or
   keyboard-routed (requires the foreground window).
4. Add a live test against a fixture scenario, and an offline test for the decision logic.
5. Document it in `docs/test-specification.md` and `skills/host-desktop-testing/SKILL.md`.

## Adding an assertion evaluator

1. Add the evaluator to `Evaluator` in `contracts.py` and implement it in
   `src/jev_desktop/verification.py`, returning an `AssertionResult` with an explicit origin
   (`application`, `runner`, or `environment`).
2. Decide what evidence it must attach and whether absence-style logic needs complete
   coverage.
3. Add offline tests for pass, fail, and inconclusive paths. The inconclusive case matters
   most, because that is where a runner quietly pretends to know something.

## Reporting bugs and requesting features

Use the issue templates. For anything that looks security-relevant (input escaping its
scope, journal replay, path escape, guard bypass), use the private process in `SECURITY.md`
instead of a public issue.

## Commit and PR style

* Conventional-style subjects are welcome but not required (`fix:`, `feat:`, `docs:`).
Explain the failure mode you are fixing, and say what breaks without the fix.
* Keep the PR template checklist honest; strike items that genuinely do not apply.

## License

Contributions are accepted under the MIT license in `LICENSE`. Adapted third-party material
must be recorded in `NOTICE`.
