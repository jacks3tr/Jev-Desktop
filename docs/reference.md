# Technical reference

## Data and storage

Jev decisions send scoped application observations to TypeSafe. Declared secret values are
redacted and password fields withheld, but other sensitive application content can remain
visible. Select only the application and windows you intend to expose. Shared application
hosts require explicit window references. Same-user processes are outside the security boundary.

Screenshots require a foreground scoped window without overlapping windows. Coordinate actions
check screenshot freshness and geometry before input. Screenshots do not classify arbitrary
sensitive content. Use `inspect --no-screenshot` or MCP `screenshot: false` to skip capture.

Evidence defaults to 256 MiB per run and 2 GiB total. Ordinary evidence expires after seven
days; failure and visual-assistance evidence stays until size limits require removal. Cleanup
runs after test slices and standalone screenshot inspections. These limits do not cover the
SQLite journal. Keep all local records out of the public repository.

## Goal handoff

`desktop_run(task=...)` keeps the observation and action loop inside the broker. Provide a
`goal`, `app_ref`, explicit `window_refs`, optional named `texts` and `secret_texts`, and
allowed `hotkeys`. `texts` values are sent to the TypeSafe API with the observation.
`secret_texts` values are typed like `texts`, but Jev sees only their names and lengths, and
they are redacted from observations sent to the model. Together they hold at most 16 uniquely
named strings. Both are kept in the local journal with the run. The CLI accepts the same object with `run --task file.json`. See the
[Calculator](../examples/calculator.json) and [Chrome](../examples/browser.json) tasks.

Defaults are 20 actions, 40 decisions, and 60 seconds; `timeout_seconds` may not exceed 120.
Set `max_model_decisions` for longer tasks. `max_elements` and `max_depth` control observation
coverage and default to 180 and 12. The loop batches operation and target questions together.
With multiple supplied values, a second question selects the value for the chosen input control.
Each control lists the offered operations it supports. When Jev finds no target for its chosen
operation, the loop withdraws that operation and decides again on the same observation.
Jev receives structured accessibility data and action history, not screenshots. Native dispatch
rechecks the target and records input before the next observation. Supplied text and chords
are closed choices. Jev cannot invent values or launch applications. Low confidence, missing
input, cancellation, and limits return control to the caller.

Results include final observations, actions, and cumulative metrics for the task. Token
counts include provider-reported usage from accepted and rejected responses.
`usage_complete` is false when any HTTP attempt lacks either token field.
`model_latency_ms` includes rejected calls and retries; `elapsed_seconds` includes observation, input,
and waits. No dollar estimate is inferred. Completion is model-reported and must be checked
against the returned observation. A fresh observation is taken before accepting DONE, and a
separate question asks whether the goal's end state is visible in it without the action history.
A confident no pauses with `needs_visual_assistance`. An unsure answer gets one more look: the
broker waits briefly, observes again, and asks about completion once more; if that is still
unsure, the pause carries the check's own answer (`selected` YES or NO) under `low_confidence`.
After a low-confidence decision following input, the broker waits briefly, observes again, and
decides once more. Actions stay available unless the doubted answer was DONE or WAIT; then the
recheck asks only about completion, cannot send input, and a `needs_visual_assistance` pause
carries the doubted answer under `low_confidence`. A second low-confidence answer before the
next action pauses with the first one's detail. If the result remains unclear, inspect the final
window or request a screenshot.

Paused tasks resume with `run_id` and `resume_token` within their original total budgets.
A broker restart requires a new task and fresh references. Use direct actions for recovery;
a new task is needed if its goal or supplied inputs change. This path preserves the desktop
lease, native guards, cancellation, and no-replay journal without requiring a test definition.

## Direct desktop use

