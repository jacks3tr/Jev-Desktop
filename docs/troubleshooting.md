# Troubleshooting

Start here:

```bash
jev-desktop doctor
```

It prints the pipe name, the broker session, driver health, policy configuration, evidence
usage, the active lease, and whether the emergency stop is set. Paste that output into a bug
report.

## The CLI says the broker is not reachable

The client starts a broker on demand and waits for the pipe. If that fails, check that the
current process runs in the same terminal session as the one that started the broker, and
that `JEV_DESKTOP_PIPE`, if you set it, points at a live broker. `jev-desktop broker` runs
one in the foreground if you want to see its log.

## Runs fail with a policy error before anything happens

Runs with steps need a decision policy. Set `TYPESAFE_API_KEY` in the broker's environment,
or configure `policy.api_key_env` to point at the variable you use. Without a key the broker
still serves inspection, `desktop_act`, `desktop_stop`, and status, and it says so instead of
guessing.

## A run pauses with `needs_text`

The step referenced a fixture that has no value. Declare it in the specification with a
`null` value if you intend to supply it at resume time, then pass it:

```bash
jev-desktop run --run-id run:... --resume-token resume:... --fixture name_value="Ada"
```

## A run pauses with `permission_boundary` and the window looks fine

The driver refuses input when the top-level window is disabled, which is what a modal dialog
does, and when activation is denied. Windows will not let a background process steal focus.
The driver activates an application by clicking it when a real mouse click is allowed, and it
says `window could not be activated` when Windows refuses. Bring the application forward
yourself, or let the policy click it as part of the user path.

## Clicks land on the wrong thing

They should not, and that is a bug worth reporting. The driver hit-tests the exact point
before dispatch and refuses when the control under that point is not the target. Two
situations produce a refusal that looks like a wrong click:

A window moved between observation and dispatch. The cached and live rectangles are compared
with a small tolerance, and a mismatch raises `stale_observation`.

Another window covers the target. The refusal says the point under the target is covered by a
different control. Move the covering window, or put the application under test in the
foreground as part of the test.

## Observation comes back partial

Partial coverage means something was skipped, and the snapshot says what. Common causes are
the element cap, the depth cap, and offscreen content. Raise `scope.max_elements` if the
application is large, or point `scope.window_refs` at the window you care about. Absence
assertions refuse to conclude anything from a partial observation, which is deliberate: a
truncated tree cannot prove that a control is missing.

## The run keeps pausing with `step_unresolved` after an escalation

`ESCALATE` means no offered target resolved the step. The usual cause is a thin observation:
the window is behind another window, so its controls report offscreen and observation skips
them. Add a `FOCUS_WINDOW` step for that window before the step that needs it, and check
`coverage` in the observation: partial coverage with a truncation note explains the escalation.

## A run pauses with `needs_visual_assistance`

Structured observation could not find the control the step needs. The pause carries a scoped
screenshot, the snapshot id, and the coordinate transform. Decide from the image, then either
resume to let the policy try again or use `desktop_act` bound to that snapshot. Custom-drawn
controls with no accessibility provider end up here, and that is the honest answer rather
than guessing at coordinates.

## The verdict is `inconclusive` even though everything ran

Check the assertion list. No assertions means no pass, always. Then check the build identity:
an unverifiable or mismatched build blocks a pass, because a result for an unknown binary is
worthless. Then check for uncertain effects in the step records.

## Evidence keeps growing

Screenshots are written at checkpoints, on failures, and when visual assistance is requested.
Retention prunes by age and total size, and failure evidence is kept preferentially. Lower
`capture_scale`, drop `checkpoint` from steps you do not need recorded, or move
`evidence_dir` to a disk with room. `jev-desktop status` shows current usage.

## The emergency stop is stuck on

```bash
jev-desktop stop --emergency --clear
```

Clearing is deliberately manual. A stop that could clear itself would not be a stop. Any
process running as your user can set the event, so check whether a script left it set before
assuming the plugin did.

## The broker reports a failing journal

When SQLite cannot record a dispatch intent, the broker stops mutating and reports
`journal_unhealthy`. That is the correct response: a run that cannot record what it is about
to do must not act. Check free disk space and the permissions on `journal_path`, then restart
the broker. Runs left mid-flight come back as uncertain and are reconciled by observation,
never replayed.

## Exit codes from the CLI

| Code | Meaning |
| --- | --- |
| 0 | Ran and passed |
| 1 | The call failed, for example a broker error or an invalid specification |
| 2 | Ran, but not a pass: paused, blocked, cancelled, error, or a verdict other than passed |

Scripts that only want real passes should treat anything but 0 as not-passed.

## MCP clients

The server speaks MCP over stdio and writes diagnostics to stderr, so stdout stays clean.
Point your client at `python -m jev_desktop.transports.mcp_stdio`. Screenshots come back as
image content when the tool call asks for inline images and the caller can use them, and as
fetchable evidence references otherwise. Nothing in the baseline path depends on MCP
background tasks, sampling, or elicitation.
