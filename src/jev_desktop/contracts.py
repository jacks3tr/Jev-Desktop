"""Frozen cross-boundary contracts: identifiers, statuses, schemas, driver surface.

Everything that crosses a module, process, or transport boundary is defined here and
nothing else in this package may widen it. Serialization is explicit: every wire type
has `to_json`/`from_json` with validation, so a malformed payload fails at the edge
instead of corrupting engine state.

Adapted question/answer material from browser-use/jev-ultrafast lives in `policy.py`;
this module is original. See NOTICE.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields
from enum import Enum, StrEnum
from typing import Any, Protocol

SCHEMA_VERSION = "1"

# --------------------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------------------


class _StrEnum(StrEnum):
    """String-valued enums, so serialisation never needs a converter."""


class Execution(_StrEnum):
    COMPLETED = "completed"
    PAUSED = "paused"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    ERROR = "error"


class Verdict(_StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"


class Reason(_StrEnum):
    NEEDS_TEXT = "needs_text"
    NEEDS_VISUAL_ASSISTANCE = "needs_visual_assistance"
    LOW_CONFIDENCE = "low_confidence"
    STALE_OBSERVATION = "stale_observation"
    UNSUPPORTED_CONTROL = "unsupported_control"
    PERMISSION_BOUNDARY = "permission_boundary"
    USER_TAKEOVER = "user_takeover"
    BUDGET_EXHAUSTED = "budget_exhausted"
    UNCERTAIN_EFFECT = "uncertain_effect"
    INCORRECT_BUILD = "incorrect_build"
    NO_APPROPRIATE_TARGET = "no_appropriate_target"
    INVALID_MODEL_RESPONSE = "invalid_model_response"
    UNEXPECTED_MODEL_VERSION = "unexpected_model_version"
    NEEDS_NARROWER_OBSERVATION = "needs_narrower_observation"
    STEP_UNRESOLVED = "step_unresolved"
    MISSING_FIXTURE = "missing_fixture"
    ASSERTION_FAILED = "assertion_failed"


class Operation(_StrEnum):
    CLICK = "CLICK"
    TYPE_TEXT = "TYPE_TEXT"
    SELECT = "SELECT"
    TOGGLE = "TOGGLE"
    SCROLL = "SCROLL"
    FOCUS_WINDOW = "FOCUS_WINDOW"
    LAUNCH_APP = "LAUNCH_APP"
    HOTKEY = "HOTKEY"
    WAIT = "WAIT"
    DONE = "DONE"
    ESCALATE = "ESCALATE"

    @classmethod
    def _missing_(cls, value: object) -> Operation | None:
        if isinstance(value, str):
            upper = value.upper()
            for member in cls:
                if member.value == upper:
                    return member
        return None


TARGET_OPERATIONS: tuple[Operation, ...] = (
    Operation.CLICK,
    Operation.TYPE_TEXT,
    Operation.SELECT,
    Operation.TOGGLE,
    Operation.SCROLL,
    Operation.FOCUS_WINDOW,
)
CONTROL_OPERATIONS: tuple[Operation, ...] = (
    Operation.HOTKEY,
    Operation.LAUNCH_APP,
)
ZERO_TARGET_OPERATIONS: tuple[Operation, ...] = (
    Operation.WAIT,
    Operation.DONE,
    Operation.ESCALATE,
)


class InputMode(_StrEnum):
    USER_PATH = "user_path"
    SEMANTIC = "semantic"


class Purpose(_StrEnum):
    EXPLORATORY = "exploratory"
    REGRESSION = "regression"


class Coverage(_StrEnum):
    COMPLETE = "complete"
    TRUNCATED = "truncated"
    PARTIAL = "partial"


class DispatchState(_StrEnum):
    DISPATCHING = "dispatching"
    DISPATCHED = "dispatched"
    NOT_DISPATCHED = "not_dispatched"
    UNCERTAIN = "uncertain"


class DispatchMechanism(_StrEnum):
    SEND_INPUT_MOUSE = "send_input_mouse"
    SEND_INPUT_KEYBOARD = "send_input_keyboard"
    UIA_PATTERN = "uia_pattern"
    NONE = "none"


class AssertionStatus(_StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"
    NOT_EVALUATED = "not_evaluated"


class Evaluator(_StrEnum):
    UIA_PROPERTY = "uia_property"
    UIA_PRESENCE = "uia_presence"
    UIA_ABSENCE = "uia_absence"
    WINDOW_STATE = "window_state"
    ARTIFACT = "artifact"
    PROCESS_IDENTITY = "process_identity"
    MODEL_VISUAL = "model_visual"
    CALLER_RESULT = "caller_result"


class IdentityStatus(_StrEnum):
    VERIFIED = "verified"
    MISMATCH = "mismatch"
    UNVERIFIABLE = "unverifiable"


class RunStatus(_StrEnum):
    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    ERROR = "error"


# --------------------------------------------------------------------------------------
# Exceptions
# --------------------------------------------------------------------------------------


class ContractError(ValueError):
    """A payload or identifier violated the frozen schema."""


class AuthorizationError(ContractError):
    """Identifiers were well-formed but not authorized for the caller."""


class PolicyError(ContractError):
    """The policy request itself is invalid (bad candidate set, size, or model id)."""


class DriverError(RuntimeError):
    """Native observation or execution failed before the dispatch boundary."""


class UncertainEffect(DriverError):
    """Failure occurred after entering the dispatch boundary: outcome unknown."""

    def __init__(self, message: str, *, mechanism: DispatchMechanism = DispatchMechanism.NONE) -> None:
        super().__init__(message)
        self.mechanism = mechanism


class EmergencyStop(RuntimeError):
    """Local emergency stop is set; no further input may be issued."""


class Pause(RuntimeError):
    """Return control to the caller without issuing further desktop input."""

    def __init__(self, reason: Reason | str, detail: Mapping[str, Any] | None = None) -> None:
        self.reason = Reason(str(reason)) if str(reason) in set(Reason) else str(reason)
        self.detail: dict[str, Any] = dict(detail or {})
        super().__init__(f"paused:{self.reason}")

    @property
    def reason_value(self) -> str:
        return self.reason.value if isinstance(self.reason, Reason) else str(self.reason)


# --------------------------------------------------------------------------------------
# Identifiers, digests, time
# --------------------------------------------------------------------------------------

_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,23}:[0-9a-f]{8,64}$")
_PREFIXES = {
    "app",
    "win",
    "snap",
    "el",
    "ev",
    "act",
    "req",
    "sess",
    "run",
    "lease",
    "resume",
    "oracle",
    "cfg",
}


def new_id(prefix: str) -> str:
    if prefix not in _PREFIXES:
        raise ContractError(f"unknown id prefix: {prefix}")
    return f"{prefix}:{secrets.token_hex(12)}"


def validate_id(kind: str, value: Any) -> str:
    if kind not in _PREFIXES:
        raise ContractError(f"unknown id prefix: {kind}")
    if not isinstance(value, str) or not _ID_RE.match(value) or not value.startswith(f"{kind}:"):
        raise ContractError(f"malformed {kind} identifier")
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def keyed_fingerprint(secret: bytes, value: str) -> str:
    """Keyed, non-reversible fingerprint for fixture/secret values in journals."""
    return hashlib.blake2s(value.encode("utf-8"), key=secret[:32], digest_size=16).hexdigest()


def now() -> float:
    return time.time()


def _require_mapping(value: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{what} must be an object")
    return value


def _require_str(value: Any, what: str, *, allow_empty: bool = True) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ContractError(f"{what} must be a string")
    return value


def _opt_str(value: Any, what: str) -> str | None:
    if value is None:
        return None
    return _require_str(value, what)


def _require_bool(value: Any, what: str) -> bool:
    if not isinstance(value, bool):
        raise ContractError(f"{what} must be a boolean")
    return value


def _require_int(value: Any, what: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"{what} must be an integer")
    if minimum is not None and value < minimum:
        raise ContractError(f"{what} must be >= {minimum}")
    return value


def _require_number(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ContractError(f"{what} must be a finite number")
    return float(value)


def _require_list(value: Any, what: str) -> list[Any]:
    if not isinstance(value, list):
        raise ContractError(f"{what} must be an array")
    return value


def _require_str_list(value: Any, what: str) -> list[str]:
    return [_require_str(item, f"{what}[]", allow_empty=False) for item in _require_list(value, what)]


def _require_enum(enum_cls: type[Enum], value: Any, what: str) -> Any:
    try:
        return enum_cls(value)
    except ValueError as exc:
        raise ContractError(f"{what}: unknown value {value!r}") from exc


@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    def to_json(self) -> dict[str, int]:
        return {"left": self.left, "top": self.top, "right": self.right, "bottom": self.bottom}

    @classmethod
    def from_json(cls, data: Any) -> Rect:
        data = _require_mapping(data, "rect")
        return cls(
            left=_require_int(data.get("left"), "rect.left"),
            top=_require_int(data.get("top"), "rect.top"),
            right=_require_int(data.get("right"), "rect.right"),
            bottom=_require_int(data.get("bottom"), "rect.bottom"),
        )

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def is_empty(self) -> bool:
        return self.width <= 0 or self.height <= 0

    def center(self) -> tuple[int, int]:
        return (self.left + self.right) // 2, (self.top + self.bottom) // 2

    def contains(self, x: int, y: int) -> bool:
        return self.left <= x < self.right and self.top <= y < self.bottom

    def intersect(self, other: Rect) -> Rect:
        return Rect(
            max(self.left, other.left),
            max(self.top, other.top),
            min(self.right, other.right),
            min(self.bottom, other.bottom),
        )


@dataclass(frozen=True)
class Geometry:
    """Display/geometry epoch for coordinate-bearing evidence."""

    epoch: int
    virtual_left: int
    virtual_top: int
    virtual_width: int
    virtual_height: int
    primary_dpi: int
    primary_scale: float

    def to_json(self) -> dict[str, Any]:
        return {
            "epoch": self.epoch,
            "virtual_left": self.virtual_left,
            "virtual_top": self.virtual_top,
            "virtual_width": self.virtual_width,
            "virtual_height": self.virtual_height,
            "primary_dpi": self.primary_dpi,
            "primary_scale": self.primary_scale,
        }

    @classmethod
    def from_json(cls, data: Any) -> Geometry:
        data = _require_mapping(data, "geometry")
        return cls(
            epoch=_require_int(data.get("epoch"), "geometry.epoch"),
            virtual_left=_require_int(data.get("virtual_left"), "geometry.virtual_left"),
            virtual_top=_require_int(data.get("virtual_top"), "geometry.virtual_top"),
            virtual_width=_require_int(data.get("virtual_width"), "geometry.virtual_width", minimum=1),
            virtual_height=_require_int(data.get("virtual_height"), "geometry.virtual_height", minimum=1),
            primary_dpi=_require_int(data.get("primary_dpi"), "geometry.primary_dpi", minimum=48),
            primary_scale=_require_number(data.get("primary_scale"), "geometry.primary_scale"),
        )


# --------------------------------------------------------------------------------------
# Application / window / element observations
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class AppRef:
    app_ref: str
    package_identity: str | None
    executable_path: str
    process_id: int
    process_creation_time: float
    window_refs: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "app_ref": self.app_ref,
            "package_identity": self.package_identity,
            "executable_path": self.executable_path,
            "process_id": self.process_id,
            "process_creation_time": self.process_creation_time,
            "window_refs": list(self.window_refs),
        }

    @classmethod
    def from_json(cls, data: Any) -> AppRef:
        data = _require_mapping(data, "app_ref")
        return cls(
            app_ref=validate_id("app", data.get("app_ref")),
            package_identity=_opt_str(data.get("package_identity"), "app_ref.package_identity"),
            executable_path=_require_str(data.get("executable_path"), "app_ref.executable_path"),
            process_id=_require_int(data.get("process_id"), "app_ref.process_id", minimum=1),
            process_creation_time=_require_number(data.get("process_creation_time"), "app_ref.process_creation_time"),
            window_refs=tuple(
                validate_id("win", ref) for ref in _require_str_list(data.get("window_refs", []), "window_refs")
            ),
        )


@dataclass(frozen=True)
class WindowInfo:
    window_ref: str
    app_ref: str
    title: str
    class_name: str
    process_id: int
    modal: bool
    owner_window_ref: str | None
    focused: bool
    visible: bool
    enabled: bool
    rect: Rect
    scope: str  # "scoped" | "dialog" | "unrelated"

    def to_json(self) -> dict[str, Any]:
        return {
            "window_ref": self.window_ref,
            "app_ref": self.app_ref,
            "title": self.title,
            "class_name": self.class_name,
            "process_id": self.process_id,
            "modal": self.modal,
            "owner_window_ref": self.owner_window_ref,
            "focused": self.focused,
            "visible": self.visible,
            "enabled": self.enabled,
            "rect": self.rect.to_json(),
            "scope": self.scope,
        }

    @classmethod
    def from_json(cls, data: Any) -> WindowInfo:
        data = _require_mapping(data, "window")
        return cls(
            window_ref=validate_id("win", data.get("window_ref")),
            app_ref=validate_id("app", data.get("app_ref")),
            title=_require_str(data.get("title"), "window.title"),
            class_name=_require_str(data.get("class_name"), "window.class_name"),
            process_id=_require_int(data.get("process_id"), "window.process_id", minimum=1),
            modal=_require_bool(data.get("modal"), "window.modal"),
            owner_window_ref=(
                None if data.get("owner_window_ref") is None else validate_id("win", data.get("owner_window_ref"))
            ),
            focused=_require_bool(data.get("focused"), "window.focused"),
            visible=_require_bool(data.get("visible"), "window.visible"),
            enabled=_require_bool(data.get("enabled"), "window.enabled"),
            rect=Rect.from_json(data.get("rect")),
            scope=_require_str(data.get("scope"), "window.scope"),
        )


@dataclass(frozen=True)
class ElementInfo:
    element_id: str
    window_ref: str
    role: str
    name: str
    value: str | None
    enabled: bool
    visible: bool
    editable: bool
    focusable: bool
    focused: bool
    operations: tuple[str, ...]
    rect: Rect
    index: int
    path: tuple[str, ...] = ()
    state: Mapping[str, Any] = field(default_factory=dict)
    text: str | None = None
    truncation: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "element_id": self.element_id,
            "window_ref": self.window_ref,
            "role": self.role,
            "name": self.name,
            "value": self.value,
            "enabled": self.enabled,
            "visible": self.visible,
            "editable": self.editable,
            "focusable": self.focusable,
            "focused": self.focused,
            "operations": list(self.operations),
            "rect": self.rect.to_json(),
            "index": self.index,
            "path": list(self.path),
            "state": dict(self.state),
            "text": self.text,
            "truncation": self.truncation,
        }

    @classmethod
    def from_json(cls, data: Any) -> ElementInfo:
        data = _require_mapping(data, "element")
        state = _require_mapping(data.get("state", {}), "element.state")
        return cls(
            element_id=validate_id("el", data.get("element_id")),
            window_ref=validate_id("win", data.get("window_ref")),
            role=_require_str(data.get("role"), "element.role"),
            name=_require_str(data.get("name"), "element.name"),
            value=_opt_str(data.get("value"), "element.value"),
            enabled=_require_bool(data.get("enabled"), "element.enabled"),
            visible=_require_bool(data.get("visible"), "element.visible"),
            editable=_require_bool(data.get("editable"), "element.editable"),
            focusable=_require_bool(data.get("focusable"), "element.focusable"),
            focused=_require_bool(data.get("focused"), "element.focused"),
            operations=tuple(_require_str_list(data.get("operations", []), "element.operations")),
            rect=Rect.from_json(data.get("rect")),
            index=_require_int(data.get("index"), "element.index"),
            path=tuple(_require_str_list(data.get("path", []), "element.path")),
            state=dict(state),
            text=_opt_str(data.get("text"), "element.text"),
            truncation=_opt_str(data.get("truncation"), "element.truncation"),
        )


@dataclass(frozen=True)
class Snapshot:
    snapshot_id: str
    app_ref: str
    captured_at: float
    interval_ms: int
    geometry: Geometry
    fingerprint: str
    coverage: Coverage
    truncation: tuple[str, ...]
    windows: tuple[WindowInfo, ...]
    elements: tuple[ElementInfo, ...]
    context: Mapping[str, Any] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "app_ref": self.app_ref,
            "captured_at": self.captured_at,
            "interval_ms": self.interval_ms,
            "geometry": self.geometry.to_json(),
            "fingerprint": self.fingerprint,
            "coverage": self.coverage.value,
            "truncation": list(self.truncation),
            "windows": [w.to_json() for w in self.windows],
            "elements": [e.to_json() for e in self.elements],
            "context": dict(self.context),
            "notes": list(self.notes),
        }

    @classmethod
    def from_json(cls, data: Any) -> Snapshot:
        data = _require_mapping(data, "snapshot")
        return cls(
            snapshot_id=validate_id("snap", data.get("snapshot_id")),
            app_ref=validate_id("app", data.get("app_ref")),
            captured_at=_require_number(data.get("captured_at"), "snapshot.captured_at"),
            interval_ms=_require_int(data.get("interval_ms"), "snapshot.interval_ms", minimum=0),
            geometry=Geometry.from_json(data.get("geometry")),
            fingerprint=_require_str(data.get("fingerprint"), "snapshot.fingerprint"),
            coverage=_require_enum(Coverage, data.get("coverage"), "snapshot.coverage"),
            truncation=tuple(_require_str_list(data.get("truncation", []), "snapshot.truncation")),
            windows=tuple(WindowInfo.from_json(w) for w in _require_list(data.get("windows", []), "windows")),
            elements=tuple(ElementInfo.from_json(e) for e in _require_list(data.get("elements", []), "elements")),
            context=dict(_require_mapping(data.get("context", {}), "snapshot.context")),
            notes=tuple(_require_str_list(data.get("notes", []), "snapshot.notes")),
        )

    def element(self, element_id: str) -> ElementInfo:
        for element in self.elements:
            if element.element_id == element_id:
                return element
        raise ContractError(f"element {element_id} is not part of snapshot {self.snapshot_id}")


@dataclass(frozen=True)
class ScopeSpec:
    app_ref: str
    window_refs: tuple[str, ...] = ()
    include_dialogs: bool = True
    # Measured on a content-rich Chromium page: 240 elements produced a 61 kB state (about 15k
    # tokens) that the provider refused, while 120 elements produced 29 kB and still contained
    # every visible control. Chrome and dialogs are collected before content rows, so a smaller
    # cap costs coverage of list rows, not of anything a test can act on.
    max_elements: int = 120
    max_depth: int = 12
    text_limit: int = 4000
    include_invisible: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "app_ref": self.app_ref,
            "window_refs": list(self.window_refs),
            "include_dialogs": self.include_dialogs,
            "max_elements": self.max_elements,
            "max_depth": self.max_depth,
            "text_limit": self.text_limit,
            "include_invisible": self.include_invisible,
        }

    @classmethod
    def from_json(cls, data: Any) -> ScopeSpec:
        data = _require_mapping(data, "scope")
        return cls(
            app_ref=validate_id("app", data.get("app_ref")),
            window_refs=tuple(
                validate_id("win", ref) for ref in _require_str_list(data.get("window_refs", []), "scope.window_refs")
            ),
            include_dialogs=_require_bool(data.get("include_dialogs", True), "scope.include_dialogs"),
            max_elements=_require_int(data.get("max_elements", 120), "scope.max_elements", minimum=1),
            max_depth=_require_int(data.get("max_depth", 12), "scope.max_depth", minimum=1),
            text_limit=_require_int(data.get("text_limit", 4000), "scope.text_limit", minimum=0),
            include_invisible=_require_bool(data.get("include_invisible", False), "scope.include_invisible"),
        )


@dataclass(frozen=True)
class ExpectedIdentity:
    """How the running build is bound to the tested artifact."""

    mode: str  # "file_marker" | "exe_hash" | "fresh_launch" | "package_family" | "any"
    marker_path: str | None = None
    expect_marker: str | None = None
    expect_exe: str | None = None
    expect_sha256: str | None = None
    expect_package: str | None = None
    launched_after: float | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "marker_path": self.marker_path,
            "expect_marker": self.expect_marker,
            "expect_exe": self.expect_exe,
            "expect_sha256": self.expect_sha256,
            "expect_package": self.expect_package,
            "launched_after": self.launched_after,
        }

    @classmethod
    def from_json(cls, data: Any) -> ExpectedIdentity:
        data = _require_mapping(data, "expected_identity")
        mode = _require_str(data.get("mode", "any"), "expected_identity.mode")
        if mode not in {"file_marker", "exe_hash", "fresh_launch", "package_family", "any"}:
            raise ContractError(f"unknown identity mode: {mode}")
        return cls(
            mode=mode,
            marker_path=_opt_str(data.get("marker_path"), "expected_identity.marker_path"),
            expect_marker=_opt_str(data.get("expect_marker"), "expected_identity.expect_marker"),
            expect_exe=_opt_str(data.get("expect_exe"), "expected_identity.expect_exe"),
            expect_sha256=_opt_str(data.get("expect_sha256"), "expected_identity.expect_sha256"),
            expect_package=_opt_str(data.get("expect_package"), "expected_identity.expect_package"),
            launched_after=(
                None
                if data.get("launched_after") is None
                else _require_number(data.get("launched_after"), "expected_identity.launched_after")
            ),
        )


@dataclass(frozen=True)
class IdentityReport:
    app_ref: str
    status: IdentityStatus
    expected: Mapping[str, Any]
    observed: Mapping[str, Any]
    evidence_refs: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    checked_at: float = 0.0

    def to_json(self) -> dict[str, Any]:
        return {
            "app_ref": self.app_ref,
            "status": self.status.value,
            "expected": dict(self.expected),
            "observed": dict(self.observed),
            "evidence_refs": list(self.evidence_refs),
            "notes": list(self.notes),
            "checked_at": self.checked_at,
        }

    @classmethod
    def from_json(cls, data: Any) -> IdentityReport:
        data = _require_mapping(data, "identity_report")
        return cls(
            app_ref=validate_id("app", data.get("app_ref")),
            status=_require_enum(IdentityStatus, data.get("status"), "identity_report.status"),
            expected=dict(_require_mapping(data.get("expected", {}), "identity_report.expected")),
            observed=dict(_require_mapping(data.get("observed", {}), "identity_report.observed")),
            evidence_refs=tuple(
                validate_id("ev", ref) for ref in _require_str_list(data.get("evidence_refs", []), "evidence_refs")
            ),
            notes=tuple(_require_str_list(data.get("notes", []), "identity_report.notes")),
            checked_at=_require_number(data.get("checked_at", 0.0), "identity_report.checked_at"),
        )


# --------------------------------------------------------------------------------------
# Evidence
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceRef:
    evidence_id: str
    run_id: str
    kind: str  # "screenshot" | "artifact" | "text" | "trace"
    path: str
    media_type: str
    sha256: str
    size_bytes: int
    created_at: float
    checkpoint: str | None = None
    snapshot_id: str | None = None
    geometry: Geometry | None = None
    source_rect: Rect | None = None
    scale: float | None = None
    description: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "run_id": self.run_id,
            "kind": self.kind,
            "path": self.path,
            "media_type": self.media_type,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "created_at": self.created_at,
            "checkpoint": self.checkpoint,
            "snapshot_id": self.snapshot_id,
            "geometry": None if self.geometry is None else self.geometry.to_json(),
            "source_rect": None if self.source_rect is None else self.source_rect.to_json(),
            "scale": self.scale,
            "description": self.description,
        }

    @classmethod
    def from_json(cls, data: Any) -> EvidenceRef:
        data = _require_mapping(data, "evidence")
        snapshot_id = data.get("snapshot_id")
        return cls(
            evidence_id=validate_id("ev", data.get("evidence_id")),
            run_id=validate_id("run", data.get("run_id")),
            kind=_require_str(data.get("kind"), "evidence.kind"),
            path=_require_str(data.get("path"), "evidence.path"),
            media_type=_require_str(data.get("media_type"), "evidence.media_type"),
            sha256=_require_str(data.get("sha256"), "evidence.sha256"),
            size_bytes=_require_int(data.get("size_bytes"), "evidence.size_bytes", minimum=0),
            created_at=_require_number(data.get("created_at"), "evidence.created_at"),
            checkpoint=_opt_str(data.get("checkpoint"), "evidence.checkpoint"),
            snapshot_id=None if snapshot_id is None else validate_id("snap", snapshot_id),
            geometry=None if data.get("geometry") is None else Geometry.from_json(data.get("geometry")),
            source_rect=None if data.get("source_rect") is None else Rect.from_json(data.get("source_rect")),
            scale=None if data.get("scale") is None else _require_number(data.get("scale"), "evidence.scale"),
            description=_require_str(data.get("description", ""), "evidence.description"),
        )


# --------------------------------------------------------------------------------------
# Actions and execution
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ActionRequest:
    action_id: str
    run_id: str
    operation: Operation
    mode: InputMode
    element_id: str | None
    snapshot_id: str | None
    window_ref: str | None
    lease_generation: int
    step_id: str | None = None
    text: str | None = None
    replace_existing: bool = True
    option_label: str | None = None
    hotkey: tuple[str, ...] = ()
    scroll: Mapping[str, Any] = field(default_factory=dict)
    launch_config_id: str | None = None
    deadline_s: float = 10.0
    request_hash: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "run_id": self.run_id,
            "operation": self.operation.value,
            "mode": self.mode.value,
            "element_id": self.element_id,
            "snapshot_id": self.snapshot_id,
            "window_ref": self.window_ref,
            "lease_generation": self.lease_generation,
            "step_id": self.step_id,
            "text": self.text,
            "replace_existing": self.replace_existing,
            "option_label": self.option_label,
            "hotkey": list(self.hotkey),
            "scroll": dict(self.scroll),
            "launch_config_id": self.launch_config_id,
            "deadline_s": self.deadline_s,
            "request_hash": self.request_hash,
        }

    @classmethod
    def from_json(cls, data: Any) -> ActionRequest:
        data = _require_mapping(data, "action")
        element_id = data.get("element_id")
        snapshot_id = data.get("snapshot_id")
        window_ref = data.get("window_ref")
        return cls(
            action_id=validate_id("act", data.get("action_id")),
            run_id=validate_id("run", data.get("run_id")),
            operation=_require_enum(Operation, data.get("operation"), "action.operation"),
            mode=_require_enum(InputMode, data.get("mode"), "action.mode"),
            element_id=None if element_id is None else validate_id("el", element_id),
            snapshot_id=None if snapshot_id is None else validate_id("snap", snapshot_id),
            window_ref=None if window_ref is None else validate_id("win", window_ref),
            lease_generation=_require_int(data.get("lease_generation"), "action.lease_generation", minimum=0),
            step_id=_opt_str(data.get("step_id"), "action.step_id"),
            text=_opt_str(data.get("text"), "action.text"),
            replace_existing=_require_bool(data.get("replace_existing", True), "action.replace_existing"),
            option_label=_opt_str(data.get("option_label"), "action.option_label"),
            hotkey=tuple(_require_str_list(data.get("hotkey", []), "action.hotkey")),
            scroll=dict(_require_mapping(data.get("scroll", {}), "action.scroll")),
            launch_config_id=_opt_str(data.get("launch_config_id"), "action.launch_config_id"),
            deadline_s=_require_number(data.get("deadline_s", 10.0), "action.deadline_s"),
            request_hash=_require_str(data.get("request_hash", ""), "action.request_hash"),
        )

    def dispatch_binding(self) -> dict[str, Any]:
        """Everything the dispatch identity is bound to, excluding the action id itself."""
        return {
            "run_id": self.run_id,
            "lease_generation": self.lease_generation,
            "operation": self.operation.value,
            "mode": self.mode.value,
            "element_id": self.element_id,
            "snapshot_id": self.snapshot_id,
            "window_ref": self.window_ref,
            "step_id": self.step_id,
            "option_label": self.option_label,
            "hotkey": list(self.hotkey),
            "scroll": dict(self.scroll),
            "launch_config_id": self.launch_config_id,
        }


@dataclass(frozen=True)
class Receipt:
    action_id: str
    dispatch_state: DispatchState
    mechanism: DispatchMechanism
    inserted_events: int
    started_at: float
    finished_at: float
    target: Mapping[str, Any] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "dispatch_state": self.dispatch_state.value,
            "mechanism": self.mechanism.value,
            "inserted_events": self.inserted_events,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "target": dict(self.target),
            "notes": list(self.notes),
        }

    @classmethod
    def from_json(cls, data: Any) -> Receipt:
        data = _require_mapping(data, "receipt")
        return cls(
            action_id=validate_id("act", data.get("action_id")),
            dispatch_state=_require_enum(DispatchState, data.get("dispatch_state"), "receipt.dispatch_state"),
            mechanism=_require_enum(DispatchMechanism, data.get("mechanism"), "receipt.mechanism"),
            inserted_events=_require_int(data.get("inserted_events"), "receipt.inserted_events", minimum=0),
            started_at=_require_number(data.get("started_at"), "receipt.started_at"),
            finished_at=_require_number(data.get("finished_at"), "receipt.finished_at"),
            target=dict(_require_mapping(data.get("target", {}), "receipt.target")),
            notes=tuple(_require_str_list(data.get("notes", []), "receipt.notes")),
        )


@dataclass(frozen=True)
class Capture:
    evidence: EvidenceRef
    geometry: Geometry
    source_rect: Rect
    scale: float


# --------------------------------------------------------------------------------------
# Driver surface (implemented by drivers/windows, faked in engine tests)
# --------------------------------------------------------------------------------------


class Driver(Protocol):
    """Native desktop driver. All members run on the driver's owning thread."""

    def start(self) -> None: ...

    def close(self) -> None: ...

    def list_apps(self) -> list[AppRef]: ...

    def list_windows(self) -> list[WindowInfo]: ...

    def observe(self, scope: ScopeSpec) -> Snapshot: ...

    def capture(
        self,
        *,
        scope: ScopeSpec,
        snapshot_id: str | None,
        region: Rect | None,
        max_scale: float,
        run_id: str,
        checkpoint: str | None,
        description: str,
    ) -> Capture: ...

    def identity(self, app_ref: str, expected: ExpectedIdentity) -> IdentityReport: ...

    def execute(self, request: ActionRequest, guard: Callable[[], None], snapshot: Snapshot | None) -> Receipt: ...

    def emergency_stop(self) -> None: ...

    def health(self) -> Mapping[str, Any]: ...


