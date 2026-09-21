"""Support for the three optional real-Notepad acceptance cases."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

from jev_desktop.contracts import RunSpec, ScopeSpec
from jev_desktop.drivers.windows import WindowsDriver
from jev_desktop.evidence import EvidenceStore
from jev_desktop.journal import DispatchJournal
from jev_desktop.ownership import Ownership
from jev_desktop.policy import JevPolicy
from jev_desktop.runtime import Runtime, RuntimeConfig


class RealApp:
    """Own only the Notepad process launched by this test. Never reuse a user's window."""

    def __init__(self, driver: WindowsDriver, application: str, *, args: list[str] | None = None) -> None:
        if application != "notepad":
            raise ValueError("acceptance tests only launch Notepad")
        self.executable = r"C:\Windows\System32\notepad.exe"
        self.process = subprocess.Popen([self.executable, *(args or [])], close_fds=True)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            for app in driver.list_apps():
                if app.process_id == self.process.pid and app.window_refs:
                    self.app_ref = app.app_ref
                    self.scope = ScopeSpec(app_ref=self.app_ref, max_elements=240)
                    return
            if self.process.poll() is not None:
                break
            time.sleep(0.1)
        self.close()
        raise RuntimeError("Notepad did not expose a window in the launched process; process handoff is unsupported")

    def close(self) -> None:
        # Test-owned unsaved documents must not create Save confirmation popups at teardown.
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)


def run_spec(
    driver: WindowsDriver,
    policy: JevPolicy,
    spec: RunSpec,
    workdir: Path,
    *,
    slice_seconds: float = 90.0,
    approved_roots: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    journal = DispatchJournal(str(workdir / "journal.sqlite"))
    ownership = Ownership()
    roots = approved_roots or (str(workdir),)
    evidence = EvidenceStore(root=workdir / "evidence", approved_roots=list(roots))
    runtime = Runtime(
        driver=driver,
        journal=journal,
        ownership=ownership,
        evidence=evidence,
        config=RuntimeConfig(evidence_dir=workdir / "evidence", approved_roots=roots),
        policy=policy,
    )
    try:
        session = ownership.create_session("real-app-calibration")
        created = runtime.create_run(spec, session_id=session.session_id)
        ownership.acquire(session.session_id, created["run_id"])
        started = time.perf_counter()
        result = runtime.slice(
            run_id=created["run_id"],
            session_id=session.session_id,
            resume_token=created["resume_token"],
            slice_seconds=slice_seconds,
        )
        wall = time.perf_counter() - started
        return {
            "run_id": created["run_id"],
            "execution": result.execution.value,
            "verdict": result.verdict.value,
            "reason": result.reason,
            "wall_seconds": round(wall, 2),
            "actions": result.budgets["actions"],
            "model_decisions": result.budgets["decisions"],
            "steps": [
                {
                    "step_id": step.step_id,
                    "operation": step.operation.value,
                    "dispatch_state": step.dispatch_state.value,
                    "target": step.target_description,
                    "changed": step.observation_changed,
                    "error": step.error,
                }
                for step in result.steps
            ],
            "assertions": [
                {
                    "id": assertion.assertion_id,
                    "status": assertion.status.value,
                    "observed": dict(assertion.observed),
                    "notes": list(assertion.notes),
                }
                for assertion in result.assertions
            ],
            "evidence_bytes": sum(reference.size_bytes for reference in result.evidence),
            "detail": dict(result.detail),
        }
    finally:
        ownership.force_release()
        journal.close()
