# Discovery and inspection

## Sub-features

- Discover applications and windows; filter by title or executable substring.
- Inspect a single application with explicit window scope.
- Request screenshot evidence only when needed.

## How to get to it (user POV)

Run CLI `inspect` or call MCP `desktop_inspect`. Use returned references to inspect the intended window.

## Driving it with CLI/MCP

Run `python .cursor/skills/verify-jev-desktop/scripts/smoke.py`. It launches the real broker and executes `doctor`, `inspect --no-screenshot`, and `inspect --query jev-no-match-<run UUID> --no-screenshot`. Proof: at least one window, every window belongs to a returned application, and the unique unmatched query returns empty app/window lists. The helper supplies the actual UUID, no manual substitution is needed.

For scoped coverage, run `python -m jev_desktop.transports.cli --no-autostart inspect --query Calculator --no-screenshot` against a disposable Calculator. Copy the returned `app_ref` and `window_ref` into `inspect --app-ref $appRef --window $windowRef --no-screenshot`. Assert the returned window matches and inspect `elements`, `coverage`, and `truncation`. Repeat with screenshot enabled if changing capture behavior. MCP equivalents are `desktop_inspect(query="Calculator", screenshot=false)` followed by `desktop_inspect(app_ref=..., window_refs=[...], screenshot=false)` using actual returned values.

## Gotchas

References are opaque and ephemeral. Discovery is not a permission grant. An empty result may mean no visible matching window. Screenshot-free discovery still contains private metadata. Do not claim scoped inspection or screenshot coverage from the discovery smoke.

