# Windows fixture application contract (frozen)

Controlled local test application used by every live Windows test. It is a self-contained
ctypes Win32 program (stdlib only) that exposes *deliberately* broken behaviours so the
plugin's verification path can be proven to catch failure-masking, and so that no live
user application is ever touched by unattended tests.

## Invocation

```
python tests/fixtures/jev_fixture_app.py \
    --scenario <name> \
    --state-dir <absolute dir> \
    --build-id <id> \
    [--run-id <run id>] \
    [--position 120,120] [--size 720x520] \
    [--auto-close-after <seconds>]
```

* `--state-dir` is created if missing and is the ONLY directory the app may write to.
* `--run-id` is the run identity the app must echo into run-scoped artifacts. Absent the
  flag it reads `JEV_DESKTOP_RUN_ID`, which the plugin sets when it launches an approved
  configuration, then `JEV_FIXTURE_RUN_ID`, then the literal `run:0000000000000000`.
* `--auto-close-after` is a test-safety net; omit for interactive runs.

## Window and controls

* Main window class `JevFixtureWindow`, title `Jev Fixture - <scenario> - build <build-id>`.
  Created at `--position` with `--size`, `WS_OVERLAPPEDWINDOW`, top-level, not topmost.
* Modal dialog class `JevFixtureDialog`, title `Fixture Dialog`, owned by the main window.
* Control identities are the Win32 control IDs; UIA exposes the control *text* as the
  accessible name. Required controls (all direct children of the main window):

| Control ID | Win32 class | Text / content | Purpose |
| --- | --- | --- | --- |
| 100 | `STATIC` | `Name` | label |
| 101 | `EDIT` | text field | TYPE_TEXT target |
| 102 | `EDIT` (multiline + `WS_VSCROLL`) | text field | TYPE_TEXT target |
| 103 | `COMBOBOX` (`CBS_DROPDOWNLIST`) | `Draft`, `Review`, `Final` | SELECT target |
| 104 | `BUTTON` (`BS_AUTOCHECKBOX`) | `Enable feature` | TOGGLE target |
| 105 | `BUTTON` | `Save` | primary action under test |
| 106 | `BUTTON` | `Open dialog` | modal + dialog assertions |
| 107 | `BUTTON` | `Export` | artifact assertion |
| 108 | `LISTBOX` (`WS_VSCROLL`, 40 items `Item 01`..`Item 40`) | items | SCROLL target |
| 109 | `STATIC` | `Status: <status>` | observable status/error text |
| 110 | `STATIC` | `Build: <build-id>` | on-screen build identity |

Dialog controls: `201` `EDIT` (text field), `202` `BUTTON` `OK`, `203` `BUTTON` `Cancel`.

## Files written (all inside `--state-dir`)

* `ready.json`, written once after the main window is painted and idle:
  `{"ok": true, "pid": <int>, "hwnd": "<hex>", "build_id": "<id>", "scenario": "<name>", "started_at": <epoch>}`
* `build_marker.json`, the observed build identity the plugin binds to:
  `{"build_id": "<id>", "scenario": "<name>", "started_at": <epoch>, "pid": <int>}`
* `state.json`, persisted settings written by a successful Save:
  `{"name": "<name edit text>", "mode": "<combo selection>", "enabled": <bool>, "notes": "<notes text>", "run_id": "<run id>", "saved_at": <epoch>}`
* `export.json`, written by Export:
  `{"run_id": "<run id>", "name": "<name edit text>", "exported_at": <epoch>}`
* On startup the app loads `state.json` if present and restores name/mode/enabled/notes.

## stdout events

One JSON object per line, flushed, on ready and on every observable state change:

```
{"event":"ready","pid":..,"hwnd":"0x...","scenario":"..","build_id":".."}
{"event":"state","status":"ready|saving|saved|save-ignored|exported|export-failed|error","name":"..","mode":"..","enabled":true,"dialog":"none|open","saved_at":<epoch|null>}
```

`status` is also mirrored into control 109 as `Status: <status>`.

## Scenarios

| Scenario | Behaviour that must hold |
| --- | --- |
| `basic` | Everything works: Save persists `state.json`, Export writes the run-scoped `export.json`, dialog opens/closes, build marker matches `--build-id`. |
| `dead-save` | `Save` is enabled and mouse-clickable; clicking it sets status `save-ignored` and persists nothing. `Ctrl+S` performs a real save. A regression test that requires the button must not pass. |
| `semantic-only` | `Save` ignores mouse input entirely (no status change); UIA `Invoke` performs a real save. Proves semantic success is not user-path evidence. |
| `stale-artifact` | Export writes `export.json` with the fixed run id `run:000000000000deadbeef`, never the current run id. |
| `nonpersistent` | Save writes `state.json` but keeps the *previous* field values, never the current edits. |
| `wrong-build` | `build_marker.json` reports `"<build-id>-stale"`, i.e. the running build never matches the expected identity. |
| `false-done` | `Save` sets status `saved` and shows a confirmation dialog, but writes no `state.json`. A model-declared DONE must not become a pass. |
| `modal-block` | `Open dialog` opens the modal dialog; the main window's controls are disabled while it is open, so any click on `Save` must be refused rather than executed. |
| `slow-transition` | After a save click the status stays `saving` for ~3 s before `saved`; the save is real. |
| `crash-after-save` | Export writes `export.json` and then exits immediately (`ExitProcess`) so the effect is real but the process disappears mid-slice. |

Any scenario may be extended, but the behaviours above must hold exactly; tests depend on
them. Unknown scenario names must fail loudly at startup (exit code 2).

## launcher helper (`tests/fixtures/launcher.py`)

```
start_fixture(scenario, *, build_id="fixture-1", run_id=None, state_dir=None, position="120,120",
              size="720x520", auto_close_after=None) -> FixtureProcess
FixtureProcess: .pid .hwnd .state_dir .build_id .scenario
    .wait_ready(timeout=15.0) -> dict      # waits for ready.json and a live window
    .read_state() -> dict | None           # state.json
    .read_export() -> dict | None          # export.json
    .read_marker() -> dict | None          # build_marker.json
    .status(timeout=5.0) -> str | None     # last stdout event status
    .stop(timeout=5.0) -> None             # graceful WM_CLOSE then terminate
```

The launcher must never leave orphan processes: always terminate on failure, and reap on
interpreter exit via `atexit`.