# --------------------------------------------------------------------------------------
# Test specification (immutable) and limits
# --------------------------------------------------------------------------------------


# Defaults chosen from measurement rather than taste, see docs/calibration.md:
# a live run consumed 1.0 model decisions and 1.09 s of wall time per dispatched action, so a
# full 25-action budget needs about 40 decisions and 27 s of slice time.
DEFAULT_LIMITS: dict[str, Any] = {
    "max_actions": 25,
    "max_model_decisions": 40,
    "deadline_seconds": 600.0,
    "slice_seconds": 45.0,
    "stale_retries": 2,
    "no_progress_retries": 2,
}

POLICY_LIMITS: dict[str, Any] = {
    "max_actions": 200,
    "max_model_decisions": 400,
    "deadline_seconds": 3600.0,
    "slice_seconds": 120.0,
    "stale_retries": 5,
    "no_progress_retries": 5,
}


@dataclass(frozen=True)
class Limits:
    max_actions: int
    max_model_decisions: int
    deadline_seconds: float
    slice_seconds: float
    stale_retries: int
    no_progress_retries: int

    def to_json(self) -> dict[str, Any]:
        return {
            "max_actions": self.max_actions,
            "max_model_decisions": self.max_model_decisions,
            "deadline_seconds": self.deadline_seconds,
            "slice_seconds": self.slice_seconds,
            "stale_retries": self.stale_retries,
            "no_progress_retries": self.no_progress_retries,
        }

    @classmethod
    def from_json(cls, data: Any) -> Limits:
        data = _require_mapping(data, "limits")
        return cls(
            max_actions=_require_int(data.get("max_actions"), "limits.max_actions", minimum=1),
            max_model_decisions=_require_int(data.get("max_model_decisions"), "limits.max_model_decisions", minimum=1),
            deadline_seconds=_require_number(data.get("deadline_seconds"), "limits.deadline_seconds"),
            slice_seconds=_require_number(data.get("slice_seconds"), "limits.slice_seconds"),
            stale_retries=_require_int(data.get("stale_retries"), "limits.stale_retries", minimum=0),
            no_progress_retries=_require_int(data.get("no_progress_retries"), "limits.no_progress_retries", minimum=0),
        )

    def clamped(self) -> Limits:
        """Caller limits may only narrow local policy."""
        return Limits(
            max_actions=min(self.max_actions, POLICY_LIMITS["max_actions"]),
            max_model_decisions=min(self.max_model_decisions, POLICY_LIMITS["max_model_decisions"]),
            deadline_seconds=min(self.deadline_seconds, POLICY_LIMITS["deadline_seconds"]),
            slice_seconds=min(self.slice_seconds, POLICY_LIMITS["slice_seconds"]),
            stale_retries=min(self.stale_retries, POLICY_LIMITS["stale_retries"]),
            no_progress_retries=min(self.no_progress_retries, POLICY_LIMITS["no_progress_retries"]),
        )

    @classmethod
    def defaults(cls) -> Limits:
        return cls(**DEFAULT_LIMITS)


