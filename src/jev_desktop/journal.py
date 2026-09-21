"""Durable dispatch journal and run records.

Single-use dispatch identities plus conservative recovery. SQLite in WAL mode with
``synchronous=FULL`` so an intent is durable *before* any native input is issued.

Guarantees implemented here:

* A duplicate *completed* action returns its original receipt and never re-dispatches.
* A duplicate action in ``dispatching`` or ``uncertain`` never auto-replays; it raises
  :class:`~jev_desktop.contracts.Pause` so the runtime must observe and reconcile first.
* A guard rejection is recorded as ``not_dispatched`` (no side effect attempted).
* Any failure after the dispatch boundary is recorded as ``uncertain``.
* A journal write failure marks the journal unhealthy and the runtime must stop mutating.

Exactly-once GUI effects are NOT claimed: only single-use dispatch identities.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from .contracts import (
    ContractError,
    DispatchState,
    DriverError,
    EmergencyStop,
    Pause,
    Reason,
    Receipt,
    UncertainEffect,
    canonical_json,
    now,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, value BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS requests (
    request_id TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    response TEXT
);
CREATE TABLE IF NOT EXISTS effects (
    action_id    TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    run_id       TEXT,
    state        TEXT NOT NULL CHECK(state IN ('dispatching','dispatched','not_dispatched','uncertain')),
    receipt      TEXT,
    note         TEXT,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS effects_run ON effects(run_id);

CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    spec_digest  TEXT NOT NULL,
    spec_json    TEXT NOT NULL,
    status       TEXT NOT NULL,
    state_json   TEXT NOT NULL,
    resume_token TEXT,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS traces (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id   TEXT,
    at       REAL NOT NULL,
    kind     TEXT NOT NULL,
    payload  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS traces_run ON traces(run_id, id);

CREATE TABLE IF NOT EXISTS evidence (
    evidence_id TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    at          REAL NOT NULL,
    payload     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS evidence_run ON evidence(run_id, at);
"""


class JournalUnhealthy(RuntimeError):
    """The journal could not durably record state; no further mutation is permitted."""


@dataclass(frozen=True)
class DispatchRecord:
    action_id: str
    request_hash: str
    run_id: str | None
    state: DispatchState
    receipt: Mapping[str, object] | None
    note: str | None
    created_at: float
    updated_at: float


