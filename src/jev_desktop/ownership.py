"""Session authorization, desktop ownership, cancellation, and local emergency stop.

The broker is the only component that acquires the desktop lease, and every dispatch is
re-checked against the live lease, the cancellation state, and the emergency-stop event.
An identifier is never permission: every request carries a session and run id that must
match an active, authorized session and the current lease generation.

The emergency stop uses a named kernel event with an explicit DACL. A local process running
as the same user can set it independently of the broker, model, or capture work. A marker
file (same DACL) persists the stop, so it survives the event dying with its last process;
only an explicit clear removes it.
"""

from __future__ import annotations

import ctypes
import os
import threading
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path

from .contracts import (
    AuthorizationError,
    ContractError,
    EmergencyStop,
    Pause,
    Reason,
    new_id,
    now,
)
from .security import SecurityAttributes, current_user_sid, logon_session_id, session_id

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
EVENT_MODIFY_STATE = 0x0002
SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0

kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateEventW.restype = wintypes.HANDLE
kernel32.SetEvent.argtypes = [wintypes.HANDLE]
kernel32.ResetEvent.argtypes = [wintypes.HANDLE]
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE,
]
kernel32.CreateFileW.restype = wintypes.HANDLE
_event_handle: int | None = None
_event_lock = threading.Lock()


def _retained_event() -> int:
    # A named event disappears when its last handle closes. Every controlling process
    # retains one so a short-lived emergency-stop client can actually stop the broker.
    global _event_handle
    with _event_lock:
        if _event_handle is None:
            with SecurityAttributes() as attributes:
                handle = kernel32.CreateEventW(ctypes.byref(attributes), True, False, _event_name())
            if not handle:
                raise ContractError("could not create the emergency-stop event")
            _event_handle = int(handle)
            if _stop_marker().exists():  # the event died with the last process; the stop did not
                kernel32.SetEvent(_event_handle)
        return _event_handle


def _event_name() -> str:
    """Per-user, per-logon-session event name (local namespace only)."""
    sid = current_user_sid()
    return f"Local\\JevDesktop.EmergencyStop.{sid}.{logon_session_id()}"


def _local_path(name: str, suffix: str) -> Path:
    """Per-user, per-logon-session state file under the user's local application data."""
    directory = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "JevDesktop"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{name}-{current_user_sid()}-{logon_session_id()}{suffix}"


def _stop_marker() -> Path:
    return _local_path("emergency", ".stop")


def emergency_signal() -> bool:
    """Set the local emergency stop. Safe to call from any local process; survives restarts."""
    with SecurityAttributes() as attributes:
        handle = kernel32.CreateFileW(str(_stop_marker()), 0x40000000, 0, ctypes.byref(attributes), 2, 0x80, None)
    if handle != ctypes.c_void_p(-1).value:
        kernel32.CloseHandle(handle)
    return bool(kernel32.SetEvent(_retained_event()))


def emergency_clear() -> bool:
    _stop_marker().unlink(missing_ok=True)
    return bool(kernel32.ResetEvent(_retained_event()))


def emergency_is_set() -> bool:
    return kernel32.WaitForSingleObject(_retained_event(), 0) == WAIT_OBJECT_0


@dataclass
class Session:
    session_id: str
    client_name: str
    created_at: float
    last_seen: float
    logon_session: int
    closed: bool = False


@dataclass
class Lease:
    lease_id: str
    generation: int
    session_id: str
    run_id: str
    acquired_at: float
    released_at: float | None = None


@dataclass
class RunFlags:
    cancelled: bool = False
    cancel_reason: str | None = None
    takeover: bool = False
    takeover_detail: dict = field(default_factory=dict)


