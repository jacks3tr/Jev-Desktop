"""Deterministic doubles for engine tests: no desktop, no network, no wall-clock waits."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jev_desktop.contracts import (
    AppRef,
    Capture,
    Coverage,
    Decision,
    DispatchMechanism,
    DispatchState,
    DriverError,
    ElementInfo,
    EvidenceRef,
    ExpectedIdentity,
    Geometry,
    IdentityReport,
    IdentityStatus,
    InputMode,
    Operation,
    Pause,
    Reason,
    Receipt,
    Rect,
    ScopeSpec,
    Snapshot,
    TargetCandidate,
    UncertainEffect,
    WindowInfo,
    new_id,
    now,
)
from jev_desktop.policy import OpContext


class Clock:
    """Manually advanced clock so budget tests are deterministic."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds

    def sleep(self, seconds: float) -> None:
        self.value += max(seconds, 0.0)


@dataclass
class FakeElement:
    role: str
    name: str
    value: str | None = None
    enabled: bool = True
    visible: bool = True
    editable: bool = False
    operations: tuple[str, ...] = ("CLICK",)
    state: dict[str, Any] = field(default_factory=dict)
    text: str | None = None


@dataclass
class FakeApp:
    """A tiny mutable window model the fake driver observes and mutates."""

    app_ref: str
    window_ref: str
    title: str = "Fake App"
    elements: list[FakeElement] = field(default_factory=list)
    status: str = "ready"
    dialog_open: bool = False
    modal: bool = False

    def fingerprint(self) -> str:
        payload = "|".join(
            [self.title, self.status, str(self.dialog_open), str(self.modal)]
            + [
                f"{element.role}:{element.name}:{element.value}:{element.enabled}:{element.visible}"
                for element in self.elements
            ]
        )
        return hashlib.blake2s(payload.encode("utf-8"), digest_size=8).hexdigest()

    def find(self, name: str) -> FakeElement | None:
        return next((element for element in self.elements if element.name == name), None)


