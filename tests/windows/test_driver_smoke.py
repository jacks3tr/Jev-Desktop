"""Pytest wrapper around the native driver self-test.

The self-test creates its own throwaway window and exercises UIA observation, capture,
user-path input, semantic invocation, a refused stale action, and identity binding against
it. Marked `windows` and `live`: it moves the real mouse for well under a second and never
touches a window it did not create.

Run with: python -m pytest tests/windows/test_driver_smoke.py -m live -q
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = [pytest.mark.windows, pytest.mark.live]


def test_native_selftest_reports_every_capability():
    environment = {**os.environ, "JEV_DESKTOP_LIVE": "1", "PYTHONIOENCODING": "utf-8"}
    process = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "native_selftest.py")],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=180,
        env=environment,
    )
    assert process.returncode == 0, process.stdout[-2000:] + process.stderr[-2000:]
    payload = json.loads(process.stdout)
    steps = {step["step"]: step for step in payload.get("steps", [])}
    assert payload.get("ok") is True, json.dumps(payload, indent=2)[-2000:]
    assert steps["observe"]["elements"] >= 4
    assert steps["capture"]["ok"] and steps["capture"]["bytes"] > 0
    assert steps["user_path_click"]["clicks_seen_by_window"] >= 1
    assert steps["user_path_type_text"]["observed"] == "hello-jev"
    assert steps["semantic_toggle"]["mechanism"] == "uia_pattern"
    assert steps["refused_stale_action"]["ok"], "a stale reference must be refused, not dispatched"
    assert steps["identity"]["status"] in {"verified", "mismatch", "unverifiable"}
