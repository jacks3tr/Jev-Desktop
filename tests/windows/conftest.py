"""Live-suite gate: nothing in this directory may touch the desktop by accident.

Everything under `tests/windows` creates real windows and injects real input. Collection is
skipped unless the operator explicitly opts in:

    set JEV_DESKTOP_LIVE=1 && python -m pytest tests/windows -m live -q

Without that variable, pytest does not collect the desktop tests.
"""

from __future__ import annotations

import os

import pytest

if os.environ.get("JEV_DESKTOP_LIVE") != "1":
    collect_ignore_glob = ["*.py"]


from pathlib import Path

from jev_desktop.policy import HttpTransport, JevPolicy, PolicyConfig


@pytest.fixture
def jev_policy():
    key = os.environ.get("TYPESAFE_API_KEY")
    path = os.environ.get("JEV_TYPESAFE_ENV_FILE")
    if not key and path:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("TYPESAFE_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
    if not key:
        pytest.fail("real Jev acceptance requires TYPESAFE_API_KEY or JEV_TYPESAFE_ENV_FILE")
    transport = HttpTransport()
    try:
        yield JevPolicy(transport=transport, config=PolicyConfig(), api_key=key)
    finally:
        transport.close()
