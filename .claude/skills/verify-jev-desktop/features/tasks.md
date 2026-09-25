# Bounded tasks and predefined workflows

## Sub-features

Task handoff, supplied text/hotkeys, action/time budgets, workflow assertions, and saved-file verification.

## How to get to it (user POV)

CLI `run --task` or `run --spec`; MCP `desktop_run(task=...)` or `desktop_run(run=...)`.

## Driving it with CLI/MCP

Discover a disposable Standard Calculator. Copy `examples/calculator.json` into the run's ignored evidence directory and fill its application/window references from inspection. Execute `python -m jev_desktop.transports.cli --no-autostart run --task $taskFile`. Capture input JSON, output JSON, and final scoped inspection showing 15. Repeat through MCP when that entry point changes. The broker needs `TYPESAFE_API_KEY`; never put its value in artifacts.

For predefined workflows, the existing acceptance test types text in real Notepad, uses File > Save As, checks disk content, closes the owned process, and reopens the file to observe the text:

```powershell
$env:JEV_DESKTOP_LIVE = '1'
$proof = Join-Path '.artifacts/verification' ([guid]::NewGuid().ToString('N'))
python -m pytest tests/windows/test_jev_real_apps.py::test_jev_notepad_save_and_reopen -m live -q --basetemp "$proof/work" --junitxml "$proof/junit.xml"
Remove-Item Env:JEV_DESKTOP_LIVE
```

Restore any prior gate value instead of removing it if it was already set; use `try/finally` around the run. This acceptance test drives the real runtime and model, but is not a CLI/MCP end-to-end substitute. Preserve result JSON, images, saved text, and reopen assertions. Require a non-skipped passing test. Use a unique basetemp because pytest clears reused directories.

## Gotchas

Live tests send scoped observations to TypeSafe and inject native input. Run only with exclusive desktop access. The Notepad harness rejects process handoff instead of attaching to an unrelated user's window. Task completion is model-reported; inspect the actual result. CLI exit 2 means a non-pass outcome, not success. Missing fixtures, exhausted budgets, and uncertain decisions should return control rather than be retried blindly.
