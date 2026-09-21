# Examples

These run against the controlled fixture application in `tests/fixtures/`, so you can see a
whole run without pointing the plugin at software you care about.

## One-time setup

The fixture needs the desktop opt-in, because it creates real windows:

```bash
export JEV_DESKTOP_LIVE=1                        # PowerShell: $env:JEV_DESKTOP_LIVE = "1"
```

Register the fixture as an approved launch configuration in
`%LOCALAPPDATA%\JevDesktop\config.json`:

```json
{
  "launch_configs": {
    "fixture": {
      "executable": "python",
      "args": ["tests/fixtures/jev_fixture_app.py", "--scenario", "basic",
                "--state-dir", "C:/temp/jev-fixture", "--allow-desktop"],
      "cwd": "."
    }
  },
  "approved_roots": ["C:/temp/jev-fixture"],
  "evidence_dir": "C:/temp/jev-evidence"
}
```

The runtime gives a launched application the run id in `JEV_DESKTOP_RUN_ID`, and the fixture
writes it into `state.json`. That is what makes the run-scoped assertion in the first example
mean something.

## Running an example

```bash
jev-desktop inspect --pretty                 # lists the fixture once it is running
jev-desktop run --spec examples/specs/save-persists.json --pretty
```

`save-persists.json` and `toggle-state.json` reference `app:000000000000000000000000` as a
placeholder. Either pass the real reference from `inspect` in place of it, or let the
`LAUNCH_APP` step start the fixture and resolve the application afterwards. The point of the
examples is the shape of the specification, not those particular identifiers.

| Spec | Shows |
| --- | --- |
| `save-persists.json` | A typed fixture, a required click, a status assertion, and a run-scoped artifact assertion |
| `toggle-state.json` | Toggling a control and reading its nested state with `state.checked` |
| `visual-assistance.json` | A deterministic window assertion plus a visual assertion whose oracle you select |

## What happens on a pause

`save-persists.json` pauses with `needs_text` if you remove the `name_value` fixture. That is
the intended behaviour: the plugin will not invent the value. Supply it on resume:

```bash
jev-desktop run --run-id run:... --resume-token resume:... --fixture name_value="Ada Lovelace"
```

`visual-assistance.json` pauses with `needs_visual_assistance` the first time the visual
assertion comes due. Look at the screenshot it returns, then resume with your judgement:

```bash
jev-desktop run --run-id run:... --resume-token resume:... --visual '{"dialog-looks-right": {"status": "passed"}}'
```