Call `desktop_inspect` to discover an application, then inspect its `app_ref` and explicit
`window_refs`. Without `app_ref`, `query` filters applications by executable path or window
title. With `app_ref`, `query` returns up to `max_elements` elements whose name, value, text, or
path contains it. The search walks up to 10,000 nodes or 8 seconds and keeps up to 500 matches;
the truncation notes `traversal node budget reached` and `query search time limit reached` mean
it was incomplete, so an empty result does not prove absence. A query result holds only the
matches, so inspect without `query` to act on controls around them. Returned observations omit rows
with no name, value, text, operation, or focus, and context texts already shown by an element;
Jev still receives them. Call `desktop_act` with the returned `snapshot_id`, `access_token`, `window_ref`,
operation, and control reference or screenshot point. Alternatively, use `target_description`
for Jev to choose a compatible control. That option requires a TypeSafe key. Supply `text`, `option_label`, `hotkey`,
or `scroll` as appropriate. No run, specification, assertions, or build hash is required.

Each inspection authorizes at most one dispatch attempt. Inspect again after an action. An
action refused before any input was sent (for example `stale_observation` or a refused
`FOCUS_WINDOW`) leaves the inspection usable, unless physical input arrived meanwhile. A used
inspection returns `invalid_request`: "the inspection is unknown or already used".
Input uses the native scope, freshness, desktop ownership, cancellation, and journal checks.
A receipt acknowledges input; inspect the application to establish the result. Do not replay
uncertain input. Screenshots are optional.

## Optional workflows and tests

Use `desktop_run` for a predefined sequence with Jev-selected controls. The fields below
apply only to predefined test workflows, not goal handoffs or ordinary direct actions.

A JSON document defines each run. The broker freezes it and records its digest. Resuming
cannot change the test's acceptance criteria.

```json
{
  "goal": "regression: Save persists the document",
  "purpose": "regression",
  "interaction_mode": "user_path",
  "app_ref": "app:...",
  "expected_identity": {"mode": "file_marker", "marker_path": "C:/app/build.json", "expect_marker": "build-4711"},
  "launch_config_id": null,
  "steps": [
    {"step_id": "type-name", "operation": "TYPE_TEXT", "target_description": "the Name field",
     "fixture_reference": "name_value"},
    {"step_id": "save", "operation": "CLICK", "target_description": "the Save button", "checkpoint": true}
  ],
  "assertions": [
    {"assertion_id": "status-text", "evaluator": "uia_property",
     "target": {"role": "text", "name_regex": "^Status:"}, "property": "text",
     "expected": {"contains": "saved"}, "checkpoint": "save"},
    {"assertion_id": "state-file", "evaluator": "artifact",
     "target": {"path": "C:/app/state.json", "run_id_field": "run_id"},
     "expected": {}, "property": "run_scoped", "checkpoint": "save"}
  ],
  "fixtures": {"name_value": "Ada Lovelace"},
  "secret_refs": {"api_token": "APP_TEST_TOKEN"},
  "limits": {"max_actions": 25, "max_model_decisions": 30, "deadline_seconds": 600,
              "slice_seconds": 45, "stale_retries": 2, "no_progress_retries": 2},
  "scope": {"app_ref": "app:...", "max_elements": 240}
}
```

## Top level

| Field | Meaning |
| --- | --- |
| `goal` | One sentence the policy reads. Say what the test proves, not how to click. |
| `purpose` | `regression` binds required steps and their mechanisms. `exploratory` allows alternative routes and records them. |
| `interaction_mode` | `user_path` drives mouse and keyboard, except that `SELECT` chooses an option the pointer cannot be proven to reach, such as a Chromium native select's, through its selection pattern. `semantic` uses control patterns. The runtime preserves the selected mode. |
| `app_ref` | Opaque application reference from `desktop_inspect`. |
| `expected_identity` | How the running build is bound to the artifact under test. See below. |
| `launch_config_id` | Name of an approved launch configuration. Only used by a `LAUNCH_APP` step. |
| `steps` | Required interactions, in order. |
| `assertions` | What makes the test pass. No assertions means no pass. |
| `fixtures` | Named values the test may type. A name with a `null` value asks the caller to supply it on resume. |
| `secret_refs` | Name to environment variable. The value is resolved inside the broker and never journaled in plaintext. |
| `limits` | Budgets. Anything above local policy is rejected at creation. |
| `scope` | Observation bounds: window filter, element and depth caps, text limit. |
| `allow_restart` | Whether the run may restart the application to verify persistence. |

