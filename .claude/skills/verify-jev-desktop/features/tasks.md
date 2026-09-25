# Bounded tasks and predefined workflows

## Sub-features

Task handoff, supplied text/hotkeys, action/time budgets, workflow assertions, and saved-file verification.

## How to get to it (user POV)

CLI `run --task` or `run --spec`; MCP `desktop_run(task=...)` or `desktop_run(run=...)`.

## Driving it with CLI/MCP

The broker needs `TYPESAFE_API_KEY` in its environment; never put its value in artifacts. Against the disposable Notepad from [the skill's Drive section](../SKILL.md#drive), write a task file into the evidence directory:

```json
{"goal": "In Notepad, replace the editor's text with the supplied line, then save the file with ctrl+s. Stop once the editor shows exactly the supplied line and the title has no unsaved-changes asterisk.",
 "app_ref": "<from inspection>", "window_refs": ["<from inspection>"],
 "texts": {"line": "Saved by a Jev task"}, "hotkeys": ["ctrl+s"], "max_actions": 6, "timeout_seconds": 90}
```

Run `jev run --task $taskFile`. Capture input and output JSON, then assert the file on disk holds the line; `completion: model_reported` alone is not proof. `examples/calculator.json` shows the same shape. Repeat through MCP when that entry point changes.

For predefined workflows, the existing acceptance test types text in real Notepad, uses File > Save As, checks disk content, closes the owned process, and reopens the file to observe the text. It also needs `TYPESAFE_API_KEY` (or `JEV_TYPESAFE_ENV_FILE`):

```powershell
$prior = $env:JEV_DESKTOP_LIVE
$proof = Join-Path '.artifacts/verification' ([guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force $proof | Out-Null   # --basetemp fails if its parent is missing
try {
    $env:JEV_DESKTOP_LIVE = '1'
    python -m pytest tests/windows/test_jev_real_apps.py::test_jev_notepad_save_and_reopen -m live -q --basetemp "$proof/work" --junitxml "$proof/junit.xml"
} finally {
    if ($null -eq $prior) { Remove-Item Env:JEV_DESKTOP_LIVE -ErrorAction SilentlyContinue } else { $env:JEV_DESKTOP_LIVE = $prior }
}
```

This acceptance test drives the real runtime and model but bypasses the broker, so it is not a CLI/MCP end-to-end substitute. Require a non-skipped pass in `junit.xml`; the unique basetemp keeps pytest from clearing earlier proof.

## Gotchas

Live tests send scoped observations to TypeSafe and inject native input. Run only with exclusive desktop access. The Notepad harness rejects process handoff instead of attaching to an unrelated user's window. Task completion is model-reported; inspect the actual result. CLI exit 2 means a non-pass outcome, not success. Missing fixtures, exhausted budgets, and uncertain decisions should return control rather than be retried blindly.
