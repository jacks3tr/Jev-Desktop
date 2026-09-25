# Discovery and inspection

## Sub-features

- Discover applications and windows; filter by title or executable substring.
- Inspect a single application with explicit window scope; a scoped `query` returns only matching elements.
- Request screenshot evidence only when needed.

## How to get to it (user POV)

Run CLI `inspect` or call MCP `desktop_inspect`. Use returned references to inspect the intended window.

## Driving it with CLI/MCP

Run `python .claude/skills/verify-jev-desktop/scripts/smoke.py`. It launches the real broker and executes `doctor`, `inspect --no-screenshot`, and `inspect --query jev-no-match-<run UUID> --no-screenshot`. Proof: at least one window, every window belongs to a returned application, and the unique unmatched query returns empty app/window lists. The helper supplies the actual UUID, no manual substitution is needed.

For scoped coverage, launch the disposable Notepad from [the skill's Drive section](../SKILL.md#drive) and run `inspect --query <its file name> --no-screenshot`. Copy the returned `app_ref` and `window_ref` into `inspect --app-ref $appRef --window $windowRef --no-screenshot`. Assert the returned window matches and inspect `elements`, `coverage`, and `truncation`. Then add `--query 'Text Editor'`: only elements whose name, value, text, or path contain the query come back. Drop `--no-screenshot` when changing capture behavior and assert `screenshot.evidence_id` is present and `screenshot_error` is null. MCP equivalents are `desktop_inspect(query=..., include_screenshot=false)` followed by `desktop_inspect(app_ref=..., window_refs=[...], include_screenshot=false)` using actual returned values.

## Gotchas

References are opaque and ephemeral. Discovery is not a permission grant. An empty result may mean no visible matching window. Screenshot-free discovery still contains private metadata. Do not claim scoped inspection or screenshot coverage from the discovery smoke.

Avoid Calculator as the disposable target: its window belongs to `ApplicationFrameHost`, whose `app_ref` also groups every other open UWP window (Sticky Notes, Snip & Sketch), and a user's existing Calculator is indistinguishable from yours. Truncation lines name the limit hit, such as `depth cap reached (12)`, `traversal node budget reached`, or `<n> offscreen elements were not observed`.