## Build identity

Passing requires observable evidence that the intended build is running.

| Mode | Check |
| --- | --- |
| `file_marker` | Read `marker_path` and compare with `expect_marker`. The marker must be JSON with a build field (`build_id`, `build`, `version`, or `id`), `pid`, and `started_at` bound to the observed process. |
| `exe_hash` | Compare `sha256` of the running image with `expect_sha256`, and optionally its path with `expect_exe`. |
| `fresh_launch` | The process creation time must be at or after `launched_after`, and its image must match `expect_exe`. |
| `package_family` | The bound process must belong to `expect_package`, and its executable hash must match `expect_sha256`. |
| `any` | No expectation. The report says unverifiable and the verdict stays inconclusive. |

An unreadable marker is not a mismatch. It reports unverifiable, which blocks a pass.

## Steps

| Field | Meaning |
| --- | --- |
| `step_id` | Unique inside the run. Also the name you use as an assertion checkpoint. |
| `operation` | One of `CLICK`, `TYPE_TEXT`, `SELECT`, `TOGGLE`, `SCROLL`, `FOCUS_WINDOW`, `LAUNCH_APP`, `HOTKEY`. |
| `target_description` | What the policy should pick. Describe the control, not its coordinates. |
| `fixture_reference` | Fixture name. Required for `TYPE_TEXT` and `SELECT`, and for `HOTKEY` when you drive the chord from a fixture. |
| `depends_on` | Step ids that must complete first. An incomplete dependency is a spec error, not a pause. |
| `checkpoint` | Capture a screenshot after this step completes. |
| `required` | Defaults to true. Required steps count toward the verdict. The runtime still executes all listed steps in order. |
| `replace_existing` | Defaults to true. Text input replaces the current field value. Set false to append. |

`TYPE_TEXT` types the supplied fixture value. `SELECT` takes the option label from a fixture
and binds it to the control the policy chose, then verifies the control reports that value.
`HOTKEY` accepts a chord such as `ctrl+s`, supplied through a fixture reference.

## Assertions

| Field | Meaning |
| --- | --- |
| `assertion_id` | Unique. Also the key used to supply a caller result on resume. |
| `evaluator` | See the evaluator table. |
| `target` | What to look at. Shape depends on the evaluator. |
| `property` | Which value to read. Dotted paths work: `value`, `name`, `text`, `enabled`, `state.checked`, `rect.width`, or `count`. |
| `expected` | A comparator for value checks. Presence, absence, and run-scoped checks do not require one. |
| `checkpoint` | A step id, `any`, `run_start`, or `run_end`. `any` is evaluated after every action and keeps its worst result. |
| `required` | Defaults to true. Only required assertions take part in the verdict. |
| `oracle` | Required for `model_visual`: `caller` or `provider`. |
| `deadline_s` | Time allowed for the assertion to pass against fresh observations. |

Comparators: `equals`, `not_equals`, `contains`, `regex`, `in`, `is_true`, `is_false`,
`gte`, `lte`, `prefix`, `suffix`. Passing two comparators is a specification error, and the
assertion reports inconclusive with a runner origin rather than a failure.

### Evaluators

| Evaluator | Target | Notes |
| --- | --- | --- |
| `uia_property` | `role`, `name`, `name_regex`, `value_regex`, `window_ref`, `index` | Reads `property` from the first match. No match fails only with complete coverage; otherwise it is inconclusive. |
| `uia_presence` | Same filters | An empty result with partial coverage becomes inconclusive, not a failure. |
| `uia_absence` | Same filters | Requires complete coverage with no truncation. A truncated tree can never prove absence. |
| `window_state` | `window_ref` or `title_regex` | Properties: `exists`, `visible`, `enabled`, `modal`, `focused`. |
| `artifact` | `path`, plus `json_field`, `run_id_field`, `content_regex` | Properties: `exists`, `sha256`, `size_at_least`, `mtime_after`, `json_path`, `run_scoped`, `content_regex`. |
| `process_identity` | empty | Reads the identity report. An unverifiable build gives inconclusive with an environment origin. |
| `model_visual` | empty | Needs `oracle`. Without a caller result the run pauses for visual assistance. |
| `caller_result` | empty | A verifier result you supply on resume, bound to the assertion and its evidence. |

