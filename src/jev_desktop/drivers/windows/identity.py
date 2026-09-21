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
import time
from dataclasses import dataclass
from typing import Any

from ...contracts import DriverError, ExpectedIdentity, IdentityReport, IdentityStatus, now
from . import win32

_HASH_CACHE: dict[tuple[str, int, int], str] = {}


def sha256_file(path: str) -> str | None:
    try:
        stat = os.stat(path)
        key = (os.path.normcase(os.path.abspath(path)), stat.st_size, int(stat.st_mtime_ns))
    except OSError:
        return None
    cached = _HASH_CACHE.get(key)
    if cached is not None:
        return cached
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError:
        return None
    value = digest.hexdigest()
    _HASH_CACHE.clear()  # bounded: only the most recent file matters for identity work
    _HASH_CACHE[key] = value
    return value


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


def _package_instances(family: str) -> list[dict[str, Any]]:
    """Processes belonging to a package family, with their creation times."""
    instances: list[dict[str, Any]] = []
    for candidate in win32.iter_process_ids():
        if win32.process_package_family(candidate) != family:
            continue
        try:
            created = win32.process_creation_time(candidate)
        except DriverError:
            continue
        instances.append({"process_id": candidate, "family": family, "process_creation_time": created})
    return instances


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

    if expected.mode == "fresh_launch":
        if expected.launched_after is None:
            notes.append("fresh_launch requires launched_after")
            return report(IdentityStatus.UNVERIFIABLE)
        observed["launched_after"] = expected.launched_after
        if observed_app is None:
            return report(IdentityStatus.UNVERIFIABLE)
        if observed_app.creation_time + 1e-3 >= expected.launched_after:
            return report(IdentityStatus.VERIFIED)
        notes.append("process predates the requested launch window")
        return report(IdentityStatus.MISMATCH)

    if expected.mode == "exe_hash":
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
        # Packaged applications hand their window to a frame host, whose process predates the
        # launch, so neither exe_hash nor fresh_launch can verify them. The package family name
        # plus a launch timestamp identifies the instance that was actually started.
        if not expected.expect_package:
            notes.append("package_family requires expect_package")
            return report(IdentityStatus.UNVERIFIABLE)
        matches: list[dict[str, Any]] = []
        try:
            # A packaged application can start its process a moment after its window appears,
            # so give it a short, bounded window rather than reporting a mismatch instantly.
            deadline = now() + 3.0
            while True:
                matches = _package_instances(expected.expect_package)
                if matches or now() >= deadline:
                    break
                time.sleep(0.25)
        except DriverError as exc:
            notes.append(f"process enumeration failed: {exc}")
            return report(IdentityStatus.UNVERIFIABLE)
        observed["package_family"] = expected.expect_package
        observed["instances"] = matches
        if not matches:
            notes.append("no running process belongs to the expected package family")
            return report(IdentityStatus.MISMATCH)
        if expected.launched_after is not None:
            fresh = [item for item in matches if item["process_creation_time"] + 1e-3 >= expected.launched_after]
            observed["instances_after_launch"] = fresh
            if not fresh:
                notes.append("running instances predate the requested launch window")
                return report(IdentityStatus.MISMATCH)
        return report(IdentityStatus.VERIFIED)

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
        return report(IdentityStatus.VERIFIED)

    notes.append(f"unknown identity mode {expected.mode!r}")
    return report(IdentityStatus.UNVERIFIABLE)