@dataclass(frozen=True)
class RequiredStep:
    step_id: str
    operation: Operation
    target_description: str
    fixture_reference: str | None = None
    depends_on: tuple[str, ...] = ()
    checkpoint: bool = False
    required: bool = True
    replace_existing: bool = True

    def to_json(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "operation": self.operation.value,
            "target_description": self.target_description,
            "fixture_reference": self.fixture_reference,
            "depends_on": list(self.depends_on),
            "checkpoint": self.checkpoint,
            "required": self.required,
            "replace_existing": self.replace_existing,
        }

    @classmethod
    def from_json(cls, data: Any) -> RequiredStep:
        data = _require_mapping(data, "step")
        return cls(
            step_id=_require_str(data.get("step_id"), "step.step_id", allow_empty=False),
            operation=_require_enum(Operation, data.get("operation"), "step.operation"),
            target_description=_require_str(data.get("target_description"), "step.target_description"),
            fixture_reference=_opt_str(data.get("fixture_reference"), "step.fixture_reference"),
            depends_on=tuple(_require_str_list(data.get("depends_on", []), "step.depends_on")),
            checkpoint=_require_bool(data.get("checkpoint", False), "step.checkpoint"),
            required=_require_bool(data.get("required", True), "step.required"),
            replace_existing=_require_bool(data.get("replace_existing", True), "step.replace_existing"),
        )


