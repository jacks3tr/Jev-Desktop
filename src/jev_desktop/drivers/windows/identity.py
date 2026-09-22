"""Application and build identity binding.

A test result is only meaningful for the build that actually ran. Binding is to the
observed process (pid + creation time + image), never to a window title, and an expected
build identity must be *observed*: repository HEAD or the generic interpreter executable
alone is not evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from ...contracts import DriverError, ExpectedIdentity, IdentityReport, IdentityStatus, now
from . import win32


def sha256_file(path: str) -> str | None:
    """Hash the file as it is now. Deliberately uncached: a rebuilt executable copied with
    its size and timestamps preserved must never be reported as the previous build."""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest()


@dataclass(frozen=True)
class ObservedApp:
    pid: int
    executable_path: str
    creation_time: float
    window_handle: int


def observe_app(pid: int, hwnd: int) -> ObservedApp:
    return ObservedApp(
        pid=pid,
        executable_path=win32.process_image_path(pid),
        creation_time=win32.process_creation_time(pid),
        window_handle=hwnd,
    )


def _read_marker(path: str | None) -> tuple[str | None, str | None]:
    """Return (marker_value, note). Marker may be JSON with a build_id field or plain text."""
    if not path:
        return None, "no marker path configured"
    try:
        with open(path, "rb") as handle:
            raw = handle.read(64 * 1024)
    except OSError as exc:
        return None, f"marker unreadable: {exc}"
    text = raw.decode("utf-8", errors="replace").strip()
    try:
        payload = json.loads(text)
    except ValueError:
        return text, None
    if isinstance(payload, dict):
        for key in ("build_id", "build", "version", "id"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value, None
        return text, "marker JSON had no recognised build field"
    return text, None


def verify_identity(
    app_ref: str,
    *,
    pid: int,
    hwnd: int,
    expected: ExpectedIdentity,
    title: str = "",
    class_name: str = "",
) -> IdentityReport:
    notes: list[str] = []
    observed: dict[str, object] = {
        "process_id": pid,
        "window_title": title or win32.window_title(hwnd),
        "window_class": class_name or win32.window_class(hwnd),
    }
    try:
        observed_app = observe_app(pid, hwnd)
    except DriverError as exc:
        # Package-based modes do not need the window's own process to be inspectable: a frame
        # host or a launcher may already be gone while the application keeps running.
        notes.append(f"the window process could not be inspected: {exc}")
        observed_app = None
    if observed_app is not None:
        observed.update(
            {
                "executable_path": observed_app.executable_path,
                "process_creation_time": observed_app.creation_time,
            }
        )
    expectation: dict[str, object] = expected.to_json()

    def report(status: IdentityStatus, *extra: str) -> IdentityReport:
        return IdentityReport(
            app_ref=app_ref,
            status=status,
            expected=expectation,
            observed=observed,
            evidence_refs=(),
            notes=tuple(notes + list(extra)),
            checked_at=now(),
        )

    if expected.mode == "any":
        notes.append("no build identity expectation supplied; the running build is unverified")
        return report(IdentityStatus.UNVERIFIABLE)

    if (
        observed_app is not None
        and expected.mode in {"fresh_launch", "exe_hash"}
        and Path(observed_app.executable_path).stem.lower()
        in {"python", "pythonw", "node", "dotnet", "java", "javaw", "applicationframehost"}
    ):
        return report(
            IdentityStatus.UNVERIFIABLE,
            "host executable identity is not application identity; use a runtime build marker",
        )

    if expected.mode == "fresh_launch":
        if expected.launched_after is None or not expected.expect_exe:
            notes.append("fresh_launch requires launched_after and the intended executable path")
            return report(IdentityStatus.UNVERIFIABLE)
        observed["launched_after"] = expected.launched_after
        if observed_app is None:
            return report(IdentityStatus.UNVERIFIABLE)
        if os.path.normcase(os.path.abspath(observed_app.executable_path)) != os.path.normcase(
            os.path.abspath(expected.expect_exe)
        ):
            return report(IdentityStatus.MISMATCH, "fresh process is not the intended executable")
        if observed_app.creation_time + 1e-3 >= expected.launched_after:
            return report(IdentityStatus.VERIFIED)
        notes.append("process predates the requested launch window")
        return report(IdentityStatus.MISMATCH)

    if expected.mode == "exe_hash":
        if not expected.expect_sha256:
            return report(IdentityStatus.UNVERIFIABLE, "exe_hash requires the expected artifact hash")
        if observed_app is None:
            notes.append("the executable could not be inspected")
            return report(IdentityStatus.UNVERIFIABLE)
        digest = sha256_file(observed_app.executable_path)
        observed["sha256"] = digest
        if digest is None:
            notes.append("executable could not be read for hashing")
            return report(IdentityStatus.UNVERIFIABLE)
        if expected.expect_sha256 and digest.lower() != expected.expect_sha256.lower():
            notes.append("executable hash does not match the expected artifact")
            return report(IdentityStatus.MISMATCH)
        if expected.expect_exe and os.path.normcase(os.path.abspath(observed_app.executable_path)) != os.path.normcase(
            os.path.abspath(expected.expect_exe)
        ):
            notes.append("executable path does not match the expected artifact")
            return report(IdentityStatus.MISMATCH)
        return report(IdentityStatus.VERIFIED)

    if expected.mode == "package_family":
        family = win32.process_package_family(pid)
        observed["package_family"] = family
        if not expected.expect_package or observed_app is None or family is None:
            return report(IdentityStatus.UNVERIFIABLE, "the bound window process has no verifiable package identity")
        if family != expected.expect_package:
            return report(IdentityStatus.MISMATCH, "bound process belongs to another package family")
        if expected.launched_after is not None and observed_app.creation_time < expected.launched_after:
            return report(IdentityStatus.MISMATCH, "bound process predates the launch")
        if not expected.expect_sha256:
            return report(IdentityStatus.UNVERIFIABLE, "package family alone is not a build; supply expect_sha256")
        observed["sha256"] = sha256_file(observed_app.executable_path)
        if observed["sha256"] is None:
            return report(IdentityStatus.UNVERIFIABLE, "package executable could not be hashed")
        return report(
            IdentityStatus.VERIFIED
            if str(observed["sha256"]).lower() == expected.expect_sha256.lower()
            else IdentityStatus.MISMATCH
        )

    if expected.mode == "file_marker":
        value, note = _read_marker(expected.marker_path)
        if note:
            notes.append(note)
        observed["marker_path"] = expected.marker_path
        observed["marker_value"] = value
        if value is None:
            return report(IdentityStatus.UNVERIFIABLE)
        if expected.expect_marker is None:
            notes.append("no expected marker value supplied")
            return report(IdentityStatus.UNVERIFIABLE)
        if value != expected.expect_marker:
            notes.append("running build marker does not match the expected build")
            return report(IdentityStatus.MISMATCH)
        try:
            with open(expected.marker_path or "", encoding="utf-8") as handle:
                marker = json.load(handle)
            bound = (
                isinstance(marker, dict)
                and marker.get("pid") == pid
                and observed_app is not None
                and float(marker.get("started_at", 0)) >= observed_app.creation_time
            )
        except (OSError, ValueError, TypeError):
            bound = False
        if not bound:
            return report(IdentityStatus.UNVERIFIABLE, "marker is not bound to the observed process instance")
        return report(IdentityStatus.VERIFIED)

    notes.append(f"unknown identity mode {expected.mode!r}")
    return report(IdentityStatus.UNVERIFIABLE)
