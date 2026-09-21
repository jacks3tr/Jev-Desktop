"""Live-suite gate: nothing in this directory may touch the desktop by accident.

Everything under `tests/windows` creates real windows and injects real input. Collection is
skipped unless the operator explicitly opts in:

    set JEV_DESKTOP_LIVE=1 && python -m pytest tests/windows -m live -q

Without that variable, importing/pytest-collecting these modules cannot start a fixture
process (the fixture itself also refuses to create a window without the opt-in).
"""

from __future__ import annotations

import os

import pytest

if os.environ.get("JEV_DESKTOP_LIVE") != "1":
    collect_ignore_glob = ["*.py"]


def pytest_collection_modifyitems(config, items):
    if os.environ.get("JEV_DESKTOP_LIVE") != "1":
        for item in items:
            if str(item.fspath).startswith(str(config.rootdir)):
                item.add_marker(pytest.mark.skip(reason="desktop access is not enabled (JEV_DESKTOP_LIVE=1)"))
