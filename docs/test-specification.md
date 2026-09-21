# Test specification reference

A run is defined by one JSON document. The broker freezes it and records its digest, so a
resume can never quietly change what "pass" means.

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
| `interaction_mode` | `user_path` drives mouse and keyboard. `semantic` uses control patterns. The runtime never switches this for you. |
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

A verdict for the wrong binary is worthless, so identity is required and it must be
observable.

| Mode | Check |
| --- | --- |
| `file_marker` | Read `marker_path` and compare with `expect_marker`. The marker may be JSON with `build_id`, `build`, `version`, or `id`, or plain text. |
| `exe_hash` | Compare `sha256` of the running image with `expect_sha256`, and optionally its path with `expect_exe`. |
| `fresh_launch` | The process creation time must be at or after `launched_after`. |
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
| `required` | Defaults to true. A non-required step that has not run still blocks a pass, because the required path did not complete. |

`TYPE_TEXT` types the fixture value. The model never writes field text, which removes a whole
class of invented data from your test results. `SELECT` takes the option label from a fixture
and binds it to the control the policy chose, then verifies the control reports that value.
`HOTKEY` accepts a chord such as `ctrl+s`, supplied through a fixture reference.

## Assertions

| Field | Meaning |
| --- | --- |
| `assertion_id` | Unique. Also the key used to supply a caller result on resume. |
| `evaluator` | See the evaluator table. |
| `target` | What to look at. Shape depends on the evaluator. |
| `property` | Which value to read. Dotted paths work: `value`, `name`, `text`, `enabled`, `state.checked`, `rect.width`, or `count`. |
| `expected` | Exactly one comparator key. |
| `checkpoint` | A step id, `any`, `run_start`, or `run_end`. `any` is evaluated after every action. |
| `required` | Defaults to true. Only required assertions take part in the verdict. |
| `oracle` | Required for `model_visual`: `caller` or `provider`. |
| `deadline_s` | Reserved for bounded evaluators. |

Comparators: `equals`, `not_equals`, `contains`, `regex`, `in`, `is_true`, `is_false`,
`gte`, `lte`, `prefix`, `suffix`. Passing two comparators is a specification error, and the
assertion reports inconclusive with a runner origin rather than a failure.

### Evaluators

| Evaluator | Target | Notes |
| --- | --- | --- |
| `uia_property` | `role`, `name`, `name_regex`, `value_regex`, `window_ref`, `index` | Reads `property` from the first match. No match is a failure. |
| `uia_presence` | Same filters | An empty result with partial coverage becomes inconclusive, not a failure. |
| `uia_absence` | Same filters | Requires complete coverage with no truncation. A truncated tree can never prove absence. |
| `window_state` | `window_ref` or `title_regex` | Properties: `exists`, `visible`, `enabled`, `modal`, `focused`. |
| `artifact` | `path`, plus `json_field`, `run_id_field`, `content_regex` | Properties: `exists`, `sha256`, `size_at_least`, `mtime_after`, `json_path`, `run_scoped`, `content_regex`. |
| `process_identity` | empty | Reads the identity report. An unverifiable build gives inconclusive with an environment origin. |
| `model_visual` | empty | Needs `oracle`. Without a caller result the run pauses for visual assistance. |
| `caller_result` | empty | A verifier result you supply on resume, bound to the assertion and its evidence. |

`run_scoped` compares the JSON field named by `run_id_field` against this run's id. It is the
cheap way to catch an export left over from an earlier run. Artifact paths must sit inside an
approved root, and links or reparse points along the way are refused.

### Evidence labels

Deterministic evaluators produce `deterministic` results. A visual judgement you selected
produces `model_assessed`, and a verifier result you supplied produces `caller_supplied`.
Labels travel with the result so nobody later mistakes a judgement for a measurement.

## Limits

| Limit | Default | Local ceiling |
| --- | --- | --- |
| `max_actions` | 25 | 200 |
| `max_model_decisions` | 30 | 400 |
| `deadline_seconds` | 600 | 3600 |
| `slice_seconds` | 45 | 120 |
| `stale_retries` | 2 | 5 |
| `no_progress_retries` | 2 | 5 |

The broker clamps nothing on your behalf. A limit above the ceiling is rejected when the run
is created, so you find out immediately instead of at the end.

## Resume inputs

Resuming takes the `run_id` and the current `resume_token`, and it accepts exactly three
kinds of input:

```json
{"fixtures": {"name_value": "Ada Lovelace"},
 "visual_results": {"looks-right": {"status": "passed", "observed": {"note": "banner visible"},
                                    "evidence_refs": ["ev:..."]}},
 "verifier_results": {"state-file": {"status": "failed", "observed": {"reason": "stale run id"}}}}
```

A fixture name that is not declared in the specification is rejected. A visual result for an
assertion that did not select a visual oracle is rejected. Acceptance criteria, limits, and
failed steps cannot be touched.

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
| `low_confidence` | The policy answer was below the local floor. Narrow the observation or re-observe. |
| `invalid_model_response` | The answer failed validation. Do not retry blindly; look at the request. |
| `unexpected_model_version` | The pinned model id changed. Recalibrate before running again. |
| `stale_observation` | The observation no longer matches live state. Re-observe. |
| `unsupported_control` | The control genuinely lacks the affordance, or the step needs a different operation. |
| `permission_boundary` | A modal dialog, a disabled window, or a privilege boundary stopped input. |
| `user_takeover` | A person or another process owns the desktop. Stop and ask. |
| `uncertain_effect` | Input may have landed without a receipt. Recovery reconciles by observation; never assume and never replay. |
| `incorrect_build` | The running build is not the expected one. Fix the build first. |
| `budget_exhausted` | A limit was reached. Resume from the checkpoint. |
| `step_unresolved` | No progress after bounded retries, or the model asked to finish with required steps left. |

## Specifications that get rejected

Duplicate step or assertion ids. An assertion checkpoint that names no step. A `depends_on`
that names no step. A visual assertion without an oracle. Limits above the ceiling. More than
254 candidate targets for one operation, which the policy reports as
`needs_narrower_observation` instead of silently dropping options.