@dataclass(frozen=True)
class AssertionSpec:
    assertion_id: str
    evaluator: Evaluator
    target: Mapping[str, Any]
    expected: Mapping[str, Any]
    checkpoint: str = "any"  # step_id | "any" | "run_start" | "run_end"
    property: str = ""
    required: bool = True
    deadline_s: float = 5.0
    oracle: str | None = None  # explicitly selected visual oracle: "caller" | "provider"
    description: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "assertion_id": self.assertion_id,
            "evaluator": self.evaluator.value,
            "target": dict(self.target),
            "expected": dict(self.expected),
            "checkpoint": self.checkpoint,
            "property": self.property,
            "required": self.required,
            "deadline_s": self.deadline_s,
            "oracle": self.oracle,
            "description": self.description,
        }

    @classmethod
    def from_json(cls, data: Any) -> AssertionSpec:
        data = _require_mapping(data, "assertion")
        return cls(
            assertion_id=_require_str(data.get("assertion_id"), "assertion.assertion_id", allow_empty=False),
            evaluator=_require_enum(Evaluator, data.get("evaluator"), "assertion.evaluator"),
            target=dict(_require_mapping(data.get("target", {}), "assertion.target")),
            expected=dict(_require_mapping(data.get("expected", {}), "assertion.expected")),
            checkpoint=_require_str(data.get("checkpoint", "any"), "assertion.checkpoint"),
            property=_require_str(data.get("property", ""), "assertion.property"),
            required=_require_bool(data.get("required", True), "assertion.required"),
            deadline_s=_require_number(data.get("deadline_s", 5.0), "assertion.deadline_s"),
            oracle=_opt_str(data.get("oracle"), "assertion.oracle"),
            description=_require_str(data.get("description", ""), "assertion.description"),
        )