class DispatchJournal:
    def fingerprint_key(self) -> bytes:
        with self._lock:
            self._execute(
                "INSERT OR IGNORE INTO metadata (name, value) VALUES ('fingerprint_key', ?)", (os.urandom(32),)
            )
            return bytes(self._execute("SELECT value FROM metadata WHERE name='fingerprint_key'").fetchone()[0])

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._unhealthy: str | None = None
        self._closed = False
        self._db = sqlite3.connect(path, isolation_level=None, timeout=10, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(SCHEMA)

    # -- health -------------------------------------------------------------------

    def begin_request(self, request_id: str, request_hash: str) -> dict | None:
        """Durably reserve a transport request before it can create a run or action."""
        self.require_healthy()
        with self._lock:
            row = self._execute(
                "SELECT request_hash, response FROM requests WHERE request_id=?", (request_id,)
            ).fetchone()
            if row is not None:
                if row[0] != request_hash:
                    raise ContractError("request_id was reused with different parameters")
                if row[1] is None:
                    raise Pause(Reason.UNCERTAIN_EFFECT, {"request_id": request_id})
                return json.loads(row[1])
            self._execute("INSERT INTO requests VALUES (?, ?, NULL)", (request_id, request_hash))
        return None

    def finish_request(self, request_id: str, response: Mapping) -> None:
        self._execute("UPDATE requests SET response=? WHERE request_id=?", (canonical_json(response), request_id))

    @property
    def healthy(self) -> bool:
        return self._unhealthy is None

    def require_healthy(self) -> None:
        if self._unhealthy is not None:
            raise JournalUnhealthy(self._unhealthy)

    def close(self) -> None:
        """Idempotent. After this, every operation reports a closed store rather than
        touching a closed SQLite connection, which would take the process down."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._db.close()

    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Single entry point for SQL. Callers inside the lock pay nothing: it is reentrant."""
        with self._lock:
            if self._closed:
                raise JournalUnhealthy("journal is closed")
            try:
                return self._db.execute(sql, params)
            except sqlite3.Error as exc:  # pragma: no cover - requires fault injection
                self._unhealthy = f"journal write failed: {exc}"
                raise JournalUnhealthy(self._unhealthy) from exc

    # -- dispatch -----------------------------------------------------------------

    def dispatch_once(
        self,
        *,
        action_id: str,
        request_hash: str,
        run_id: str,
        guard: Callable[[], None],
        send: Callable[[], Receipt],
    ) -> Receipt:
        """Dispatch an action at most once. Caller holds exclusive desktop ownership."""
        self.require_healthy()
        with self._lock:
            existing = self.lookup(action_id)
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise ContractError("idempotency key reused for a different action")
                if existing.state is DispatchState.DISPATCHED and existing.receipt is not None:
                    return Receipt.from_json(existing.receipt)
                raise Pause(
                    Reason.UNCERTAIN_EFFECT if existing.state is DispatchState.UNCERTAIN else Reason.STALE_OBSERVATION,
                    {"existing_action": action_id, "state": existing.state.value},
                )

            stamp = now()
            try:
                self._db.execute("BEGIN IMMEDIATE")
                self._db.execute(
                    "INSERT INTO effects VALUES (?, ?, ?, 'dispatching', NULL, NULL, ?, ?)",
                    (action_id, request_hash, run_id, stamp, stamp),
                )
                self._db.execute("COMMIT")  # durable before any native input
            except sqlite3.Error as exc:
                self._db.execute("ROLLBACK")
                self._unhealthy = f"could not persist dispatch intent: {exc}"
                raise JournalUnhealthy(self._unhealthy) from exc

        try:
            guard()  # read-only: cancellation, scope, focus, freshness, ownership
        except BaseException:
            self._settle(action_id, DispatchState.NOT_DISPATCHED, None, "guard rejected")
            raise

        try:
            receipt = send()
        except UncertainEffect as exc:
            self._settle(action_id, DispatchState.UNCERTAIN, None, f"uncertain: {exc}")
            raise
        except (DriverError, EmergencyStop, Pause, ContractError) as exc:
            self._settle(action_id, DispatchState.NOT_DISPATCHED, None, f"pre-dispatch failure: {exc}")
            raise
        except BaseException as exc:  # origin unknown: never auto-replay
            self._settle(action_id, DispatchState.UNCERTAIN, None, f"unknown failure after boundary: {exc!r}")
            raise

        self._settle(action_id, DispatchState.DISPATCHED, receipt, None)
        return receipt

    def _settle(
        self,
        action_id: str,
        state: DispatchState,
        receipt: Receipt | None,
        note: str | None,
    ) -> None:
        encoded = None if receipt is None else canonical_json(receipt.to_json())
        try:
            self._execute(
                "UPDATE effects SET state=?, receipt=?, note=?, updated_at=? WHERE action_id=?",
                (state.value, encoded, note, now(), action_id),
            )
        except sqlite3.Error as exc:  # pragma: no cover - requires fault injection
            self._unhealthy = f"could not settle dispatch {action_id}: {exc}"
            raise JournalUnhealthy(self._unhealthy) from exc

    def reconcile(self, action_id: str, state: DispatchState, note: str) -> None:
        """Record the outcome of observing a previously uncertain action."""
        if state not in {DispatchState.DISPATCHED, DispatchState.NOT_DISPATCHED, DispatchState.UNCERTAIN}:
            raise ContractError("reconcile target state must be dispatched, not_dispatched or uncertain")
        with self._lock:
            self._execute(
                "UPDATE effects SET state=?, note=?, updated_at=? WHERE action_id=?",
                (state.value, note, now(), action_id),
            )

    def lookup(self, action_id: str) -> DispatchRecord | None:
        with self._lock:
            row = self._execute(
                "SELECT action_id, request_hash, run_id, state, receipt, note, created_at, updated_at "
                "FROM effects WHERE action_id=?",
                (action_id,),
            ).fetchone()
        if row is None:
            return None
        return DispatchRecord(
            action_id=row[0],
            request_hash=row[1],
            run_id=row[2],
            state=DispatchState(row[3]),
            receipt=None if row[4] is None else json.loads(row[4]),
            note=row[5],
            created_at=row[6],
            updated_at=row[7],
        )

    def unfinished_actions(self) -> list[DispatchRecord]:
        """Actions left mid-flight, e.g. after a broker restart. Recover as uncertain."""
        with self._lock:
            rows = self._execute(
                "SELECT action_id FROM effects WHERE state IN ('dispatching','uncertain') ORDER BY created_at"
            ).fetchall()
        return [record for record in (self.lookup(row[0]) for row in rows) if record is not None]

    def recover_unfinished(self) -> list[str]:
        """After restart: a durable 'dispatching' row means outcome unknown, never replay."""
        recovered: list[str] = []
        for record in self.unfinished_actions():
            self.reconcile(record.action_id, DispatchState.UNCERTAIN, "recovered after broker restart")
            recovered.append(record.action_id)
        return recovered

    def actions_for_run(self, run_id: str) -> list[DispatchRecord]:
        with self._lock:
            rows = self._execute(
                "SELECT action_id FROM effects WHERE run_id=? ORDER BY created_at", (run_id,)
            ).fetchall()
        return [record for record in (self.lookup(row[0]) for row in rows) if record is not None]

    # -- runs ---------------------------------------------------------------------

    def put_run(
        self,
        *,
        run_id: str,
        spec_digest: str,
        spec_json: str,
        status: str,
        state_json: str,
        resume_token: str | None,
    ) -> None:
        stamp = now()
        with self._lock:
            self._execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET status=excluded.status, state_json=excluded.state_json, "
                "resume_token=excluded.resume_token, updated_at=excluded.updated_at",
                (run_id, spec_digest, spec_json, status, state_json, resume_token, stamp, stamp),
            )

    def get_run(self, run_id: str) -> Mapping[str, object] | None:
        with self._lock:
            row = self._execute(
                "SELECT run_id, spec_digest, spec_json, status, state_json, resume_token, created_at, updated_at "
                "FROM runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "run_id": row[0],
            "spec_digest": row[1],
            "spec_json": row[2],
            "status": row[3],
            "state_json": row[4],
            "resume_token": row[5],
            "created_at": row[6],
            "updated_at": row[7],
        }

    def list_runs(self, limit: int = 50) -> list[Mapping[str, object]]:
        with self._lock:
            rows = self._execute(
                "SELECT run_id, status, spec_digest, updated_at FROM runs ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [{"run_id": r[0], "status": r[1], "spec_digest": r[2], "updated_at": r[3]} for r in rows]

    # -- traces and evidence ------------------------------------------------------

    def append_trace(self, run_id: str | None, kind: str, payload: Mapping[str, object]) -> None:
        self._execute(
            "INSERT INTO traces (run_id, at, kind, payload) VALUES (?, ?, ?, ?)",
            (run_id, now(), kind, canonical_json(payload)),
        )

    def traces(self, run_id: str, limit: int = 500) -> list[Mapping[str, object]]:
        with self._lock:
            rows = self._execute(
                "SELECT at, kind, payload FROM traces WHERE run_id=? ORDER BY id LIMIT ?", (run_id, limit)
            ).fetchall()
        return [{"at": r[0], "kind": r[1], "payload": json.loads(r[2])} for r in rows]

    def add_evidence(self, evidence_id: str, run_id: str, payload: Mapping[str, object]) -> None:
        self._execute(
            "INSERT OR REPLACE INTO evidence (evidence_id, run_id, at, payload) VALUES (?, ?, ?, ?)",
            (evidence_id, run_id, now(), canonical_json(payload)),
        )

    def evidence_for_run(self, run_id: str) -> list[Mapping[str, object]]:
        with self._lock:
            rows = self._execute("SELECT payload FROM evidence WHERE run_id=? ORDER BY at", (run_id,)).fetchall()
        return [json.loads(r[0]) for r in rows]