`run_scoped` compares the JSON field named by `run_id_field` against this run's id. It is the
way to detect an export left over from an earlier run. Artifact paths must sit inside an
approved root, and links or reparse points along the way are refused.

### Evidence labels

Deterministic evaluators produce `deterministic` results. A visual judgement you selected
produces `model_assessed`, and a verifier result you supplied produces `caller_supplied`.
Each result includes its label to identify how the assertion was evaluated.

## Limits

| Limit | Default | Local ceiling |
| --- | --- | --- |
| `max_actions` | 25 | 200 |
| `max_model_decisions` | 40 | 400 |
| `deadline_seconds` | 600 | 3600 |
| `slice_seconds` | 45 | 120 |
| `stale_retries` | 2 | 5 |
| `no_progress_retries` | 2 | 5 |

The broker rejects specification limits above local ceilings when it creates the run.
Scope `max_elements`, `max_depth`, and `text_limit` may not exceed 500, 32, and 8000.

## Resume inputs

Resuming takes the `run_id` and the current `resume_token`, and it accepts exactly three
kinds of input:

```json
{"fixtures": {"name_value": "Ada Lovelace"},
 "visual_results": {"looks-right": {"status": "passed", "observed": {"note": "banner visible"},
                                    "evidence_refs": ["ev:..."]}},
 "verifier_results": {"state-file": {"status": "failed", "observed": {"reason": "stale run id"}}}}
```

A fixture name that is not declared in the specification, or a value that is not a string, is
rejected. A visual result for an assertion that did not select a visual oracle is rejected.
Acceptance criteria, limits, and failed steps cannot be touched.

## Execution, verdict, reason

```
execution: completed | paused | blocked | cancelled | error
verdict:   passed | failed | inconclusive
```

`completed` means the bounded sequence finished. It does not mean the test passed.

| Reason | Meaning and what to do |
| --- | --- |
| `needs_text` | A declared fixture has no value. Resume with it. |
| `needs_visual_assistance` | Structured observation cannot resolve the step. Use the attached screenshot, then resume or act on that snapshot. |
| `low_confidence` | The policy answer was below the local floor or margin. Detail names the doubted `operation`, `selected`, `confidence`, `margin`, and `floor` or `required`. Narrow the observation or re-observe. |
| `invalid_model_response` | The answer failed validation. Do not retry blindly; look at the request. |
| `unexpected_model_version` | The pinned model id changed. Recalibrate before running again. |
| `stale_observation` | The observation no longer matches live state. Re-observe. |
| `unsupported_control` | The driver cannot perform the required operation on this control. |
| `permission_boundary` | A modal dialog, a disabled window, or a privilege boundary stopped input. |
| `user_takeover` | A person or another process owns the desktop, or Windows kept another application (`foreground_process`) in the foreground. Stop and ask; the run stays resumable. |
| `uncertain_effect` | Input may have landed without a receipt. A direct action returns it as its error code. Remains paused; never assume and never replay. |
| `incorrect_build` | The running build is not the expected one. Fix the build first. |
| `budget_exhausted` | Check `budget`. A new slice can extend `slice_deadline`, but cannot reset the frozen run's total budgets, such as `run_deadline`. |
| `step_unresolved` | No progress after bounded retries, or the model asked to finish with required steps left. |

## Specifications that get rejected

Duplicate step or assertion ids. An assertion checkpoint that names no step. A `depends_on`
that names no earlier step. A `TYPE_TEXT` or `SELECT` step without a `fixture_reference`. A
non-numeric `gte`, `lte`, `bytes`, or `after`, an `in` that is not a list, or an invalid regular
expression. A visual assertion without an oracle. Limits above the ceiling. More than
254 candidate targets for one operation, which the policy reports as
`needs_narrower_observation` instead of silently dropping options.