@dataclass(frozen=True)
class RunSpec:
    goal: str
    purpose: Purpose
    interaction_mode: InputMode
    app_ref: str
    expected_identity: ExpectedIdentity
    launch_config_id: str | None
    steps: tuple[RequiredStep, ...]
    assertions: tuple[AssertionSpec, ...]
    fixtures: Mapping[str, Any]
    secret_refs: Mapping[str, str]
    limits: Limits
    scope: ScopeSpec
    allow_restart: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "purpose": self.purpose.value,
            "interaction_mode": self.interaction_mode.value,
            "app_ref": self.app_ref,
            "expected_identity": self.expected_identity.to_json(),
            "launch_config_id": self.launch_config_id,
            "steps": [s.to_json() for s in self.steps],
            "assertions": [a.to_json() for a in self.assertions],
            "fixtures": dict(self.fixtures),
            "secret_refs": dict(self.secret_refs),
            "limits": self.limits.to_json(),
            "scope": self.scope.to_json(),
            "allow_restart": self.allow_restart,
        }

    @classmethod
    def from_json(cls, data: Any) -> RunSpec:
        data = _require_mapping(data, "spec")
        return cls(
            goal=_require_str(data.get("goal"), "spec.goal", allow_empty=False),
            purpose=_require_enum(Purpose, data.get("purpose"), "spec.purpose"),
            interaction_mode=_require_enum(InputMode, data.get("interaction_mode"), "spec.interaction_mode"),
            app_ref=validate_id("app", data.get("app_ref")),
            expected_identity=ExpectedIdentity.from_json(data.get("expected_identity", {"mode": "any"})),
            launch_config_id=_opt_str(data.get("launch_config_id"), "spec.launch_config_id"),
            steps=tuple(RequiredStep.from_json(s) for s in _require_list(data.get("steps", []), "spec.steps")),
            assertions=tuple(
                AssertionSpec.from_json(a) for a in _require_list(data.get("assertions", []), "spec.assertions")
            ),
            fixtures=dict(_require_mapping(data.get("fixtures", {}), "spec.fixtures")),
            secret_refs=dict(_require_mapping(data.get("secret_refs", {}), "spec.secret_refs")),
            limits=Limits.from_json(data.get("limits", DEFAULT_LIMITS)),
            scope=ScopeSpec.from_json(data.get("scope", {"app_ref": data.get("app_ref")})),
            allow_restart=_require_bool(data.get("allow_restart", False), "spec.allow_restart"),
        )

    def frozen_digest(self) -> str:
        return digest(self.to_json())


