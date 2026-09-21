# Calibration

Every tunable value here was chosen from measurement. Most of it comes from driving **real
applications**, not the test fixture: a fixture with seven controls cannot tell you what a
shell dialog does to your element budget or what a browser does to your token count.

```bash
set JEV_DESKTOP_LIVE=1
set TYPESAFE_API_KEY=...            # key stays in the environment, never in the repo
python scripts/calibrate_real_apps.py --cases notepad,calculator,chromium --json real.json
python scripts/calibrate_thresholds.py --sections policy,input,settle,observe,capture,budgets
```

Measured on Windows 10 Pro 19045, one 1920x1080 display, against the applications already
installed on the machine and the live TypeSafe API with `jev-1.13.0`.

## Real applications, end to end

| Case | What it exercises | Result |
| --- | --- | --- |
| Notepad | Type into a document, open the File menu, choose Save As, replace the prefilled file name with a full path, click Save in the dialog, verify the file on disk and its run id | `completed` / `passed`, 5 actions, 5 decisions, 6.0 s |
| Calculator | A UWP application: click 7, +, 5, = among a grid of similarly named buttons, then read the display | `completed` / `passed` |
| Chromium | Focus the window, click a link in the page, verify the window title changed | `completed` / `passed`, 2 actions, 2 decisions |
| Chromium observation | A content-rich page: 150 links, 150 buttons, a text field, a list | measured below |

The Notepad run is the important one. It is a genuine desktop test, and getting it to pass
took five fixes that no fixture-based test would have surfaced.

## What real applications broke, and what changed

**1. The model hedged on operations that were not on offer.** Six of twenty-four decisions
were refused for low confidence, all of them the *correct* operation at 0.10 to 0.38
confidence. A checkbox advertised both `CLICK` and `TOGGLE` while the step permitted only
`TOGGLE`, so the probability split across operations the runner would never issue. Stating
the permitted operations in the state and the instructions moved refusals from 6/24 to 1/24
and operation confidence from a median of 0.66 to 0.87.

**2. Text-entry affordance was wrong, badly.** The driver treated "exposes a Value pattern"
as "accepts typed text". Shell navigation trees, file lists, and column headers all expose
Value, so a Notepad Save As dialog produced **151 tree items advertised as text fields**, and
the actual file name field was lost among them. Typed text now requires a text-entry role.

**3. Content rows ate the element budget.** A depth-first walk spent the entire 240-element
budget on file-list rows, so the dialog's own Save button never appeared in the observation at
all. Observation now collects interactive chrome first, allocates content rows at most a
quarter of the budget, and visits dialogs before background windows.

**4. Real states blew past the provider's input budget.** A Save As dialog produced an 85 kB
state and the provider answered `max_tokens_exceeded`, which the runtime reported as a runner
error. Now long strings are shortened, elements are trimmed to a byte budget with the drop
recorded in `state_trimmed`, a provider size refusal shrinks the budget and retries once, and
the run state remembers the smaller budget. Failures of this kind are a resumable pause with
an actionable reason, never a runner error.

**5. Typing appended instead of replacing.** Notepad pre-selects `*.txt` in the file name
field, so typed text produced `*.txtC:\...\note.txt`, an invalid name, and the dialog simply
stayed open. `TYPE_TEXT` now selects the existing content first (`replace_existing`, default
true), which is what a person does with a prefilled field.

## Policy confidence and timeout

Measured over live decisions with the permitted-operations hint in place, asking for a specific
control the way the runtime does.

| Measurement | Value |
| --- | --- |
| Model resolved | `jev-1.13.0`, matching the pinned id every time |
| Decision latency | median 0.20 s, worst 1.13 s over roughly eighty requests |
| Operation confidence, correct answers | 0.36 to 0.97, median 0.87 |
| Target confidence, correct answers | 0.94 to 1.0 |
| Correct operation, correct target | 23 of 23 validated answers, both |
| Cost | about 1.9k input tokens for a small window, 7.4k for a content-rich page |

| Threshold | Value | Why |
| --- | --- | --- |
| `operation_floor` | 0.35 | Uniform across four offered operations is 0.25. The floor sits above chance and below the lowest correct answer observed. |
| `target_floor` | 0.45 | Never binds on a healthy answer. It catches two near-identical controls, where the distribution splits. |
| `timeout_s` | 8.0 | Seven times the worst request observed. A timeout fails the decision, so it should not be tight. |
| `max_retries` | 2 | Unmeasured: no 429 or 529 appeared in roughly eighty requests. Kept as a bounded hedge. |

An earlier operation floor of 0.55 rejected correct answers, which is what the distribution
argument prevents: a right answer with a median confidence of 0.87 still arrived at 0.51 twice
in twenty-four tries.

## Observation and element caps

| Application and cap | Elements | State | Estimated tokens | Time |
| --- | --- | --- | --- | --- |
| Notepad, empty document | 23 | 7.7 kB | 1.9k | 62 ms |
| Notepad with a Save As dialog, cap 240 | 240 | 85 kB | 29k, refused by the provider | 400 ms |
| Notepad with a Save As dialog, cap 120, chrome-first | 66 | 24 kB | 6k | 200 ms |
| Chromium content page, cap 60 | 47 | 17 kB | 4.3k | 217 ms |
| Chromium content page, cap 120 | 77 | 29 kB | 7.4k | 181 ms |
| Chromium content page, cap 240 | 158 | 61 kB | 15k, refused by the provider | 197 ms |

