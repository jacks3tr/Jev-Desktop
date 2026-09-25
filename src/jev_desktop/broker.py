"""Local host broker: one per interactive user/logon session, MCP and CLI are clients.

The broker owns authorization, desktop ownership, the bounded runtime, the durable
journal, and the single native driver instance. Clients cannot reach the driver directly:
they send versioned envelopes over the local pipe and receive structured results with
opaque handles. A disconnected client keeps its paused run state but loses the lease, so
no input is issued unattended.
"""

from __future__ import annotations

import base64
import copy
import json
import os
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .contracts import (
    SCHEMA_VERSION,
    SCOPE_LIMITS,
    ActionRequest,
    AuthorizationError,
    ContractError,
    DispatchState,
    DriverError,
    EmergencyStop,
    Envelope,
    EvidenceRef,
    InputMode,
    Operation,
    Pause,
    Purpose,
    Reason,
    RunResult,
    RunSpec,
    ScopeSpec,
    UncertainEffect,
    canonical_json,
    keyed_fingerprint,
    new_id,
)
from .drivers.windows import WindowsDriver
from .evidence import EvidenceStore, RetentionPolicy
from .ipc import MAX_MESSAGE_BYTES, ConnectionInfo, PipeServer, pipe_name
from .journal import DispatchJournal, JournalUnhealthy
from .ownership import Ownership, emergency_clear, emergency_is_set, emergency_signal, session_description
from .policy import (
    HttpTransport,
    JevPolicy,
    PolicyConfig,
    PolicyError,
    RecordingTransport,
    Transport,
    build_contexts,
)
from .runtime import ResumeInputs, Runtime, RuntimeConfig, caller_view

MAX_INLINE_IMAGE_BYTES = 4 * 1024 * 1024

METHODS = {
    "hello",
    "bye",
    "inspect",
    "run",
    "act",
    "stop",
    "status",
    "evidence",
    "health",
    "shutdown",
}


def default_home() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or str(Path.home())
    return Path(base) / "JevDesktop"


