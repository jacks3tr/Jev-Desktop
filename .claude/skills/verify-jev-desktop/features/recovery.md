# Stop, resume, status, and evidence

## Sub-features

Resume paused work with its current token, inspect status, retrieve evidence, cancel owned work, and emergency stop input.

## How to get to it (user POV)

CLI `run`, `status`, `evidence`, `stop`; MCP `desktop_run` and `desktop_stop`. CLI status/evidence have no same-named standalone MCP tools.

## Driving it with CLI/MCP

Pause a spec run for a missing fixture against the disposable Notepad: one `TYPE_TEXT` step with `target_description` "the Notepad text editor" and `fixture_reference` `name_value`, `fixtures: {"name_value": null}`, and a `uia_property` assertion that the `edit` named `Text Editor` has value `Ada Lovelace` (the payload in `tests/unit/test_cli_transport.py::test_cli_reports_a_paused_run_with_a_non_zero_status` has the full shape). Spec runs need a verified build: `expected_identity: {"mode": "exe_hash", "expect_exe": "C:\\Windows\\System32\\notepad.exe", "expect_sha256": "<Get-FileHash>"}`. With `mode: any` the run is blocked as `incorrect_build`. Expect exit 2 and `reason` `needs_text`, then:

```powershell
jev status --run-id $runId --resume-token $token                               # status.run.status == paused
jev run --run-id $runId --resume-token $token --fixture 'name_value=Ada Lovelace'  # completed, verdict passed, token rotated
```

Reinspect and assert the editor shows the name. On a second paused run, inspect with `--run-id $runId --resume-token $token` to capture run-bound evidence, fetch it with `evidence --evidence-id $id --resume-token $token --out $png`, then `stop --run-id $runId --resume-token $token`. Status reports `cancelled`, and a resume fails with `invalid_request` "run is terminal" and types nothing. Through MCP use `desktop_run(run_id=..., resume_token=..., fixtures={name_value:"Ada Lovelace"})` and `desktop_stop(run_id=..., resume_token=...)`.

For an explicitly scoped emergency-stop test on an otherwise idle desktop, set it with CLI `stop --emergency` or MCP `desktop_stop(emergency=true)`. Doctor reports `emergency_stop: true`, and an `act` on a fresh inspection fails with `error.code` `emergency_stop`. Clear it in a `finally` with `stop --emergency --clear`, which exists on the CLI only. Confirm doctor reports false on both your broker and the default one.

## Gotchas

Emergency stop is session-wide, not isolated by custom pipes; the CLI sets and clears it without contacting any broker. Do not include it in unattended smoke tests. Evidence from an inspection outside a run is fetched with that screenshot's own `screenshot.access_token`, not the inspection's top-level `access_token`; run evidence uses the run's current resume token. Tokens and image content stay in ignored local storage. Existing unit tests use driver doubles and are supporting coverage only. Never retry an uncertain native input just to obtain a cleaner transcript.