class Ownership:
    """Cross-process desktop ownership for one interactive user/logon session."""

    def __init__(self, *, clock: Callable[[], float] = now, quiesce: Callable[[], None] | None = None) -> None:
        _retained_event()
        self._lock = threading.RLock()
        self._clock = clock
        self._sessions: dict[str, Session] = {}
        self._leases: dict[str, Lease] = {}
        self._active_lease: str | None = None
        self._generation = 0
        self._runs: dict[str, RunFlags] = {}
        self._desktop_handle: int | None = None
        self._quiesce = quiesce
        self._on_acquire: Callable[[int], None] | None = None

    # -- sessions ------------------------------------------------------------------

    def create_session(self, client_name: str) -> Session:
        with self._lock:
            token = new_id("sess")
            session = Session(
                session_id=token,
                client_name=client_name[:120],
                created_at=self._clock(),
                last_seen=self._clock(),
                logon_session=logon_session_id(),
            )
            self._sessions[token] = session
            return session

    def authorize(self, session_id: str | None) -> Session:
        if not session_id:
            raise AuthorizationError("no session id supplied")
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.closed:
                raise AuthorizationError("unknown or closed session")
            if session.logon_session != logon_session_id():
                raise AuthorizationError("session belongs to a different logon session")
            session.last_seen = self._clock()
            return session

    def close_session(self, session_id: str) -> None:
        """Drop the session and its desktop lease. Run state stays paused, not cancelled.

        Losing the lease is what stops input: no client, no dispatch. The paused run stays
        resumable by whoever holds its resume token, so a disconnect is not a cancellation.
        """
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return
            session.closed = True
            if self._active_lease is not None and self._leases[self._active_lease].session_id == session_id:
                self.release(self._active_lease)

    def sessions(self) -> list[Session]:
        with self._lock:
            return list(self._sessions.values())

    # -- lease ---------------------------------------------------------------------

    def acquire(self, session_id: str, run_id: str) -> Lease:
        with self._lock:  # authorize inside the lock: a session closed meanwhile must not get a lease
            session = self.authorize(session_id)
            if self._active_lease is not None:
                holder = self._leases[self._active_lease]
                if holder.session_id != session_id or holder.run_id != run_id:
                    raise AuthorizationError(
                        f"desktop is owned by another run ({holder.run_id} by {holder.session_id})"
                    )
                return holder
            # Share mode zero excludes every other broker/direct engine, independent
            # of thread identity. Windows closes the handle on process termination.
            path = _local_path("desktop", ".lock")
            with SecurityAttributes() as attributes:
                handle = kernel32.CreateFileW(
                    str(path), 0x80000000 | 0x40000000, 0, ctypes.byref(attributes), 4, 0x80, None
                )
            if handle == ctypes.c_void_p(-1).value:
                raise AuthorizationError("desktop lease is held by another engine or unavailable")
            self._desktop_handle = int(handle)
            try:
                if self._on_acquire is not None:
                    self._on_acquire(self._desktop_handle)
            except BaseException:
                kernel32.CloseHandle(self._desktop_handle)
                self._desktop_handle = None
                raise
            self._generation += 1
            lease = Lease(
                lease_id=new_id("lease"),
                generation=self._generation,
                session_id=session.session_id,
                run_id=run_id,
                acquired_at=self._clock(),
            )
            self._leases[lease.lease_id] = lease
            self._active_lease = lease.lease_id
            return lease

    def active_lease(self) -> Lease | None:
        with self._lock:
            return self._leases[self._active_lease] if self._active_lease else None

    def validate(self, lease_id: str, generation: int, session_id: str, run_id: str) -> Lease:
        self.authorize(session_id)
        with self._lock:
            lease = self._leases.get(lease_id)
            if lease is None or lease.released_at is not None:
                raise AuthorizationError("desktop lease is not held")
            if self._active_lease != lease_id:
                raise AuthorizationError("desktop lease was superseded")
            if lease.generation != generation:
                raise AuthorizationError("stale lease generation")
            if lease.session_id != session_id or lease.run_id != run_id:
                raise AuthorizationError("lease does not belong to this session and run")
            return lease

    def release(self, lease_id: str) -> None:
        with self._lock:
            lease = self._leases.get(lease_id)
            if lease is None:
                return
            if self._active_lease == lease_id:
                if self._quiesce is not None:
                    self._quiesce()
                if self._desktop_handle is not None:
                    kernel32.CloseHandle(self._desktop_handle)
                    self._desktop_handle = None
                self._active_lease = None
            lease.released_at = self._clock()

    def __del__(self) -> None:
        handle = getattr(self, "_desktop_handle", None)
        if handle is not None:
            kernel32.CloseHandle(handle)

    def force_release(self) -> str | None:
        with self._lock:
            lease_id = self._active_lease
        if lease_id:
            self.release(lease_id)
        return lease_id

    # -- run flags -----------------------------------------------------------------

    def flags(self, run_id: str) -> RunFlags:
        with self._lock:
            return self._runs.setdefault(run_id, RunFlags())

    def request_cancel(self, run_id: str, reason: str = "caller cancelled") -> None:
        flags = self.flags(run_id)
        flags.cancelled = True
        flags.cancel_reason = reason

    def clear_cancel(self, run_id: str) -> None:
        self.flags(run_id).cancelled = False

    def mark_takeover(self, run_id: str, detail: dict) -> None:
        flags = self.flags(run_id)
        flags.takeover = True
        flags.takeover_detail = dict(detail)

    def clear_takeover(self, run_id: str) -> None:
        flags = self.flags(run_id)
        flags.takeover = False
        flags.takeover_detail = {}

    # -- emergency -----------------------------------------------------------------

    def emergency_active(self) -> bool:
        return emergency_is_set()

    def emergency_stop(self, *, clear: bool = False) -> bool:
        return emergency_clear() if clear else emergency_signal()

    # -- the one checkpoint every dispatch must pass --------------------------------

    def checkpoint(self, *, run_id: str, lease_id: str, generation: int, session_id: str) -> None:
        if self.emergency_active():
            raise EmergencyStop("local emergency stop is set; no input may be issued")
        flags = self.flags(run_id)
        if flags.cancelled:
            raise Pause(Reason.USER_TAKEOVER, {"reason": flags.cancel_reason or "run cancelled"})
        if flags.takeover:
            raise Pause(Reason.USER_TAKEOVER, flags.takeover_detail or {"reason": "human takeover"})
        try:
            self.validate(lease_id, generation, session_id, run_id)
        except AuthorizationError as exc:
            raise Pause(Reason.PERMISSION_BOUNDARY, {"reason": str(exc)}) from exc


def session_description() -> str:
    return f"session={session_id()} logon={logon_session_id()}"


def validate_same_logon(session: Session) -> None:
    if session.logon_session != logon_session_id():
        raise ContractError("session does not belong to this logon session")
