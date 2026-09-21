## What this changes

<!-- One short paragraph. Name the failure mode or the capability, not the diff. -->

## Why

<!-- What breaks today without this, or what the user cannot do. Link the issue if there is one. -->

## How it was verified

<!-- Paste the exact commands and their results. Evidence, not adjectives. -->

```
python -m pytest tests/unit -q
```

- [ ] Offline suite passes
- [ ] Lint and format pass (`ruff format --check .`, `ruff check .`)
- [ ] Type check passes (`mypy`)
- [ ] Live suites were run, if behaviour or the driver changed (`JEV_DESKTOP_LIVE=1 ...`)
- [ ] If live suites were not run, say so here and explain why:
- [ ] No window-creating command was run on a machine someone else was using

## Checklist

- [ ] Guards were not weakened to make a test pass
- [ ] Cross-boundary types were added to `contracts.py` with validation
- [ ] Documentation updated, meaning `README.md`, `docs/`, or `CHANGELOG.md` where a user can see the change
- [ ] Adapted third-party material is recorded in `NOTICE`
