"""No credential may be committed, and no local machine detail may leak into the repository.

This is a guard, not a scanner: it checks the shapes that matter for this project and stays
quiet about the placeholders the documentation uses. Run it before pushing, and let CI run it
on every push.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".zip", ".whl", ".tar", ".gz", ".sqlite"}

# Names that should never be tracked at all.
FORBIDDEN_NAMES = {".env", ".env.local", ".env.production", "id_rsa", "id_ed25519", "credentials.json"}

SECRET_SHAPES = {
    "private key block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    # A real OpenAI-style key is sk- followed by a long unbroken run of key characters.
    "OpenAI-style key": re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9]{32,}"),
    "Anthropic key": re.compile(r"(?<![A-Za-z0-9])sk-ant-[A-Za-z0-9_\-]{24,}"),
    "GitHub token": re.compile(r"\b(ghp_|gho_|ghu_|ghs_|ghr_)[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}"),
    "Slack token": re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"),
    "AWS access key id": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "Google API key": re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    "JSON web token": re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    "Bearer token literal": re.compile(r"[Bb]earer\s+[A-Za-z0-9_\-\.]{24,}"),
    # An assignment to a secret-named field with a long literal value. Variable names such as
    # TYPESAFE_API_KEY are fine; a filled-in value is not.
    "assigned secret literal": re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|password|passwd)"
        r"\b\s*[:=]\s*[\"'][^\"'\s]{16,}[\"']"
    ),
    "credential in URL": re.compile(r"[a-z][a-z0-9+.\-]{1,20}://[^/\s:@]+:[^/\s@]{6,}@"),
}

# Local machine detail that should not travel with the code.
LOCAL_SHAPES = {
    "Windows user profile path": re.compile(r"[Cc]:\\{1,2}Users\\{1,2}[A-Za-z0-9._\-]+"),
    "macOS or Linux home path": re.compile(r"/(Users|home)/[A-Za-z0-9._\-]+"),
}

PLACEHOLDERS = ("example", "placeholder", "your-", "changeme", "redacted", "dummy", "...", "xxxx")


def tracked_files() -> list[str]:
    return subprocess.run(["git", "ls-files"], capture_output=True, text=True, cwd=str(ROOT), check=True).stdout.split()


def test_detector_catches_known_shapes():
    """The guard above is only worth having if it fires on real credential shapes.

    The samples are assembled at runtime so this file never contains a credential-shaped
    literal. That keeps repo-wide scans and GitHub push protection quiet without weakening
    the check: the detector still sees a complete key, the source never holds one.
    """
    must_match = {
        "OpenAI-style key": "sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8",
        "GitHub token": "ghp_" + "Z" * 36,
        "AWS access key id": "AKIA" + "IOSFODNN7QWERTYU",
        "assigned secret literal": "api_key" + " = " + chr(34) + "9f8e7d6c5b4a3210" + chr(34),
        "credential in URL": "postgres://" + "reporting" + ":" + "hunter2secret" + "@" + "db.internal/warehouse",
        "Bearer token literal": "Authorization: Bearer " + "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
        "Windows user profile path": "C:" + chr(92) + "Users" + chr(92) + "somebody",
        "macOS or Linux home path": "/" + "home" + "/" + "somebody" + "/src",
    }
    for label, sample in must_match.items():
        pattern = SECRET_SHAPES.get(label) or LOCAL_SHAPES[label]
        assert pattern.search(sample), f"{label} pattern missed {sample!r}"

    must_not_match = [
        "risk-assessment-runner",
        "sk-runner",
        "task-management-system",
        "TYPESAFE_API_KEY",
        'api_key_env="DEFINITELY_NOT_SET"',
        "Bearer {self._key()}",
        "C:/temp/jev-fixture",
        "%LOCALAPPDATA%/JevDesktop/config.json",
        "policy.api_key_provider=lambda: None",
    ]
    for sample in must_not_match:
        for label, pattern in {**SECRET_SHAPES, **LOCAL_SHAPES}.items():
            assert not pattern.search(sample), f"{label} pattern fired on benign text {sample!r}"


def test_no_secret_shaped_strings_in_tracked_files():
    findings: list[str] = []
    for name in tracked_files():
        path = ROOT / name
        if path.suffix.lower() in BINARY_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for label, pattern in SECRET_SHAPES.items():
            for match in pattern.finditer(text):
                matched = match.group(0)
                if any(marker in matched.lower() for marker in PLACEHOLDERS):
                    continue
                line = text[: match.start()].count("\n") + 1
                findings.append(f"{name}:{line}: {label}: {matched[:40]}")
    assert not findings, "possible credentials in tracked files:\n" + "\n".join(findings)


def test_no_local_machine_paths_in_tracked_files():
    """A repo is not the place for the maintainer's home directory."""
    findings: list[str] = []
    for name in tracked_files():
        path = ROOT / name
        if path.suffix.lower() in BINARY_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for label, pattern in LOCAL_SHAPES.items():
            for match in pattern.finditer(text):
                line = text[: match.start()].count("\n") + 1
                findings.append(f"{name}:{line}: {label}: {match.group(0)}")
    assert not findings, "local paths leaked into the repository:\n" + "\n".join(findings)


def test_no_credential_files_are_tracked():
    offenders = [name for name in tracked_files() if Path(name).name in FORBIDDEN_NAMES]
    assert not offenders, f"credential files are tracked: {offenders}"


def test_runtime_state_stays_ignored():
    """Journals, evidence, and local config can hold secrets or private screenshots."""
    samples = [
        ".env",
        ".env.local",
        "journal.sqlite",
        "journal.sqlite-wal",
        "config.json",
        "config.local.json",
        "evidence/shot.png",
        "dist/pkg.whl",
        "build/lib/x.py",
        ".artifacts/run/trace.json",
        ".pytest_cache/state",
        "htmlcov/index.html",
    ]
    trackable = []
    for sample in samples:
        result = subprocess.run(["git", "check-ignore", "-q", sample], cwd=str(ROOT), capture_output=True)
        if result.returncode != 0:
            trackable.append(sample)
    assert not trackable, f"these paths would be committed: {trackable}"
