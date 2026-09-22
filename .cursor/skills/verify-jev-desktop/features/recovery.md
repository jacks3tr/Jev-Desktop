# Stop, resume, status, and evidence

## Sub-features

Resume paused work with its current token, inspect status, retrieve evidence, cancel owned work, and emergency stop input.

## How to get to it (user POV)

CLI `run`, `status`, `evidence`, `stop`; MCP `desktop_run` and `desktop_stop`. CLI status/evidence have no same-named standalone MCP tools.

## Driving it with CLI/MCP

Use a disposable workflow paused for a missing fixture (the payload construction in `tests/unit/test_cli_transport.py::test_cli_reports_a_paused_run_with_a_non_zero_status` documents this case). Capture the pause reason and returned run ID/token. In subsequent calls use:

```powershell
python -m jev_desktop.transports.cli --no-autostart status --run-id $runId --resume-token $resumeToken
python -m jev_desktop.transports.cli --no-autostart run --run-id $runId --resume-token $resumeToken --fixture 'name_value=Ada Lovelace'
python -m jev_desktop.transports.cli --no-autostart evidence --evidence-id $evidenceId --resume-token $resumeToken --out $imagePath
python -m jev_desktop.transports.cli --no-autostart stop --run-id $runId --resume-token $resumeToken
```

Variables must come from current responses. Update rotated tokens. Verify resumed text in a fresh observation and verify retrieved image bytes exist. On a separately paused owned run, stop and confirm status reports cancellation and a subsequent resume does not inject input. Through MCP use `desktop_run(run_id=..., resume_token=..., fixtures={name_value:"Ada Lovelace"})` and `desktop_stop(run_id=..., resume_token=...)`.

For an explicitly scoped emergency-stop test on an otherwise idle desktop, CLI `stop --emergency` or MCP `desktop_stop(emergency=true)` must block subsequent input. Observe unchanged disposable-app content and the doctor's emergency flag. Clear only the stop established by this test once continuing is appropriate. Record before/action/after evidence.

## Gotchas

Emergency stop is session-wide, not isolated by custom pipes. Do not include it in unattended smoke tests. Retrieving evidence needs a run resume token or inspection access token. Tokens and image content stay in ignored local storage. Existing unit tests use driver doubles and are supporting coverage only. Never retry an uncertain native input just to obtain a cleaner transcript.