## Directed actions within a predefined run

Create a run with `desktop_run(start_only=true)` to use directed actions without a Jev key.
The CLI equivalent is `run --spec test.json --start-only`. Inspect it using `run_id` and `resume_token` so
screenshots belong to that run. Every action supplies the current `snapshot_id`, `step_id`,
`window_ref`, operation, mode, and resume token. It must match the next immutable step.
The broker issues the action ID; callers use transport request IDs for retries. If an action
sends input and then pauses, the pause detail carries the new `resume_token`.

For screenshot-based `CLICK`, `TOGGLE`, or `SCROLL`, omit `element_id` and supply `point`:

```json
{
  "evidence_id": "ev:0123456789abcdef01234567",
  "x": 120, "y": 80,
  "source_rect": {"left": -1600, "top": 100, "right": -600, "bottom": 800},
  "scale": 0.5, "image_width": 500, "image_height": 350,
  "geometry_epoch": 3
}
```

Copy the transform fields from the screenshot evidence exactly. Coordinates are zero-based
image pixels. The driver maps them using the actual image dimensions, including rounding
and negative desktop origins. Changed pixels near the point, DPI, window position, geometry,
scope, or snapshot invalidate the action. Coordinate actions use `user_path`; they do not substitute
semantic invocation. Text and selection still require observed controls and declared fixtures.

Fresh-launch and executable-hash checks cannot certify an interpreter or shared host as the application.
Use a process-bound runtime build marker for interpreter-hosted apps. Package-family checks
require the bound process to belong to the family and `expect_sha256` to identify its build.

CLI status, stop, inspection of a run, and evidence fetch accept `--resume-token` when used
from a new connection. For inspection-only evidence, pass its returned `access_token` as that
argument. Secret fixture values stay in memory; screenshots are withheld when they are present.

## Runtime

MCP and CLI clients connect to a local broker through a named pipe. The broker owns sessions,
run state, evidence, and the desktop lease. A separate worker performs UI Automation and
native input. The runtime records dispatch intent before input and never automatically
replays an action whose outcome is uncertain. Resume tokens rotate after execution. Window
references do not survive a broker restart; a run scoped to them pauses with `stale_observation`
and needs a new run.

### Sharing the desktop

While a run or direct action holds the desktop lease, a subtle blue glow marks the edges of
every monitor. It never takes focus and passes clicks through. It stays visible to remote
desktop and screen sharing, so screenshots that reach a monitor edge show a faint blue tint.
Low-level input hooks run only while the lease is held. Jev tags its own input and the hooks
ignore only tagged input, so input from remote desktop tools counts as a person.

- Physical mouse movement is ignored.
- A physical click, scroll, or key press means the current decision is based on a screen the
  user has touched. Nothing more is sent. The run waits until there has been no physical input
  for 3 seconds, observes again, and continues; a task can refocus its window. If input
  arrives partway through typing, the action is `uncertain_effect` and is never retyped.
- Waiting extends the run deadline, by at most 300 seconds per run in total, but not the
  current call's `slice_seconds`. If the user is still active when the call or the allowance
  ends, the run pauses with `user_takeover` and `resumable: true`.
- A direct `desktop_act` that sees physical input returns `user_takeover`; inspect again.
- Physical Esc cancels the run that holds the lease (`Esc pressed`). It does not set the
  emergency stop.

Hooks cannot observe input sent to elevated windows unless the broker is elevated, or input on
the secure desktop.

## Troubleshooting

Run `jev-desktop doctor` for diagnostics. Remove credentials, tokens, private paths, and
application content before sharing output.

- Connection errors: use the same Windows session and `JEV_DESKTOP_PIPE` setting for client
  and broker. Run `jev-desktop broker` in the foreground to inspect startup errors.
- Policy errors: set `TYPESAFE_API_KEY` before starting the broker. Inspection and directed
  actions work without it.
