"""Check documentation links and keep desktop tests opt-in."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _markdown_files() -> list[Path]:
    names = subprocess.check_output(["git", "ls-files", "--", "*.md"], cwd=ROOT, text=True).splitlines()
    return [ROOT / name for name in names if (ROOT / name).is_file()]


def _strip_code_fences(text: str) -> str:
    return re.sub(r"```.*?```", "", text, flags=re.DOTALL)


def test_documentation_links_resolve():
    broken: list[str] = []
    pattern = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
    for path in _markdown_files():
        body = _strip_code_fences(path.read_text(encoding="utf-8", errors="replace"))
        for target in pattern.findall(body):
            link = target.split("#", 1)[0].strip()
            if not link or link.startswith(("http://", "https://", "mailto:", "#")):
                continue
            resolved = (path.parent / link).resolve()
            if not resolved.exists():
                broken.append(f"{path.relative_to(ROOT).as_posix()} -> {target}")
    assert not broken, "documentation links that point at nothing:\n" + "\n".join(broken)


def test_live_tests_stay_gated():
    """The live directory must collect nothing without the desktop opt-in."""
    environment = dict(os.environ)
    environment.pop("JEV_DESKTOP_LIVE", None)
    environment["PYTHONPATH"] = str(ROOT / "src")
    process = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/windows", "-q", "--collect-only"],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        env=environment,
        timeout=180,
    )
    output = process.stdout + process.stderr
    assert process.returncode in {0, 5}, output[-2000:]
    empty_markers = ("no tests collected", "no tests ran", "collected 0 items", "0 tests collected")
    assert any(marker in output for marker in empty_markers), output[-2000:]
    assert not re.search(r"collected [1-9]", output), "live tests were collected without JEV_DESKTOP_LIVE=1"
