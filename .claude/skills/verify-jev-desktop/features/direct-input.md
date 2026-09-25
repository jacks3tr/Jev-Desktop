# Direct input

## Sub-features

Focus, click, type exact text, select, toggle, hotkey, and scroll through public actions. Explicit element targets need no policy key.

## How to get to it (user POV)

Inspect a disposable application window, then use CLI `act` or MCP `desktop_act` with that observation's handles.

## Driving it with CLI/MCP

Use the disposable Notepad from [the skill's Drive section](../SKILL.md#drive). Inspect it, pick the `edit` element named `Text Editor`, and in the same script:

```powershell
jev act --operation TYPE_TEXT --snapshot $s.snapshot_id --access-token $s.access_token --window $win --element $edit.element_id --text 'Jev verify line'
jev act --operation TYPE_TEXT --snapshot $s.snapshot_id --access-token $s.access_token --window $win --element $edit.element_id --text 'again'   # refused: inspection already used
```

Reinspect and assert the editor's `value`. Then inspect again and send `--operation HOTKEY --hotkey ctrl shift q`: it is refused before input (`error.code` `paused`, `detail.reason` `unsupported_control`, `detail.supported` lists the allowed chords) and leaves that inspection usable, so the same `snapshot_id` then sends `--hotkey ctrl s`. Assert the file on disk holds the typed text. MCP uses `desktop_act(access_token=..., operation=..., snapshot_id=..., window_ref=..., element_id=...)`; read `skills/desktop-use/SKILL.md` for the other operations' fields and test each changed operation with a fresh observation and a visible end-state assertion.

## Gotchas

Use exclusive desktop access. `act` exits 1 for every refusal, so read `error.code` (`invalid_request`, `paused`, `uncertain_effect`, `emergency_stop`) rather than the exit code. Never retry `uncertain_effect`. A successful action response does not prove a display change. Close only the window the run launched. Coordinate input has no CLI flag: build the `point` object from screenshot evidence inside `--action` JSON; never guess coordinates or use them to bypass scoped handles.