- Refused input: check window focus, modal dialogs, scope, and privilege boundaries. Inspect
  again after resolving a stale observation. Do not bypass a guard.
- Partial observation: narrow the window scope or adjust observation limits. A partial tree
  cannot prove that a control is absent.
- Journal errors: check disk space and permissions, then restart the broker. Unfinished
  dispatches remain uncertain.

Clear the emergency stop with `jev-desktop stop --emergency --clear` only when input may
resume. Clearing it does not resume a run or replay input.

CLI exit codes are `0` for success and a passing run, `1` for request errors, and `2` for a
run that did not pass. MCP sends protocol messages to stdout and diagnostics to stderr.

## Tune decisions

Jev tuning changes the request and decision policy, not model weights. TypeSafe does not offer
customer fine-tuning or LoRA for Jev. Keep the model version fixed while comparing changes.

`PolicyConfig` in `src/jev_desktop/policy.py` defines the pinned model and three gates: the
operation confidence floor, the target confidence floor, and the target margin (how far the
chosen target must lead the runner-up). The broker's `config.json` can override each one under
`policy`. Evaluate changes using real application decisions and independently verified outcomes.

Set `JEV_DESKTOP_RECORD` to a directory before the broker starts to append every Jev request and
response body to `decisions-YYYYMMDD.jsonl` there. Headers are not recorded, and `secret_texts`
values never appear in requests, but the files hold application content: keep them private.
Turn recordings into rows to label, then sweep the gates offline:

```powershell
python scripts/export_decisions.py .artifacts/labeled.json .artifacts/recordings/*.jsonl
python scripts/evaluate_thresholds.py .artifacts/labeled.json
```

Label each row with `split`, `expected_operation`, and `expected_targets`: every acceptable target
key from `candidates`, or an empty list when no action is appropriate. Hold out whole
applications for `validation`. Re-running the export keeps existing labels. The sweep sends no
requests. It reports the selected gates, the current defaults, a wrong-versus-correct frontier,
and validation results by operation and candidate count. With zero wrong actions in *n*
accepted validation decisions, the wrong-action rate is only bounded near 3/*n*.

Start with the failed request and its observed outcome. Check whether the intended control
and value were available, whether each question states its operation, and whether input names
identify their destination. Remove duplicate or unavailable action candidates. Preserve the
state needed to distinguish the remaining choices. Keep arithmetic and exact comparisons in code.

Change one factor at a time. Use saved observations to compare question wording without
repeating desktop input. Label acceptable decisions from the application state before evaluating
thresholds, and reserve separate cases to check the chosen setting. List every equally valid
target; an unlisted alternative counts as a wrong action.
Record wrong accepted actions and correct actions refused, alongside completion time, requests,
and tokens. Offline
replay measures decisions; confirm changed behavior with a relevant live workflow.

Confidence describes the distribution across choices. It is not the probability that an entire
task succeeded. Evaluate operation and selected-target gates separately; ignore unused branch
answers. Choose thresholds for the consequences of the action, not to make a failed run continue.
Do not copy numerical thresholds from a cookbook as universal defaults.

Measure from task submission to the verified result. Report setup and screenshot time separately.
Exclude interrupted runs from speed comparisons, but report their failures. Keep the same initial
application state and completion check when comparing revisions. Do not repeat desktop runs
without a specific reason or the operator's agreement.

This follows TypeSafe's [confidence guidance](https://docs.typesafe.ai/confidence),
[confidence cookbook](https://docs.typesafe.ai/cookbooks/classification_using_confidence), and
[Jev 1.13 guidance](https://docs.typesafe.ai/model-jaggedness/jev-1.13).

Batch questions that share the same evidence. Each question must state its own premise because
questions cannot read one another's answers, and their IDs are not sent to the model. A second
request is appropriate when the chosen control determines the value question. Extra questions
consume tokens even when their answers are unused; compare request count and total input tokens.

Use the current [TypeSafe model documentation](https://docs.typesafe.ai/models) for provider
configuration. Jev Desktop honors provider `Retry-After` responses without adding a local rate limiter.
