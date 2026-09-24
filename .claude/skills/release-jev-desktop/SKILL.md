---
name: release-jev-desktop
description: Release Jev Desktop - bump the version, land it through a PR, tag, publish the GitHub release, and update the local Claude Code plugin. Use when asked to release, publish, ship, tag, or cut a version of Jev Desktop, or to make Claude Code use the latest Jev Desktop.
---

# Release Jev Desktop

A release is a version bump merged to `main`, a `vX.Y.Z` tag on that merge, a published GitHub
release with the sdist and wheel, and the local Claude Code plugin updated to it.

**Keep this skill fast.** When a release makes you investigate or look something up, add the
answer to [Tips](#tips) before you finish, and correct any step that turned out to be wrong.
Delete tips that no longer apply. The next release should not repeat your research.

## 1. Choose the version

Semantic versioning, pre-1.0: bump the minor version for new features or anything a user or
agent must adapt to (MCP tool arguments, pipe or lock names, contracts, limits, skill
behavior); bump the patch version for fixes only. Compare with the last release:

```powershell
gh release list --limit 3
git log --oneline "$(git describe --tags --abbrev=0)..origin/main"
```

## 2. Bump every version

These must match; `tests/unit/test_repo_hygiene.py` fails otherwise:

- `pyproject.toml` (`[project] version`)
- `src/jev_desktop/__init__.py` (`__version__`, reported by the MCP server)
- `.claude-plugin/plugin.json` and `.codex-plugin/plugin.json`

Claude Code caches plugins by version: without a bump, `claude plugin update` keeps the old
skills and manifest.

Review `skills/desktop-use/SKILL.md` against the release's changes as `CLAUDE.md` describes:
it must cover every behavior agents must know about, contain nothing stale, and still read in
working order. Fix it in the release PR if not.

## 3. Verify

```powershell
python -m ruff format --check .
python -m ruff check .
python -m mypy
python -m pytest tests/unit -q
python .cursor/skills/verify-jev-desktop/scripts/smoke.py
& "$env:USERPROFILE\.local\bin\claude.exe" plugin validate .
```

## 4. Land it through a pull request

Release notes are generated from merged pull requests, so a direct push to `main` is missing
from them. Open a PR with the bump (and any unreleased work), wait for CI, and merge it with a
merge commit, as earlier PRs were. Tag only a `main` commit whose CI passed: the release
workflow builds but does not run the tests.

## 5. Tag and let the workflow build

```powershell
git checkout main
git pull --ff-only
git tag -a vX.Y.Z -m "Jev Desktop vX.Y.Z"
git push origin vX.Y.Z
gh run list --workflow release.yml --limit 1
gh run watch <run-id> --exit-status
```

`.github/workflows/release.yml` checks that the tag matches all three manifest versions,
builds, runs `twine check`, and creates a **draft** release with both distributions and
generated notes.

## 6. Write the notes and publish

Match the previous release's shape (`gh release view <previous-tag> --json body`): title
`Jev Desktop vX.Y.Z`; a short plain-language summary of what changed for people using it; an
**Upgrade notes** section for anything a user must do (restart the broker, changed tool
arguments); the installation link `https://github.com/jacks3tr/Jev-Desktop/tree/vX.Y.Z#readme`;
then the generated "What's Changed" list, unchanged. Write the notes to a file, then:

```powershell
gh release edit vX.Y.Z --title "Jev Desktop vX.Y.Z" --notes-file <notes.md> --draft=false --latest
gh release view vX.Y.Z --json isDraft,tagName,assets --jq "{isDraft,tagName,assets:[.assets[].name]}"
```

## 7. Update Claude Code on this machine

The plugin is `jev-desktop@jev-desktop` from the GitHub marketplace `jacks3tr/Jev-Desktop`.
Its skills come from the plugin cache; its MCP server runs `python -m
jev_desktop.transports.mcp_stdio` from the system Python, which has an editable install of
this checkout, so the server runs whatever the checkout contains.

```powershell
git checkout main
git pull --ff-only
& "$env:USERPROFILE\.local\bin\claude.exe" plugin marketplace update jev-desktop
& "$env:USERPROFILE\.local\bin\claude.exe" plugin update jev-desktop@jev-desktop -y
```

Confirm `version` and `gitCommitSha` for `jev-desktop@jev-desktop` in
`~/.claude/plugins/installed_plugins.json` match the tag. Reinstall the editable package only
when dependencies or entry points changed (`C:\Python314\python.exe -m pip install -e .`).

Running MCP servers and the broker keep the old code in memory. When broker, pipe, driver, or
runtime code changed, stop the old broker (find it with the command in [Tips](#tips)) once no
run is in progress, then tell the user to restart their Claude Code sessions.

## Tips

- PowerShell does not pipe a here-string into `git commit -F -` or `gh ... --body -`. Write the
  message to a file in the scratchpad and pass `-F <file>` or `--body-file <file>`.
- `gh release view --json` has no `isLatest` field; `gh release list` shows `Latest`.
- The `claude` CLI is `~/.local/bin/claude.exe`; it is not on Git Bash's `PATH`.
- `claude plugin validate .` warns that the marketplace has no description. It is harmless.
- Git warns "CRLF will be replaced by LF" on commit: `.gitattributes` normalizes line endings.
- Find the running broker and MCP servers:
  `Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object { $_.CommandLine -match 'jev_desktop' } | Select-Object ProcessId,CommandLine`.
  The broker's command line ends in `jev_desktop.broker`. Stop it with `Stop-Process -Id <pid>`.
- Codex cuts MCP tool calls off after 60 seconds by default (`tool_timeout_sec`); Claude Code
  allows about 28 hours (`MCP_TOOL_TIMEOUT`). Keep that in mind when a release changes task
  or call time limits.
- 0.1.0 to 0.2.0 changed the logon ID used in pipe, event, and lock names: an old broker is
  invisible to new clients, so it had to be stopped by process ID.
- Read the draft's generated "What's Changed" before writing the summary: it lists every PR
  merged since the previous tag, which can include features you did not work on.
- `gh pr merge <n> --merge --delete-branch` deletes the remote and local branch and leaves you
  on an updated `main`.
- Confirm the local update with `C:\Python314\python.exe -m jev_desktop.transports.cli doctor`:
  it starts a broker from the checkout if none is running and reports its pipe and policy.
- Merging to `main` runs CI again. Find that run with
  `gh run list --branch main --limit 1 --json headSha,status,conclusion`, wait for success, then
  tag that exact commit: `git tag -a vX.Y.Z -m "..." <sha>`.
- PowerShell mangles `git rev-parse vX.Y.Z^{commit}`; use `git rev-list -n 1 vX.Y.Z`.
- Before stopping the broker, confirm no run is active: the `runs` table in
  `%LOCALAPPDATA%\JevDesktop\journal.sqlite` shows each run's `status` and `updated_at`. Paused
  runs are stored and survive the restart; the next client call autostarts a new broker.
- Auto mode may refuse `gh pr merge` for a PR the user did not approve by name, for example one
  opened after they said "merge". Ask rather than work around it.
- In the Claude desktop app, read the release PR's CI with the `ccd_pr` `get_status` tool rather
  than polling `gh pr checks`. For `main`'s run after the merge, run
  `gh run watch <run-id> --exit-status` in the background.
- A broker started after the fixes merged already runs the released code. A bump-only release
  needs no broker restart then: compare the broker's start time with the fix's merge time.
- The release workflow's draft already has both distributions attached; publishing only needs
  `gh release edit`. The full 0.2.0 release took one CI run (about 5 minutes) and one release
  run (about 1 minute).
