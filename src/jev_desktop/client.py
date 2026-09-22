"""Shared broker client used by both transports.

Thin clients only: connect to the session broker (starting it on demand), claim a session,
and exchange versioned envelopes. Transport state is never the only relationship between
calls. Every request carries its session and run identifiers, and the broker re-authorizes
each one.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contracts import Envelope
from .ipc import ConnectionClosed, PipeClient, pipe_name


class BrokerError(RuntimeError):
    def __init__(self, code: str, message: str, detail: Mapping[str, Any] | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.detail = dict(detail or {})


@dataclass
class BrokerClient:
    pipe: str | None = None
    client_name: str = "cli"
    autostart: bool = True
    timeout_s: float = 120.0

    def __post_init__(self) -> None:
        # JEV_DESKTOP_PIPE selects a specific local broker.
        self.pipe = self.pipe or os.environ.get("JEV_DESKTOP_PIPE") or pipe_name("broker")
        self._client: PipeClient | None = None
        self._session_id: str | None = None
        self._hello: dict[str, Any] | None = None
        self._request_lock = threading.RLock()

    # -- connection ----------------------------------------------------------------

    def connect(self, *, timeout_s: float | None = None) -> None:
        if self._client is None:
            self._client = PipeClient(name=self.pipe, timeout_s=timeout_s or self.timeout_s)
        try:
            self._client.connect(timeout_s=1.0)
        except ConnectionClosed:
            if not self.autostart:
                raise
            self._spawn_broker()
            self._client.connect(timeout_s=timeout_s or 30.0)

    def _spawn_broker(self) -> None:
        """Start the on-demand broker for this logon session, detached and windowless."""
        creationflags = 0
        if os.name == "nt":
            creationflags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        environment = dict(os.environ)
        source_root = str(Path(__file__).resolve().parents[2])
        existing = environment.get("PYTHONPATH", "")
        if source_root not in existing.split(os.pathsep):
            environment["PYTHONPATH"] = os.pathsep.join(filter(None, [source_root, existing]))
        subprocess.Popen(
            [sys.executable, "-m", "jev_desktop.broker"],
            creationflags=creationflags,
            close_fds=True,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )

    def close(self) -> None:
        if self._client is not None:
            try:
                if self._session_id:
                    self.call("bye", {"session_id": self._session_id}, timeout_s=5.0)
            except Exception:
                pass
            self._client.close()
        self._client = None
        self._session_id = None
        self._hello = None

    # -- requests ------------------------------------------------------------------

    @property
    def session_id(self) -> str:
        self._ensure_session()
        assert self._session_id is not None
        return self._session_id

    def _ensure_session(self) -> None:
        if self._session_id is not None:
            return
        result = self._transact(Envelope.request("hello", {"client": self.client_name}), timeout_s=30.0)
        self._session_id = str(result["session_id"])
        self._hello = result

    def call(
        self, method: str, params: Mapping[str, Any] | None = None, *, timeout_s: float | None = None
    ) -> dict[str, Any]:
        with self._request_lock:
            self._ensure_session()
            payload = dict(params or {})
            payload.setdefault("session_id", self._session_id)
            return self._transact(Envelope.request(method, payload, session_id=self._session_id), timeout_s=timeout_s)

    def _transact(self, envelope: Envelope, *, timeout_s: float | None) -> dict[str, Any]:
        try:
            self.connect(timeout_s=timeout_s)
            assert self._client is not None
            response = self._client.request(envelope, timeout_s=timeout_s or self.timeout_s)
        except Exception:
            if self._client is not None:
                self._client.close()
            self._client = None
            self._session_id = None
            self._hello = None
            raise
        if not response.ok:
            error = response.error or {}
            raise BrokerError(str(error.get("code", "error")), str(error.get("message", "")), error.get("detail"))
        return dict(response.result or {})

    # -- convenience ---------------------------------------------------------------

    def handshake(self) -> dict[str, Any]:
        self._ensure_session()
        assert self._hello is not None
        return self._hello

    def wait_for_broker(self, *, timeout_s: float = 20.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        last: Exception | None = None
        while time.monotonic() < deadline:
            try:
                return self.handshake()
            except (ConnectionClosed, TimeoutError, OSError) as exc:
                last = exc
                self._client = None
                self._session_id = None
                time.sleep(0.2)
        raise BrokerError("broker_unavailable", f"broker did not become available: {last}")


def load_json_argument(value: str | None) -> dict[str, Any]:
    """Accept a path, '-' for stdin, or inline JSON for CLI/MCP payloads."""
    if not value:
        return {}
    if value.strip().startswith("{"):
        return json.loads(value)
    if value == "-":
        return json.loads(sys.stdin.read())
    return json.loads(Path(value).read_text(encoding="utf-8"))
