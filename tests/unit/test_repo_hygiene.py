"""Repository hygiene: the properties that are easy to lose in a later refactor.

Three checks, each of which has failed in real repositories:

* documentation links that point at files nobody moved,
* live tests that quietly become runnable without the desktop opt-in,
* prose that drifts into typographic dashes and curly quotes, which makes diffs noisy and
  tooling inconsistent.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

TEXT_SUFFIXES = {".py", ".md", ".toml", ".yml", ".yaml", ".json", ".cfg", ".txt"}
IGNORED_DIRS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "build",
    "dist",
    ".venv",
    "venv",
    "htmlcov",
    ".artifacts",
}
BANNED_CHARACTERS = {
    "\u2014": "em dash",
    "\u2013": "en dash",
    "\u2018": "left single curly quote",
    "\u2019": "right single curly quote",
    "\u201c": "left double curly quote",
    "\u201d": "right double curly quote",
    "\u2026": "ellipsis character",
}
# Contributor Covenant text is quoted verbatim from the published standard, so it is not ours
# to restyle beyond the dashes check.
STYLE_EXEMPT_FILES = {Path("CODE_OF_CONDUCT.md")}


def tracked_text_files() -> list[Path]:
    files: list[Path] = []
    for path in sorted(ROOT.rglob("*")):
        if path.is_dir() or any(part in IGNORED_DIRS for part in path.parts):
            continue
        if path.suffix in TEXT_SUFFIXES or path.name in {".gitignore", ".gitattributes", ".editorconfig", "CODEOWNERS"}:
            files.append(path)
    return files


def test_no_typographic_dashes_or_curly_quotes():
    offenders: list[str] = []
    for path in tracked_text_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        found = {character for character in text if character in BANNED_CHARACTERS}
        if found:
            names = ", ".join(sorted(BANNED_CHARACTERS[character] for character in found))
            offenders.append(f"{path.relative_to(ROOT).as_posix()}: {names}")
    assert not offenders, "plain ASCII punctuation only:\n" + "\n".join(offenders)


def _markdown_files() -> list[Path]:
    return [path for path in tracked_text_files() if path.suffix == ".md"]


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