# --------------------------------------------------------------------------------------
# Policy decisions
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TargetCandidate:
    element_id: str
    description: str
    operation: Operation
    fixture_ref: str | None = None
    option_label: str | None = None  # for SELECT: the observed option bound to this candidate
    value: str | None = None  # for non-element targets (approved launch configuration id)

    def to_json(self) -> dict[str, Any]:
        return {
            "element_id": self.element_id,
            "description": self.description,
            "operation": self.operation.value,
            "fixture_ref": self.fixture_ref,
            "option_label": self.option_label,
            "value": self.value,
        }


@dataclass(frozen=True)
class Decision:
    operation: Operation
    target: TargetCandidate | None
    operation_confidence: float
    target_confidence: float | None
    model: str
    usage: Mapping[str, Any]
    latency_ms: int
    request_digest: str
    state_digest: str
    notes: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "operation": self.operation.value,
            "target": None if self.target is None else self.target.to_json(),
            "operation_confidence": self.operation_confidence,
            "target_confidence": self.target_confidence,
            "model": self.model,
            "usage": dict(self.usage),
            "latency_ms": self.latency_ms,
            "request_digest": self.request_digest,
            "state_digest": self.state_digest,
            "notes": list(self.notes),
        }


# --------------------------------------------------------------------------------------
# Results returned to callers
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class StepRecord:
    step_id: str | None
    operation: Operation
    target_element_id: str | None
    target_description: str
    dispatch_state: DispatchState
    receipt_action_id: str | None
    observation_changed: bool | None
    confidence: float | None
    fixture_reference: str | None
    at: float
    notes: tuple[str, ...] = ()
    error: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "operation": self.operation.value,
            "target_element_id": self.target_element_id,
            "target_description": self.target_description,
            "dispatch_state": self.dispatch_state.value,
            "receipt_action_id": self.receipt_action_id,
            "observation_changed": self.observation_changed,
            "confidence": self.confidence,
            "fixture_reference": self.fixture_reference,
            "at": self.at,
            "notes": list(self.notes),
            "error": self.error,
        }


