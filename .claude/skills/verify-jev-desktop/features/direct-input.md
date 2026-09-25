# Direct input

## Sub-features

Focus, click, type exact text, select, toggle, hotkey, and scroll through public actions. Explicit element targets need no policy key.

## How to get to it (user POV)

Inspect a disposable application window, then use CLI `act` or MCP `desktop_act` with that observation's handles.

## Driving it with CLI/MCP

Use a fresh disposable Calculator in Standard mode. Discover and scope it as in the discovery map. Select the observed button named `Seven` (localized names may differ), then invoke:

```powershell
python -m jev_desktop.transports.cli --no-autostart act --operation CLICK --snapshot $snapshotId --access-token $accessToken --window $windowRef --element $elementId
```

Populate variables from the latest inspection, not invented IDs. Reinspect immediately and assert the display shows 7. Save the before snapshot, action response, and after snapshot. For arithmetic coverage inspect anew for every button and use Seven, Plus, Eight, Equals; assert 15. MCP uses `desktop_act(access_token=..., operation="CLICK", snapshot_id=..., window_ref=..., element_id=...)`. Read `skills/desktop-use/SKILL.md` for the exact fields for other operations; test each changed operation with a fresh observation and visible end-state assertion.

## Gotchas

Use exclusive desktop access. Never reuse a stale snapshot after an action. A successful action response does not prove a display change. Close only the disposable window the run created. Coordinate input requires screenshot geometry and evidence references; do not guess coordinates or use it to bypass scoped handles.