`scope.max_elements` defaults to 120 for this reason. Chrome is collected before content rows,
so the smaller cap costs coverage of list rows rather than of anything a test can act on. A
page with 150 links and 150 buttons still offers 139 clickable candidates at that cap, which is
close to the 254 limit a single question can carry.

Browser content only enters the tree when the renderer exposes it. Chromium was launched with
`--force-renderer-accessibility` for these numbers; without it the same window shows browser
chrome only, around 36 elements, no matter what the page contains.

## Input

Press duration, measured by toggling a checkbox and reading the resulting state.

| Press | Registered |
| --- | --- |
| 0 ms, press and release in one input batch | 6 of 6 |
| 0 ms, split batches | 6 of 6 |
| 5, 10, 20, 30, 50 ms, split batches | 6 of 6 each |

Both batching styles work on a standard Win32 control, so `CLICK_PRESS_SECONDS` is 10 ms as a
hedge for applications that sample the physical button state, not because the click needs it.
This measurement disproved an earlier belief of ours: a zero-length press had been suspected of
losing clicks, and the real bug at the time was in how bounding rectangles were converted, which
sent clicks to the wrong coordinates.

## Settle window

Time from dispatch to an observable change, polled with full scoped observations.

| Action | Time to change |
| --- | --- |
| Typing into a field | 179 to 203 ms |
| Toggling a checkbox | 306 to 335 ms |
| Click that updates a status line | 308 ms |
| Click that opens a dialog | 365 ms |
| Misses, meaning no change within 3 s | 0 of 12 |

`settle_seconds` is 0.8, roughly twice the slowest observed change. It only bounds how long the
runner polls before deciding an action changed nothing, so a smaller value costs nothing when
the application is fast.

## Capture

| Measurement | Value |
| --- | --- |
| Screenshot, fixture window, full resolution | 8.9 kB, 31 ms |
| Screenshot at 0.6 scale | 8.9 kB, 34 ms |
| Screenshot at 0.4 scale, before the fix below | 23.9 kB, 41 ms |

Downscaling through GDI applies dithering, and dithering compresses worse: the "smaller" image
was 2.6 times larger than the original. The capture path now encodes both and keeps whichever is
smaller, reporting the scale that produced it so coordinate math stays correct.

## Budgets

Measured on live runs with real decisions.

| Measurement | Value |
| --- | --- |
| Model decisions per dispatched action | 1.0 on a clean run |
| Wall time per action | 1.09 s on the fixture, 1.2 s on Notepad |
| Decisions consumed | 5 for the 5-step Notepad path, `max_model_decisions` 12 |

| Limit | Default | Why |
| --- | --- | --- |
| `max_actions` | 25 | About 30 s of wall time, which keeps a run cheap enough to repeat. |
| `max_model_decisions` | 40 | 1.6 decisions per action across a full action budget, leaving room for the occasional `WAIT`. |
| `slice_seconds` | 45 | Covers a complete default-budget run in one slice, so a client deadline is rarely hit mid-run. |
| `deadline_seconds` | 600 | Ten times the expected duration of a default-budget run. |
| `stale_retries`, `no_progress_retries` | 2 | Unmeasured in normal operation: they bound pathological loops. |

## Packaged applications (UWP) need package identity

The first UWP case, Calculator, could not be verified at all. Its window belongs to
`ApplicationFrameHost.exe`, a process that started long before the launch, so `exe_hash`
describes the host rather than the app and `fresh_launch` reports a mismatch forever. Adding a
`package_family` identity mode fixed it: enumerate processes, match the package family name,
and optionally require an instance created after a timestamp. Calculator then verifies as
`Microsoft.WindowsCalculator_8wekyb3d8bbwe` and its run passes.

Two details came out of that work. A packaged application starts its process a moment after
its window appears, so identity verification waits up to three seconds before reporting a
mismatch. And UWP applications stay resident, so launching `calc.exe` can simply activate an
existing instance: requiring a fresh process makes the check fail for a perfectly good app.

## Driving a browser

The browser case failed at first because the requested toolbar button was absent from the
observation: the window was behind others, so its controls reported offscreen and observation
skips offscreen elements. Chasing that produced three changes, all measured:

* A `FOCUS_WINDOW` step before interacting with a background window. The model was right to
  escalate; the state was too thin to act on.
* Actionable elements are collected before content rows, so a dialog's buttons are never
  crowded out by a file list that sits earlier in the tree.
* Assertion deadlines. A browser updates its window title after the page loads, not when the
  click is dispatched, so an assertion evaluated once at the click reports a failure that is
  really a timing artifact. Assertions now re-evaluate until their `deadline_s`.

The case now clicks a link inside web content and verifies the window title changed to the
destination page.

## Known measurement gaps

UWP applications hand the window to another process, so binding by launched pid fails; the
calibration harness falls back to a new window matching the expected title, and
`ExpectedIdentity.fresh_launch` then verifies whatever process owns that window. Package
identity is not implemented, so UWP build verification is weaker than desktop verification.
Recorded here rather than papered over.

`stale_retries` and `no_progress_retries` have not been exercised by a genuine stale or
inert case outside the fixture. `max_retries` has not seen a rate limit.

## Re-calibrating

Re-run the harness after a model change, after an application that reacts more slowly than
half a second, or when a decision latency approaches the timeout. Compare the new numbers with
the tables above, change the value, and update this page in the same commit.
`tests/unit/test_policy.py::test_default_operation_floor_does_not_reject_measured_answers`
exists to catch a floor that drifts back above the measured band.