@dataclass(frozen=True)
class AssertionResult:
    assertion_id: str
    status: AssertionStatus
    evaluator: Evaluator
    origin: str  # "application" | "runner" | "environment" | "unspecified"
    expected: Mapping[str, Any]
    observed: Mapping[str, Any]
    checkpoint: str
    at: float
    evidence_refs: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    label: str = "deterministic"  # "deterministic" | "model_assessed"

    def to_json(self) -> dict[str, Any]:
        return {
            "assertion_id": self.assertion_id,
            "status": self.status.value,
            "evaluator": self.evaluator.value,
            "origin": self.origin,
            "expected": dict(self.expected),
            "observed": dict(self.observed),
            "checkpoint": self.checkpoint,
            "at": self.at,
            "evidence_refs": list(self.evidence_refs),
            "notes": list(self.notes),
            "label": self.label,
        }


@dataclass(frozen=True)
class RunResult:
    run_id: str
    execution: Execution
    verdict: Verdict
    reason: str | None
    steps: tuple[StepRecord, ...]
    assertions: tuple[AssertionResult, ...]
    evidence: tuple[EvidenceRef, ...]
    observation: Mapping[str, Any] | None
    resume_token: str | None
    budgets: Mapping[str, Any]
    message: str = ""
    detail: Mapping[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "execution": self.execution.value,
            "verdict": self.verdict.value,
            "reason": self.reason,
            "steps": [s.to_json() for s in self.steps],
            "assertions": [a.to_json() for a in self.assertions],
            "evidence": [e.to_json() for e in self.evidence],
            "observation": None if self.observation is None else dict(self.observation),
            "resume_token": self.resume_token,
            "budgets": dict(self.budgets),
            "message": self.message,
            "detail": dict(self.detail),
        }

    @classmethod
    def from_json(cls, data: Any) -> RunResult:
        data = _require_mapping(data, "result")
        observation = data.get("observation")
        return cls(
            run_id=validate_id("run", data.get("run_id")),
            execution=_require_enum(Execution, data.get("execution"), "result.execution"),
            verdict=_require_enum(Verdict, data.get("verdict"), "result.verdict"),
            reason=_opt_str(data.get("reason"), "result.reason"),
            steps=tuple(step_record_from_json(s) for s in _require_list(data.get("steps", []), "result.steps")),
            assertions=tuple(
                assertion_result_from_json(a) for a in _require_list(data.get("assertions", []), "result.assertions")
            ),
            evidence=tuple(EvidenceRef.from_json(e) for e in _require_list(data.get("evidence", []), "evidence")),
            observation=None if observation is None else dict(_require_mapping(observation, "observation")),
            resume_token=_opt_str(data.get("resume_token"), "result.resume_token"),
            budgets=dict(_require_mapping(data.get("budgets", {}), "result.budgets")),
            message=_require_str(data.get("message", ""), "result.message"),
            detail=dict(_require_mapping(data.get("detail", {}), "result.detail")),
        )