class FakeDriver:
    """In-memory driver honouring the frozen Driver protocol."""

    def __init__(self, app: FakeApp, *, evidence_dir: Path) -> None:
        self.app = app
        self.evidence_dir = Path(evidence_dir)
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.executed: list[Any] = []  # ActionRequest objects that reached the native boundary
        self.observations = 0
        self.started = False
        self.emergency = False
        self.fail_next: str | None = None  # "uncertain" | "before" | "pause:<reason>"
        self.identity_status = IdentityStatus.VERIFIED
        self.identity_checked = 0
        self.observe_failures = 0
        self.capture_calls = 0
        self.on_execute: Callable[[Any], None] | None = None
        self.lease_generation = 1

    # -- lifecycle -----------------------------------------------------------------

    def start(self) -> None:
        self.started = True

    def close(self) -> None:
        self.started = False

    def emergency_stop(self) -> None:
        self.emergency = True

    def health(self) -> Mapping[str, Any]:
        return {"started": self.started, "fake": True, "observations": self.observations}

    # -- observation ---------------------------------------------------------------

    def list_apps(self) -> list[AppRef]:
        return [
            AppRef(
                app_ref=self.app.app_ref,
                package_identity=None,
                executable_path="C:/fake/app.exe",
                process_id=4242,
                process_creation_time=1.0,
                window_refs=(self.app.window_ref,),
            )
        ]

    def _window(self) -> WindowInfo:
        return WindowInfo(
            window_ref=self.app.window_ref,
            app_ref=self.app.app_ref,
            title=self.app.title,
            class_name="FakeWindow",
            process_id=4242,
            modal=self.app.modal,
            owner_window_ref=None,
            focused=True,
            visible=True,
            enabled=not self.app.modal,
            rect=Rect(100, 100, 700, 500),
            scope="scoped",
        )

    def list_windows(self) -> list[WindowInfo]:
        return [self._window()]

    def observe(self, scope: ScopeSpec) -> Snapshot:
        if self.fail_next == "observe":
            self.fail_next = None
            raise DriverError("observation failed (injected)")
        self.observations += 1
        elements: list[ElementInfo] = []
        for index, element in enumerate(self.app.elements, start=1):
            value = element.value
            if element.role == "edit" and element.value is None:
                value = ""
            elements.append(
                ElementInfo(
                    element_id=new_id("el"),
                    window_ref=self.app.window_ref,
                    role=element.role,
                    name=element.name,
                    value=value,
                    enabled=element.enabled,
                    visible=element.visible,
                    editable=element.editable,
                    focusable=True,
                    focused=False,
                    operations=element.operations,
                    rect=Rect(120, 100 + index * 40, 320, 120 + index * 40),
                    index=index,
                    path=(self.app.title,),
                    state=dict(element.state),
                    text=element.text if element.text is not None else (element.value or element.name),
                    truncation=None,
                )
            )
        texts = [
            {"element_id": element.element_id, "role": element.role, "name": element.name, "text": element.text}
            for element in elements
            if element.role in {"text", "statusbar", "edit"} and element.text
        ]
        return Snapshot(
            snapshot_id=new_id("snap"),
            app_ref=self.app.app_ref,
            captured_at=now(),
            interval_ms=1,
            geometry=self.geometry(),
            fingerprint=self.app.fingerprint(),
            coverage=Coverage.COMPLETE,
            truncation=(),
            windows=(self._window(),),
            elements=tuple(elements),
            context={"texts": texts, "focused_element_id": None, "modal_windows": [], "foreground_window_ref": None},
        )

    def geometry(self) -> Geometry:
        return Geometry(
            epoch=1,
            virtual_left=0,
            virtual_top=0,
            virtual_width=1920,
            virtual_height=1080,
            primary_dpi=96,
            primary_scale=1.0,
        )

    def capture(
        self, *, scope=None, snapshot_id=None, region=None, max_scale=1.0, run_id, checkpoint=None, description=""
    ) -> Capture:
        self.capture_calls += 1
        directory = self.evidence_dir / run_id.replace(":", "_")
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{checkpoint or 'capture'}-{self.capture_calls}.png"
        data = b"\x89PNG\r\n\x1a\n" + b"fake-image"
        path.write_bytes(data)
        reference = EvidenceRef(
            evidence_id=new_id("ev"),
            run_id=run_id,
            kind="screenshot",
            path=str(path),
            media_type="image/png",
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
            created_at=now(),
            checkpoint=checkpoint,
            snapshot_id=snapshot_id,
            geometry=self.geometry(),
            source_rect=Rect(100, 100, 700, 500),
            scale=max_scale,
            description=description,
        )
        return Capture(
            evidence=reference, geometry=self.geometry(), source_rect=Rect(100, 100, 700, 500), scale=max_scale
        )

    def identity(self, app_ref: str, expected: ExpectedIdentity) -> IdentityReport:
        self.identity_checked += 1
        return IdentityReport(
            app_ref=app_ref,
            status=self.identity_status,
            expected=expected.to_json(),
            observed={"process_id": 4242, "executable_path": "C:/fake/app.exe"},
            evidence_refs=(),
            notes=(),
            checked_at=now(),
        )

    # -- execution -----------------------------------------------------------------

    def execute(self, request, guard: Callable[[], None], snapshot: Snapshot | None) -> Receipt:
        if self.emergency:
            raise DriverError("fake driver emergency stop")
        if self.fail_next == "before":
            self.fail_next = None
            raise DriverError("injected pre-dispatch failure")
        guard()
        self.executed.append(request)
        if self.fail_next == "uncertain":
            self.fail_next = None
            raise UncertainEffect("injected partial dispatch", mechanism=DispatchMechanism.SEND_INPUT_MOUSE)
        if self.fail_next and self.fail_next.startswith("pause:"):
            reason = self.fail_next.split(":", 1)[1]
            self.fail_next = None
            raise Pause(reason, {"injected": True})
        if self.on_execute is not None:
            self.on_execute(request)
        self._apply(request, snapshot)
        mechanism = (
            DispatchMechanism.UIA_PATTERN if request.mode is InputMode.SEMANTIC else DispatchMechanism.SEND_INPUT_MOUSE
        )
        return Receipt(
            action_id=request.action_id,
            dispatch_state=DispatchState.DISPATCHED,
            mechanism=mechanism,
            inserted_events=2,
            started_at=time.time(),
            finished_at=time.time(),
            target={"element_id": request.element_id},
            notes=("fake dispatch",),
        )

    def _apply(self, request, snapshot: Snapshot | None) -> None:
        if request.operation is Operation.FOCUS_WINDOW:
            return
        element = self._element_for(request, snapshot)
        if element is None:
            return
        if request.operation is Operation.CLICK:
            if element.name == "Save":
                self.app.status = "saved"
                saved = self.app.find("Saved")
                if saved is not None:
                    saved.value = "yes"
                    saved.text = "yes"
            elif element.name == "Open dialog":
                self.app.dialog_open = True
                self.app.status = "dialog open"
        elif request.operation is Operation.TYPE_TEXT:
            element.value = request.text or ""
            element.text = element.value
        elif request.operation is Operation.TOGGLE:
            checked = element.state.get("checked", "off")
            element.state["checked"] = "on" if checked == "off" else "off"
        elif request.operation is Operation.SELECT:
            element.value = request.option_label or element.value

    def _element_for(self, request, snapshot: Snapshot | None) -> FakeElement | None:
        if snapshot is None or request.element_id is None:
            return None
        try:
            observed = snapshot.element(request.element_id)
        except Exception:
            return None
        return self.app.find(observed.name) or next(
            (item for item in self.app.elements if item.role == observed.role), None
        )