@dataclass
class BrokerConfig:
    home: Path = field(default_factory=default_home)
    evidence_dir: Path | None = None
    journal_path: Path | None = None
    approved_roots: tuple[str, ...] = ()
    launch_configs: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    capture_scale: float = 0.6
    retention: RetentionPolicy = field(default_factory=RetentionPolicy)
    secrets: Mapping[str, str] = field(default_factory=dict)  # fixture secret name -> env var name

    def resolved(self) -> BrokerConfig:
        self.home = Path(self.home)
        self.evidence_dir = Path(self.evidence_dir or self.home / "evidence")
        self.journal_path = Path(self.journal_path or self.home / "journal.sqlite")
        roots = {str(self.evidence_dir), *[str(item) for item in self.approved_roots]}
        self.approved_roots = tuple(sorted(roots))
        return self

    @classmethod
    def load(cls, path: str | Path | None = None) -> BrokerConfig:
        config_path = Path(path) if path else default_home() / "config.json"
        config = cls()
        if config_path.is_file():
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            if "home" in payload:
                config.home = Path(payload["home"])
            if "evidence_dir" in payload:
                config.evidence_dir = Path(payload["evidence_dir"])
            if "journal_path" in payload:
                config.journal_path = Path(payload["journal_path"])
            config.approved_roots = tuple(payload.get("approved_roots", ()))
            config.launch_configs = dict(payload.get("launch_configs", {}))
            config.secrets = dict(payload.get("secrets", {}))
            config.capture_scale = float(payload.get("capture_scale", config.capture_scale))
            policy_payload = payload.get("policy", {})
            timeout_s = policy_payload.get("timeout_s", config.policy.timeout_s)
            config.policy = PolicyConfig(
                model_id=str(policy_payload.get("model_id", config.policy.model_id)),
                endpoint=str(policy_payload.get("endpoint", config.policy.endpoint)),
                operation_floor=float(policy_payload.get("operation_floor", config.policy.operation_floor)),
                target_floor=float(policy_payload.get("target_floor", config.policy.target_floor)),
                target_margin=float(policy_payload.get("target_margin", config.policy.target_margin)),
                completion_floor=float(policy_payload.get("completion_floor", config.policy.completion_floor)),
                timeout_s=None if timeout_s is None else float(timeout_s),
                max_retries=int(policy_payload.get("max_retries", config.policy.max_retries)),
                api_key_env=str(policy_payload.get("api_key_env", config.policy.api_key_env)),
            )
        return config.resolved()

    def save(self, path: str | Path | None = None) -> Path:
        config_path = Path(path) if path else self.home / "config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps(
                {
                    "home": str(self.home),
                    "evidence_dir": str(self.evidence_dir),
                    "journal_path": str(self.journal_path),
                    "approved_roots": list(self.approved_roots),
                    "launch_configs": dict(self.launch_configs),
                    "secrets": dict(self.secrets),
                    "capture_scale": self.capture_scale,
                    "policy": {
                        "model_id": self.policy.model_id,
                        "endpoint": self.policy.endpoint,
                        "operation_floor": self.policy.operation_floor,
                        "target_floor": self.policy.target_floor,
                        "target_margin": self.policy.target_margin,
                        "completion_floor": self.policy.completion_floor,
                        "timeout_s": self.policy.timeout_s,
                        "max_retries": self.policy.max_retries,
                        "api_key_env": self.policy.api_key_env,
                    },
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return config_path


class Broker:
    def __init__(self, config: BrokerConfig | None = None, *, driver: Any | None = None) -> None:
        self.config = (config or BrokerConfig.load()).resolved()
        self.home = self.config.home
        self.evidence_dir = self.home / "evidence" if self.config.evidence_dir is None else self.config.evidence_dir
        self.journal_path = (
            self.home / "journal.sqlite" if self.config.journal_path is None else self.config.journal_path
        )
        self.home.mkdir(parents=True, exist_ok=True)
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.driver = driver or WindowsDriver(evidence_dir=self.evidence_dir)
        self.journal = DispatchJournal(str(self.journal_path))
        self.ownership = Ownership(quiesce=getattr(self.driver, "quiesce", None))
        self.evidence = EvidenceStore(
            root=self.evidence_dir,
            approved_roots=list(self.config.approved_roots),
            retention=self.config.retention,
        )
        self.policy: JevPolicy | None = None
        self.runtime = Runtime(
            driver=self.driver,
            journal=self.journal,
            ownership=self.ownership,
            evidence=self.evidence,
            config=RuntimeConfig(
                evidence_dir=self.evidence_dir,
                approved_roots=self.config.approved_roots,
                secret_provider=lambda name: os.environ.get(self.config.secrets.get(name, name)),
                launch_configs=self.config.launch_configs,
                capture_scale=self.config.capture_scale,
            ),
        )
        self._server: PipeServer | None = None
        self._by_peer: dict[str, list[str]] = {}
        self._lock = threading.RLock()
        self._driver_lock = threading.RLock()
        self._started_at = time.time()
        self._recovered: list[str] = []
        self._inspection_tokens: dict[str, str] = {}
        self._observations: dict[str, tuple[ScopeSpec, str, str]] = {}
        self._standalone_run: str | None = None

    # ------------------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------------------

    def start(self) -> None:
        self.driver.start()
        self._recovered = self.journal.recover_unfinished()  # never auto-replay unknown effects
        if self._recovered:
            self.journal.append_trace(None, "recovered_uncertain_actions", {"actions": self._recovered})
        key = os.environ.get(self.config.policy.api_key_env)
        if key:
            transport: Transport = HttpTransport()
            record_dir = os.environ.get("JEV_DESKTOP_RECORD")
            if record_dir:
                transport = RecordingTransport(transport, Path(record_dir))
            self.policy = JevPolicy(transport=transport, config=self.config.policy, api_key=key)
            self.runtime.policy = self.policy

    def close(self) -> None:
        # Order matters: no handler may still be writing when the journal closes.
        if self._server is not None:
            self._server.stop(timeout=5.0)
        try:
            self.driver.close()
        finally:
            self.ownership.force_release()
            self.journal.close()

    def serve_forever(self) -> None:
        server = PipeServer(
            name=os.environ.get("JEV_DESKTOP_PIPE") or pipe_name("broker"),
            handler=self.handle,
            on_event=lambda kind, payload: self.journal.append_trace(None, f"pipe_{kind}", payload),
            on_disconnect=self.on_disconnect,
        )
        # Claim the pipe first: a second broker must exit before it starts a driver or
        # recovers the journal that the running broker is still using.
        server.claim()
        self._server = server
        try:
            self.start()
            server.serve_forever()
        finally:
            self.close()

    def on_disconnect(self, info: ConnectionInfo) -> None:
        """A client that vanishes keeps its paused run, but must not keep the desktop."""
        with self._lock:
            session_ids = self._by_peer.pop(info.connection_id, [])
        for session_id in session_ids:
            self.ownership.close_session(session_id)
            self.journal.append_trace(None, "client_disconnected", {"session": session_id, "pid": info.peer_pid})

    # ------------------------------------------------------------------------------
    # Request handling
    # ------------------------------------------------------------------------------

    def handle(self, envelope: Envelope, info: ConnectionInfo) -> Envelope:
        if envelope.kind != "request":
            return Envelope.failure(envelope.request_id, "bad_request", "only request envelopes are accepted")
        method = envelope.method
        if method not in METHODS:
            return Envelope.failure(envelope.request_id, "unknown_method", f"unknown method {method!r}")
        params = dict(envelope.params or {})
        try:
            if method == "hello":
                result = self._hello(params, info)
            elif method == "bye":
                if params.get("session_id") not in self._by_peer.get(info.connection_id, []):
                    raise AuthorizationError("session is bound to another connection")
                result = self._bye(params)
            else:
                session = self.ownership.authorize(str(params.get("session_id") or "") or None)
                # Stop only removes capability. Its client sends it on a separate connection so it
                # never waits behind that session's in-flight run.
                if method != "stop" and session.session_id not in self._by_peer.get(info.connection_id, []):
                    raise AuthorizationError("session is bound to another connection")
                if method in {"run", "act", "inspect"}:
                    with self._driver_lock:
                        result = getattr(self, f"_m_{method}")(session, params)
                else:
                    result = getattr(self, f"_m_{method}")(session, params)
            return fit_frame(
                Envelope.success(
                    envelope.request_id, result, session_id=params.get("session_id"), run_id=params.get("run_id")
                )
            )
        except AuthorizationError as exc:
            return Envelope.failure(envelope.request_id, "unauthorized", str(exc))
        except JournalUnhealthy as exc:
            return Envelope.failure(
                envelope.request_id,
                "journal_unhealthy",
                str(exc),
                {"hint": "the journal could not durably record state; restart the broker"},
            )
        except EmergencyStop as exc:
            return Envelope.failure(envelope.request_id, "emergency_stop", str(exc))
        except Pause as exc:
            return Envelope.failure(envelope.request_id, "paused", str(exc), {"reason": exc.reason_value, **exc.detail})
        except (ContractError, PolicyError) as exc:
            return Envelope.failure(envelope.request_id, "invalid_request", str(exc))
        except UncertainEffect as exc:
            return Envelope.failure(envelope.request_id, Reason.UNCERTAIN_EFFECT.value, str(exc))
        except DriverError as exc:
            return Envelope.failure(envelope.request_id, "driver_error", str(exc))
        except Exception as exc:
            self.journal.append_trace(
                params.get("run_id"),
                "broker_error",
                {"method": method, "error": f"{type(exc).__name__}: {exc}"},
            )
            return Envelope.failure(envelope.request_id, type(exc).__name__, str(exc))

    # -- methods -------------------------------------------------------------------

    def _hello(self, params: Mapping[str, Any], info: ConnectionInfo) -> dict[str, Any]:
        client = str(params.get("client") or "unknown")[:120]
        session = self.ownership.create_session(client)
        with self._lock:
            self._by_peer.setdefault(info.connection_id, []).append(session.session_id)
        self.journal.append_trace(
            None, "client_connected", {"session": session.session_id, "client": client, "pid": info.peer_pid}
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "session_id": session.session_id,
            "broker": self._health(),
            "capabilities": {
                "tools": ["desktop_inspect", "desktop_run", "desktop_act", "desktop_stop"],
                "operations": [operation.value for operation in Operation],
                "input_modes": [mode.value for mode in InputMode],
                "transport": "named_pipe",
                "pipe": self._server.name if self._server is not None else pipe_name("broker"),
            },
        }

    def _bye(self, params: Mapping[str, Any]) -> dict[str, Any]:
        session_id = str(params.get("session_id") or "")
        if session_id:
            self.ownership.close_session(session_id)
        return {"closed": bool(session_id)}

    def _m_health(self, session, params) -> dict[str, Any]:
        return self._health()

    def _m_status(self, session, params) -> dict[str, Any]:
        run_id = str(params.get("run_id") or "")
        if run_id:
            self._authorize_run(session, params, run_id)
            return {"run": self.runtime.status(run_id), "broker": self._health()}
        return {
            "broker": self._health(),
            "runs": [
                row
                for row in self.journal.list_runs(limit=int(params.get("limit", 20)))
                if self.runtime._load(str(row["run_id"])).summary.get("owner_session") == session.session_id
            ],
            "lease": (self.ownership.active_lease().__dict__ if self.ownership.active_lease() else None),
        }

    def _m_inspect(self, session, params) -> dict[str, Any]:
        params = dict(params)
        if params.get("run_id"):
            state = self.runtime._load(str(params["run_id"]))
            self._authorize_run(session, params, state.run_id)
            self.runtime._restore_binding(state)
            params.setdefault("app_ref", self.runtime._scope(state).app_ref)
        query = str(params.get("query") or "")
        if hasattr(self.driver, "discover"):
            apps, discovered_windows = self.driver.discover(params.get("app_ref"))
        else:
            apps = self.driver.list_apps()
            discovered_windows = self.driver.list_windows()
        if query and not params.get("app_ref"):
            lowered = query.lower()
            apps = [
                app
                for app in apps
                if lowered in app.executable_path.lower()
                or any(
                    lowered in window.title.lower() for window in discovered_windows if window.app_ref == app.app_ref
                )
            ]
        windows = [
            window.to_json()
            for window in discovered_windows
            if window.app_ref in {app.app_ref for app in apps}
            and (not params.get("app_ref") or window.app_ref == params.get("app_ref"))
        ]
        if not params.get("app_ref"):
            return {
                "applications": [app.to_json() for app in apps],
                "windows": windows,
                "note": "pass app_ref to observe a specific application (opaque reference, not permission)",
            }
        scope_payload = dict(params.get("scope") or {})
        scope_payload.setdefault("app_ref", params["app_ref"])
        scope = ScopeSpec.from_json(scope_payload)
        if params.get("run_id"):
            state = self.runtime._load(str(params["run_id"]))
            self._authorize_run(session, params, state.run_id)
            self.runtime._restore_binding(state)
            if scope.app_ref != self.runtime._scope(state).app_ref:
                raise AuthorizationError("inspection application differs from the run binding")
            scope = self.runtime._scope(state)
        limit = scope.max_elements
        if query:
            # Match against the widest observation; tree order would otherwise fill the cap with window chrome.
            scope = replace(scope, max_elements=SCOPE_LIMITS["max_elements"])
        snapshot = self.driver.observe(scope, query=query)
        include_screenshot = bool(params.get("screenshot", True))
        secrets = self.runtime._secret_values(state) if params.get("run_id") else []
        if secrets:
            include_screenshot = False
        screenshot: dict[str, Any] | None = None
        inspection_run_id = new_id("run")
        access_token = new_id("resume")
        screenshot_error = None
        try:
            if include_screenshot:
                run_id = str(params.get("run_id") or inspection_run_id)
                capture = self.driver.capture(
                    scope=scope,
                    snapshot_id=snapshot.snapshot_id,
                    run_id=run_id,
                    checkpoint="inspect",
                    description="inspection screenshot",
                    max_scale=self.config.capture_scale,
                )
                self.evidence.register(capture.evidence)
                self.journal.add_evidence(capture.evidence.evidence_id, run_id, capture.evidence.to_json())
                screenshot = self._evidence_payload(capture.evidence, include_base64=bool(params.get("inline_image")))
                if not params.get("run_id"):
                    token = new_id("resume")
                    self._inspection_tokens[capture.evidence.evidence_id] = token
                    screenshot["access_token"] = token
                self.evidence.prune()
                for evidence_id in list(self._inspection_tokens):
                    try:
                        self.evidence.get(evidence_id)
                    except ContractError:
                        self._inspection_tokens.pop(evidence_id, None)
        except (DriverError, ContractError) as exc:
            screenshot_error = {"reason": type(exc).__name__, "detail": str(exc)}
        payload = {
            "application": next((app.to_json() for app in apps if app.app_ref == params["app_ref"]), None),
            "windows": [window.to_json() for window in snapshot.windows],
            **caller_view(snapshot, limit=limit, query=query),
            "coverage": snapshot.coverage.value,
            "snapshot_id": snapshot.snapshot_id,
            "geometry": snapshot.geometry.to_json(),
            "interval_ms": snapshot.interval_ms,
            "screenshot": screenshot,
            "screenshot_error": screenshot_error,
        }

        if not params.get("run_id"):
            self._observations[snapshot.snapshot_id] = (scope, access_token, inspection_run_id)
            while len(self._observations) > 256:
                self._observations.pop(next(iter(self._observations)))
            payload["access_token"] = access_token

        def redact(value):
            if isinstance(value, str):
                return self.runtime._redact(value, secrets)
            if isinstance(value, dict):
                return {key: redact(item) for key, item in value.items()}
            if isinstance(value, list):
                return [redact(item) for item in value]
            return value

        return redact(payload)

    def _m_run(self, session, params) -> dict[str, Any]:
        if "task" in params:
            if any(key in params for key in ("run", "spec", "run_id")):
                raise ContractError("supply a task, a workflow, or a resume request, not several")
            return self._run_task(session, params)
        if "run" in params or "spec" in params:
            return self._run_new(session, params)
        return self._run_resume(session, params)

    def _run_task(self, session, params) -> dict[str, Any]:
        task = params["task"]
        if not isinstance(task, dict):
            raise ContractError("task must be an object")
        texts = task.get("texts", {})
        secret_texts = task.get("secret_texts", {})
        hotkeys = task.get("hotkeys", [])
        if (
            not isinstance(texts, dict)
            or not isinstance(secret_texts, dict)
            or len(texts) + len(secret_texts) > 16
            or set(texts) & set(secret_texts)
            or any(
                not isinstance(name, str) or not isinstance(value, str)
                for name, value in [*texts.items(), *secret_texts.items()]
            )
        ):
            raise ContractError("texts and secret_texts must hold at most 16 uniquely named strings")
        if not isinstance(hotkeys, list) or len(hotkeys) > 8 or any(not isinstance(key, str) for key in hotkeys):
            raise ContractError("hotkeys must contain at most 8 chords")
        from .drivers.windows.input import parse_chord

        for chord in hotkeys:
            parse_chord(chord.split("+"))
        windows = task.get("window_refs")
        if not isinstance(windows, list) or not windows:
            raise ContractError("task requires explicit window_refs from inspection")
        timeout = task.get("timeout_seconds", 60)
        if type(timeout) not in (int, float) or not 0 < timeout <= 120:
            raise ContractError("timeout_seconds must be between 0 and 120")
        spec = {
            "goal": task.get("goal"),
            "purpose": "task",
            "interaction_mode": "user_path",
            "app_ref": task.get("app_ref"),
            "expected_identity": {"mode": "any"},
            "scope": {
                "app_ref": task.get("app_ref"),
                "window_refs": windows,
                "max_elements": task.get("max_elements", 180),
                "max_depth": task.get("max_depth", 12),
            },
            "fixtures": {
                **{f"text:{name}": value for name, value in texts.items()},
                **{f"secret:{name}": value for name, value in secret_texts.items()},
                **{f"key:{index}": value for index, value in enumerate(hotkeys)},
            },
            "limits": {
                "max_actions": task.get("max_actions", 20),
                "max_model_decisions": task.get("max_model_decisions", 40),
                "deadline_seconds": timeout,
                "slice_seconds": min(timeout, 120),
                "stale_retries": 2,
                "no_progress_retries": 2,
            },
        }
        return self._run_new(session, {"run": spec, "inline_image": False})

    def _run_new(self, session, params: Mapping[str, Any]) -> dict[str, Any]:
        payload = dict(params.get("run") or params.get("spec") or {})
        spec = RunSpec.from_json(payload)
        if spec.limits.to_json() != spec.limits.clamped().to_json():
            raise ContractError("requested limits exceed local policy")
        if self.policy is None and (spec.steps or spec.purpose is Purpose.TASK) and not params.get("start_only"):
            raise PolicyError(
                f"no decision policy is configured: set {self.config.policy.api_key_env} for the broker "
                "process, or drive the run with desktop_act"
            )
        inputs_payload = dict(params.get("inputs") or {})
        inputs = ResumeInputs(
            fixtures=dict(inputs_payload.get("fixtures") or {}),
            visual_results=dict(inputs_payload.get("visual_results") or {}),
            verifier_results=dict(inputs_payload.get("verifier_results") or {}),
        )
        created = self.runtime.create_run(spec, session_id=session.session_id)
        if params.get("start_only"):
            state = self.runtime._load(created["run_id"])
            self.runtime._apply_inputs(state, inputs)
            self.runtime._persist(state)
            return created
        run_id = created["run_id"]
        self.ownership.acquire(session.session_id, run_id)
        self.journal.append_trace(run_id, "lease_acquired", {"session": session.session_id})
        result = self.runtime.slice(
            run_id=run_id,
            session_id=session.session_id,
            resume_token=created["resume_token"],
            inputs=inputs,
            slice_seconds=float(params.get("slice_seconds") or spec.limits.slice_seconds),
        )
        return self._run_payload(result, inline_image=bool(params.get("inline_image")))

    def _run_resume(self, session, params: Mapping[str, Any]) -> dict[str, Any]:
        run_id = str(params.get("run_id") or "")
        if not run_id:
            raise ContractError("run requires either a specification or a run_id with resume_token")
        state = self.runtime._load(run_id)
        if params.get("resume_token") != state.resume_token:
            raise ContractError("resume token does not match the current run checkpoint")
        if state.status in {"completed", "cancelled"}:
            raise ContractError("run is terminal; create a new run")
        lease = self.ownership.active_lease()
        if lease is None or lease.run_id != run_id:
            self.ownership.acquire(session.session_id, run_id)
        inputs_payload = dict(params.get("inputs") or {})
        inputs = ResumeInputs(
            fixtures=dict(inputs_payload.get("fixtures") or {}),
            visual_results=dict(inputs_payload.get("visual_results") or {}),
            verifier_results=dict(inputs_payload.get("verifier_results") or {}),
        )
        result = self.runtime.slice(
            run_id=run_id,
            session_id=session.session_id,
            resume_token=str(params.get("resume_token") or ""),
            inputs=inputs,
            slice_seconds=float(params.get("slice_seconds") or state.spec.limits.slice_seconds),
        )
        return self._run_payload(result, inline_image=bool(params.get("inline_image")))

    def _m_act(self, session, params) -> dict[str, Any]:
        run_id = str(params.get("run_id") or "")
        if not run_id:
            return self._act_observation(session, params)
        state = self.runtime._load(run_id)
        if params.get("resume_token") != state.resume_token:
            raise ContractError("resume token does not match the current run checkpoint")
        lease = self.ownership.active_lease()
        if lease is None or lease.run_id != run_id:
            self.ownership.acquire(session.session_id, run_id)
        action_payload = dict(params.get("action") or {})
        action_payload.setdefault("run_id", run_id)
        active = self.ownership.active_lease()
        if active is None:  # pragma: no cover - acquire above guarantees a lease
            raise ContractError("no lease is held for this run")
        action_payload.setdefault("lease_generation", active.generation)
        action_payload["action_id"] = new_id("act")
        request = ActionRequest.from_json(action_payload)
        result = self.runtime.act(
            run_id=run_id,
            session_id=session.session_id,
            resume_token=str(params.get("resume_token") or ""),
            request=request,
        )
        return result

    def _act_observation(self, session, params) -> dict[str, Any]:
        payload = dict(params.get("action") or {})
        snapshot_id = str(payload.get("snapshot_id") or "")
        observation = self._observations.get(snapshot_id)
        if observation is None:
            raise ContractError("the inspection is unknown or already used; inspect the application again")
        scope, token, action_run_id = observation
        if params.get("access_token") != token:
            raise AuthorizationError("action requires the inspection access_token")
        payload.update(action_id=new_id("act"), run_id=action_run_id, lease_generation=0)
        payload.setdefault("mode", "user_path")
        request = ActionRequest.from_json(payload)
        if request.operation not in {
            Operation.CLICK,
            Operation.TYPE_TEXT,
            Operation.SELECT,
            Operation.TOGGLE,
            Operation.SCROLL,
            Operation.FOCUS_WINDOW,
            Operation.HOTKEY,
        }:
            raise ContractError("unsupported standalone operation")
        snapshot = self.driver.snapshot(snapshot_id, scope)
        if request.window_ref not in {window.window_ref for window in snapshot.windows}:
            raise ContractError("action window is outside the observation scope")
        if request.element_id and snapshot.element(request.element_id).window_ref != request.window_ref:
            raise ContractError("action element and window do not match")
        if request.point is not None:
            request.point.resolve(self.evidence.get(request.point.evidence_id), action_run_id, snapshot_id)
        if request.operation is Operation.TYPE_TEXT and request.text is None:
            raise ContractError("TYPE_TEXT requires text")
        if request.operation is Operation.SELECT and request.option_label is None:
            raise ContractError("SELECT requires option_label")
        if request.operation is Operation.HOTKEY and not request.hotkey:
            raise ContractError("HOTKEY requires a chord")
        lease = self.ownership.acquire(session.session_id, action_run_id)
        self._standalone_run = action_run_id
        deadline = time.monotonic() + 45.0
        epoch = self.runtime._human_epoch()

        def guard() -> None:
            if time.monotonic() >= deadline:
                raise Pause(Reason.BUDGET_EXHAUSTED, {"budget": "action_deadline"})
            if self.runtime._human_epoch() != epoch:
                raise Pause(Reason.USER_TAKEOVER, {"reason": "physical input during the action; inspect again"})
            self.ownership.checkpoint(
                run_id=action_run_id,
                lease_id=lease.lease_id,
                generation=lease.generation,
                session_id=session.session_id,
            )

        try:
            guard()
            target_description = params.get("target_description")
            if target_description:
                if request.element_id or request.point:
                    raise ContractError("supply target_description or an explicit target, not both")
                if self.policy is None:
                    raise PolicyError("target_description requires a TypeSafe API key")
                if not isinstance(target_description, str) or len(target_description) > 4000:
                    raise ContractError("target_description must be text of at most 4000 characters")
                policy_observation = {
                    "elements": [
                        element.to_json() for element in snapshot.elements if element.window_ref == request.window_ref
                    ]
                }
                contexts = build_contexts(observation=policy_observation, operations=[request.operation])
                if not contexts:
                    raise ContractError("no compatible observed controls; use an explicit window for focus or hotkeys")
                decision = self.policy.decide(
                    goal=target_description,
                    state=policy_observation,
                    contexts=contexts,
                    allow_done=False,
                    current_step={"operation": request.operation.value, "target_description": target_description},
                    deadline=min(deadline, time.monotonic() + 30.0),
                )
                if decision.operation is not request.operation or decision.target is None:
                    raise Pause(Reason.STEP_UNRESOLVED, {"operation": decision.operation.value})
                request = replace(request, element_id=decision.target.element_id)
            guard()
            if request.element_id and snapshot.element(request.element_id).window_ref != request.window_ref:
                raise ContractError("action element and window do not match")
            if hasattr(self.driver, "set_boundary"):
                self.driver.set_boundary(guard, min(15.0, deadline - time.monotonic()))
            request = replace(request, lease_generation=lease.generation, deadline_s=15.0)
            request = replace(
                request,
                request_hash=keyed_fingerprint(self.journal.fingerprint_key(), canonical_json(request.to_json())),
            )
            # Consume before dispatch, including uncertain outcomes. A new request must inspect again.
            self._observations.pop(snapshot_id)
            try:
                receipt = self.journal.dispatch_once(
                    action_id=request.action_id,
                    request_hash=request.request_hash,
                    run_id=action_run_id,
                    guard=guard,
                    send=lambda: self.driver.execute(request, guard, snapshot),
                )
            except Exception as exc:
                # Nothing was sent and the screen is as inspected, so the caller may still recover from it.
                record = self.journal.lookup(request.action_id)
                if (
                    not isinstance(exc, EmergencyStop)
                    and record is not None
                    and record.state is DispatchState.NOT_DISPATCHED
                    and self.runtime._human_epoch() == epoch
                ):
                    self._observations[snapshot_id] = observation
                raise
            return {"receipt": receipt.to_json(), "app_ref": scope.app_ref, "needs_inspection": True}
        finally:
            if hasattr(self.driver, "set_boundary"):
                self.driver.set_boundary(None)
            self.ownership.release(lease.lease_id)
            self._standalone_run = None

    def _m_stop(self, session, params) -> dict[str, Any]:
        if params.get("emergency"):
            if params.get("clear"):
                cleared = emergency_clear()
                return {"emergency_stop": "cleared" if cleared else "not_set"}
            emergency_signal()
            lease = self.ownership.active_lease()
            if lease is not None:
                self.ownership.request_cancel(lease.run_id, "local emergency stop")
                self.ownership.release(lease.lease_id)
            self.journal.append_trace(None, "emergency_stop", {"session": session.session_id})
            return {"emergency_stop": "set", "released_run": lease.run_id if lease else None}
        run_id = str(params.get("run_id") or "")
        if not run_id:
            lease = self.ownership.active_lease()
            if lease is None:
                return {"cancelled": False, "detail": "no active run"}
            run_id = lease.run_id
        if run_id == self._standalone_run:
            lease = self.ownership.active_lease()
            if lease is None or lease.session_id != session.session_id:
                raise AuthorizationError("only the action owner can cancel it; use emergency stop locally")
            self.ownership.request_cancel(run_id, str(params.get("reason") or "caller cancelled"))
            return {"cancelled": True}
        return self.runtime.stop(
            run_id=run_id,
            session_id=session.session_id,
            reason=str(params.get("reason") or "caller cancelled"),
            resume_token=params.get("resume_token"),
        )

    def _m_evidence(self, session, params) -> dict[str, Any]:
        evidence_id = str(params.get("evidence_id") or "")
        reference = self.evidence.get(evidence_id)
        token = self._inspection_tokens.get(evidence_id)
        if token is not None:
            if params.get("resume_token") != token:
                raise AuthorizationError("inspection evidence requires its access token")
        else:
            self._authorize_run(session, params, reference.run_id)
        return self._evidence_payload(reference, include_base64=bool(params.get("inline_image", True)))

    def _m_shutdown(self, session, params) -> dict[str, Any]:
        self.journal.append_trace(None, "shutdown_requested", {"session": session.session_id})
        if self._server is not None:
            # This runs on a connection thread: signal only. serve_forever's caller joins and closes.
            self._server.shutdown()
        return {"stopping": True}

    # -- helpers -------------------------------------------------------------------

    def _authorize_run(self, session, params, run_id: str) -> None:
        state = self.runtime._load(run_id)
        if (
            state.summary.get("owner_session") != session.session_id
            and params.get("resume_token") != state.resume_token
        ):
            raise AuthorizationError("run access requires its current resume token")

    def _health(self) -> dict[str, Any]:
        driver_health = self.driver.health()
        return {
            "schema_version": SCHEMA_VERSION,
            "pid": os.getpid(),
            "started_at": self._started_at,
            "session": session_description(),
            "driver": driver_health,
            "policy": {
                "configured": self.policy is not None,
                "key_present": bool(os.environ.get(self.config.policy.api_key_env)),
                "model": self.config.policy.model_id,
                "key_env": self.config.policy.api_key_env,
                "resolved_models": list(self.policy.resolved_models[-5:]) if self.policy else [],
                "recording": isinstance(getattr(self.policy, "transport", None), RecordingTransport),
            },
            "journal": {
                "path": str(self.config.journal_path),
                "healthy": self.journal.healthy,
                "recovered_uncertain": self._recovered,
            },
            "evidence": {"root": str(self.evidence_dir), **self.evidence.usage()},
            "approved_roots": list(self.config.approved_roots),
            "emergency_stop": emergency_is_set(),
            "lease": (self.ownership.active_lease().__dict__ if self.ownership.active_lease() else None),
            "paused_runs": [row["run_id"] for row in self.journal.list_runs(50) if row["status"] == "paused"],
        }

    def _evidence_payload(self, reference: EvidenceRef, *, include_base64: bool) -> dict[str, Any]:
        payload: dict[str, Any] = reference.to_json()
        payload["fetchable"] = True
        if include_base64 and reference.size_bytes <= MAX_INLINE_IMAGE_BYTES and os.path.isfile(reference.path):
            payload["base64"] = base64.b64encode(self.evidence.read(reference.evidence_id)).decode("ascii")
        return payload

    def _run_payload(self, result: RunResult, *, inline_image: bool) -> dict[str, Any]:
        state = self.runtime._load(result.run_id)
        if state.spec.purpose is Purpose.TASK:
            return {
                "run_id": result.run_id,
                "resume_token": result.resume_token,
                "execution": result.execution.value,
                "reason": result.reason,
                "completion": result.detail.get("completion"),
                "detail": dict(result.detail),
                "observation": result.observation
                if result.execution.value == "completed"
                else state.summary.get("task_observation"),
                "actions": [
                    {
                        "operation": step.operation.value,
                        "target": step.target_description,
                        "changed": step.observation_changed,
                    }
                    for step in result.steps
                ],
                "metrics": {**dict(result.budgets), **state.summary.get("task_metrics", {})},
            }
        payload = result.to_json()
        payload["status"] = self.runtime.status(result.run_id)
        images = []
        for reference in result.evidence[-3:] if inline_image else []:
            images.append(self._evidence_payload(reference, include_base64=True))
        payload["images"] = images
        return payload


def _frame_size(payload: Envelope) -> int:
    return len(json.dumps(payload.to_json(), ensure_ascii=False).encode("utf-8")) + 1  # + newline


def _inline_images(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        found = [value] if isinstance(value.get("base64"), str) else []
        return found + [image for item in value.values() for image in _inline_images(item)]
    if isinstance(value, list):
        return [image for item in value for image in _inline_images(item)]
    return []


def fit_frame(response: Envelope, limit: int = MAX_MESSAGE_BYTES) -> Envelope:
    """Keep a response inside one pipe frame, so a rotated resume token always reaches the client.

    Inline images are dropped first (largest first; each stays fetchable by evidence ID). If
    the rest is still too large, only the run identity and resume token are returned.
    """
    size = _frame_size(response)
    if size <= limit or not response.ok:
        return response
    result = copy.deepcopy(dict(response.result or {}))
    for image in sorted(_inline_images(result), key=lambda item: len(item["base64"]), reverse=True):
        size -= len(image.pop("base64")) - 32  # conservative: the omission marker adds a few bytes
        image["inline_omitted"] = True
        if size <= limit:
            break
    fitted = replace(response, result=result)
    if _frame_size(fitted) <= limit:
        return fitted
    kept: dict[str, Any] = {
        key: result[key] for key in ("run_id", "resume_token", "execution", "reason") if key in result
    }
    return Envelope.failure(
        response.request_id,
        "response_too_large",
        "response exceeds the maximum frame size; fetch evidence and status separately",
        kept,
        session_id=response.session_id,
        run_id=response.run_id,
    )


def main(argv: list[str] | None = None) -> int:
    """Run the broker in the foreground (normally started on demand by a client)."""
    import argparse

    parser = argparse.ArgumentParser(prog="jev-desktop-broker", description="Jev host-desktop broker")
    parser.add_argument("--config", help="path to config.json")
    parser.add_argument("--print-config", action="store_true")
    args = parser.parse_args(argv)
    config = BrokerConfig.load(args.config)
    if args.print_config:
        print(json.dumps(json.loads(json.dumps(config.__dict__, default=str)), indent=2))
        return 0
    broker = Broker(config)
    broker.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