def step_record_from_json(data: Any) -> StepRecord:
    data = _require_mapping(data, "step_record")
    return StepRecord(
        step_id=_opt_str(data.get("step_id"), "step_record.step_id"),
        operation=_require_enum(Operation, data.get("operation"), "step_record.operation"),
        target_element_id=(
            None if data.get("target_element_id") is None else validate_id("el", data.get("target_element_id"))
        ),
        target_description=_require_str(data.get("target_description", ""), "step_record.target_description"),
        dispatch_state=_require_enum(DispatchState, data.get("dispatch_state"), "step_record.dispatch_state"),
        receipt_action_id=(
            None if data.get("receipt_action_id") is None else validate_id("act", data.get("receipt_action_id"))
        ),
        observation_changed=(
            None
            if data.get("observation_changed") is None
            else _require_bool(data.get("observation_changed"), "step_record.observation_changed")
        ),
        confidence=None if data.get("confidence") is None else _require_number(data.get("confidence"), "confidence"),
        fixture_reference=_opt_str(data.get("fixture_reference"), "step_record.fixture_reference"),
        at=_require_number(data.get("at"), "step_record.at"),
        notes=tuple(_require_str_list(data.get("notes", []), "step_record.notes")),
        error=_opt_str(data.get("error"), "step_record.error"),
    )


def assertion_result_from_json(data: Any) -> AssertionResult:
    data = _require_mapping(data, "assertion_result")
    return AssertionResult(
        assertion_id=_require_str(data.get("assertion_id"), "assertion_result.assertion_id", allow_empty=False),
        status=_require_enum(AssertionStatus, data.get("status"), "assertion_result.status"),
        evaluator=_require_enum(Evaluator, data.get("evaluator"), "assertion_result.evaluator"),
        origin=_require_str(data.get("origin", "unspecified"), "assertion_result.origin"),
        expected=dict(_require_mapping(data.get("expected", {}), "assertion_result.expected")),
        observed=dict(_require_mapping(data.get("observed", {}), "assertion_result.observed")),
        checkpoint=_require_str(data.get("checkpoint", "any"), "assertion_result.checkpoint"),
        at=_require_number(data.get("at"), "assertion_result.at"),
        evidence_refs=tuple(
            validate_id("ev", ref) for ref in _require_str_list(data.get("evidence_refs", []), "evidence_refs")
        ),
        notes=tuple(_require_str_list(data.get("notes", []), "assertion_result.notes")),
        label=_require_str(data.get("label", "deterministic"), "assertion_result.label"),
    )


# --------------------------------------------------------------------------------------
# IPC envelope
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Envelope:
    kind: str  # "request" | "response" | "event"
    request_id: str
    method: str = ""
    session_id: str | None = None
    run_id: str | None = None
    ok: bool = True
    result: Mapping[str, Any] | None = None
    error: Mapping[str, Any] | None = None
    params: Mapping[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "v": SCHEMA_VERSION,
            "kind": self.kind,
            "request_id": self.request_id,
        }
        if self.method:
            payload["method"] = self.method
        if self.session_id is not None:
            payload["session_id"] = self.session_id
        if self.run_id is not None:
            payload["run_id"] = self.run_id
        if self.kind == "request":
            payload["params"] = dict(self.params or {})
        else:
            payload["ok"] = self.ok
            if self.ok:
                payload["result"] = dict(self.result or {})
            else:
                payload["error"] = dict(self.error or {})
        return payload

    @classmethod
    def request(
        cls,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        session_id: str | None = None,
        run_id: str | None = None,
        request_id: str | None = None,
    ) -> Envelope:
        return cls(
            kind="request",
            request_id=request_id or new_id("req"),
            method=method,
            session_id=session_id,
            run_id=run_id,
            params=dict(params or {}),
        )

    @classmethod
    def success(cls, request_id: str, result: Mapping[str, Any] | None = None, **kw: Any) -> Envelope:
        return cls(kind="response", request_id=request_id, ok=True, result=dict(result or {}), **kw)

    @classmethod
    def failure(
        cls, request_id: str, code: str, message: str, detail: Mapping[str, Any] | None = None, **kw: Any
    ) -> Envelope:
        return cls(
            kind="response",
            request_id=request_id,
            ok=False,
            error={"code": code, "message": message, "detail": dict(detail or {})},
            **kw,
        )

    @classmethod
    def from_json(cls, data: Any) -> Envelope:
        data = _require_mapping(data, "envelope")
        version = _require_str(data.get("v"), "envelope.v")
        if version != SCHEMA_VERSION:
            raise ContractError(f"unsupported schema version {version!r}")
        kind = _require_str(data.get("kind"), "envelope.kind")
        if kind not in {"request", "response", "event"}:
            raise ContractError(f"unknown envelope kind {kind!r}")
        session_id = data.get("session_id")
        run_id = data.get("run_id")
        return cls(
            kind=kind,
            request_id=_require_str(data.get("request_id"), "envelope.request_id", allow_empty=False),
            method=_require_str(data.get("method", "") or "", "envelope.method"),
            session_id=None if session_id is None else validate_id("sess", session_id),
            run_id=None if run_id is None else validate_id("run", run_id),
            ok=_require_bool(data.get("ok", True), "envelope.ok"),
            result=None if data.get("result") is None else dict(_require_mapping(data.get("result"), "result")),
            error=None if data.get("error") is None else dict(_require_mapping(data.get("error"), "error")),
            params=None if data.get("params") is None else dict(_require_mapping(data.get("params"), "params")),
        )


def envelope_from_line(line: str) -> Envelope:
    try:
        data = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ContractError("envelope is not valid JSON") from exc
    return Envelope.from_json(data)


# --------------------------------------------------------------------------------------
# Dataclass helpers used by the journal and runtime
# --------------------------------------------------------------------------------------


def dataclass_to_json(instance: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for spec in fields(instance):
        value = getattr(instance, spec.name)
        payload[spec.name] = value.value if isinstance(value, Enum) else value
    return payload


__all__ = [name for name in dir() if not name.startswith("_")]