@dataclass
class ScriptedDecision:
    operation: Operation
    target_name: str | None = None
    operation_confidence: float = 0.9
    target_confidence: float = 0.9


class ScriptedPolicy:
    """Returns queued decisions, resolving target names against the offered candidates."""

    def __init__(self, script: Sequence[ScriptedDecision]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []
        self.resolved_models = ["scripted-1"]

    def decide(
        self,
        *,
        goal: str,
        state: Mapping[str, Any],
        contexts: Sequence[OpContext],
        allow_done: bool,
        allow_escalate: bool = True,
        current_step: Mapping[str, Any] | None = None,
    ) -> Decision:
        self.calls.append({"goal": goal, "state": state, "contexts": contexts, "allow_done": allow_done})
        if not self.script:
            raise Pause(Reason.STEP_UNRESOLVED, {"detail": "scripted policy exhausted"})
        entry = self.script.pop(0)
        candidate: TargetCandidate | None = None
        if entry.target_name is not None:
            for context in contexts:
                for item in context.candidates:
                    if (
                        f'name="{entry.target_name}"' in item.description
                        or f"name='{entry.target_name}'" in item.description
                    ):
                        candidate = item
                        break
                if candidate is not None:
                    break
            if candidate is None:
                raise Pause(Reason.NO_APPROPRIATE_TARGET, {"target": entry.target_name})
        return Decision(
            operation=entry.operation,
            target=candidate,
            operation_confidence=entry.operation_confidence,
            target_confidence=entry.target_confidence if candidate else None,
            model="scripted-1",
            usage={"input_tokens": 1, "output_tokens": 1},
            latency_ms=1,
            request_digest="scripted",
            state_digest="scripted",
        )


class StubTransport:
    """TypeSafe transport double: replays canned responses and records requests."""

    def __init__(self, responses: Sequence[tuple[int, Any]]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def post_json(self, url: str, *, headers: Mapping[str, str], payload: Mapping[str, Any], timeout_s: float):
        self.requests.append({"url": url, "headers": dict(headers), "payload": payload, "timeout_s": timeout_s})
        if not self.responses:
            raise AssertionError("stub transport exhausted")
        return self.responses.pop(0)


def choice_answer(selected: str, options: Sequence[str], *, confidence: float = 0.9) -> dict:
    """Build a well-formed Choice answer: the selected option wins the distribution."""
    others = [option for option in options if option != selected]
    share = (1.0 - confidence) / len(others) if others else 0.0
    probabilities = {selected: confidence, **dict.fromkeys(others, share)}
    return {"type": "choice", "choice": selected, "probabilities": probabilities, "confidence": confidence}


def fake_response(model: str, answers: Mapping[str, Any], usage: Mapping[str, int] | None = None) -> dict[str, Any]:
    return {"model": model, "answers": dict(answers), "usage": dict(usage or {"input_tokens": 10, "output_tokens": 5})}
