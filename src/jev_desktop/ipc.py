"""Local named-pipe transport: explicit DACL, byte-mode newline-framed JSON.

The pipe is created with a security descriptor that grants access only to SYSTEM, the
object owner, and the current user SID, because Windows named-pipe defaults can otherwise include
read access for Everyone and anonymous users. The server verifies that the client runs in the
same Terminal Services session. The client grants the server identification only (never
impersonation) and refuses a server process that does not run as the current user in the same
session, so a pre-created (squatted) pipe never receives requests. The pipe name embeds the
logon session id.
"""

from __future__ import annotations

import ctypes
import json
import threading
import time
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from .contracts import ContractError, Envelope
from .security import SecurityAttributes, current_user_sid, logon_session_id, process_user_sid, session_id

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

PIPE_ACCESS_DUPLEX = 0x00000003
FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
PIPE_TYPE_BYTE = 0x00000000
PIPE_READMODE_BYTE = 0x00000000
PIPE_WAIT = 0x00000000
PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
PIPE_UNLIMITED_INSTANCES = 255
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
ERROR_PIPE_CONNECTED = 535
ERROR_BROKEN_PIPE = 109
ERROR_NO_DATA = 232
ERROR_MORE_DATA = 234
ERROR_PIPE_BUSY = 231

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
SECURITY_SQOS_PRESENT = 0x00100000
SECURITY_IDENTIFICATION = 0x00010000

MAX_MESSAGE_BYTES = 8 * 1024 * 1024

kernel32.CreateNamedPipeW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.c_void_p,
]
kernel32.CreateNamedPipeW.restype = wintypes.HANDLE
kernel32.ConnectNamedPipe.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
kernel32.ConnectNamedPipe.restype = wintypes.BOOL
kernel32.DisconnectNamedPipe.argtypes = [wintypes.HANDLE]
kernel32.DisconnectNamedPipe.restype = wintypes.BOOL
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
kernel32.ReadFile.argtypes = [
    wintypes.HANDLE,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    ctypes.c_void_p,
]
kernel32.ReadFile.restype = wintypes.BOOL
kernel32.WriteFile.argtypes = [
    wintypes.HANDLE,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    ctypes.c_void_p,
]
kernel32.WriteFile.restype = wintypes.BOOL
kernel32.PeekNamedPipe.argtypes = [
    wintypes.HANDLE,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.DWORD),
]
kernel32.PeekNamedPipe.restype = wintypes.BOOL
kernel32.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
kernel32.WaitNamedPipeW.restype = wintypes.BOOL
kernel32.GetNamedPipeServerProcessId.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)]
kernel32.GetNamedPipeServerProcessId.restype = wintypes.BOOL
kernel32.GetNamedPipeClientProcessId.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)]
kernel32.GetNamedPipeClientProcessId.restype = wintypes.BOOL
kernel32.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
kernel32.ProcessIdToSessionId.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
kernel32.FlushFileBuffers.restype = wintypes.BOOL


def pipe_name(suffix: str = "broker") -> str:
    """Per-user, per-logon-session pipe name in the local namespace."""
    sid = current_user_sid()
    return f"\\\\.\\pipe\\jev-desktop-{suffix}-{sid}-{logon_session_id()}"


def client_process_id(handle: int) -> int:
    pid = wintypes.ULONG(0)
    if not kernel32.GetNamedPipeClientProcessId(handle, ctypes.byref(pid)):
        return 0
    return int(pid.value)


def _process_session(pid: int) -> int | None:
    value = wintypes.DWORD(0)
    if not pid or not kernel32.ProcessIdToSessionId(pid, ctypes.byref(value)):
        return None
    return int(value.value)


def peer_session_matches(handle: int) -> bool:
    """Verify the connected client runs in this Terminal Services session."""
    return _process_session(client_process_id(handle)) == session_id()


def verify_server(handle: int) -> None:
    """Refuse a pipe server that is not this user's process in this session."""
    pid = wintypes.ULONG(0)
    if not kernel32.GetNamedPipeServerProcessId(handle, ctypes.byref(pid)):
        raise UntrustedServer(f"could not identify the pipe server ({ctypes.get_last_error()})")
    if process_user_sid(pid.value) != current_user_sid():
        raise UntrustedServer(f"pipe server process {pid.value} does not run as the current user")
    if _process_session(pid.value) != session_id():
        raise UntrustedServer(f"pipe server process {pid.value} runs in a different session")


class ConnectionClosed(RuntimeError):
    pass


class UntrustedServer(ConnectionClosed):
    """The pipe exists but is served by another account or session; never send it requests."""


@dataclass
class ConnectionInfo:
    peer_pid: int
    session: int
    connection_id: str = field(default_factory=lambda: uuid4().hex)


class _FramedHandle:
    """Newline-framed message reader/writer over a pipe handle."""

    def __init__(self, handle: int, *, timeout_s: float | None = None) -> None:
        self.handle = handle
        self.timeout_s = timeout_s
        self._buffer = bytearray()

    def write_line(self, payload: bytes) -> None:
        view = ctypes.create_string_buffer(payload)
        written = wintypes.DWORD(0)
        offset = 0
        while offset < len(payload):
            chunk = ctypes.cast(ctypes.addressof(view) + offset, ctypes.c_void_p)
            if not kernel32.WriteFile(self.handle, chunk, len(payload) - offset, ctypes.byref(written), None):
                raise ConnectionClosed(f"WriteFile failed ({ctypes.get_last_error()})")
            if written.value == 0:
                raise ConnectionClosed("write returned zero bytes")
            offset += written.value

    def read_line(self, *, deadline: float | None = None) -> bytes | None:
        """Return one newline-terminated frame, or None when the deadline expires."""
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                line = bytes(self._buffer[:newline])
                del self._buffer[: newline + 1]
                return line
            if deadline is not None and time.monotonic() >= deadline:
                return None
            available = wintypes.DWORD(0)
            if not kernel32.PeekNamedPipe(self.handle, None, 0, None, ctypes.byref(available), None):
                error = ctypes.get_last_error()
                if error in {ERROR_BROKEN_PIPE, ERROR_NO_DATA}:
                    raise ConnectionClosed("peer closed the pipe")
                raise ConnectionClosed(f"PeekNamedPipe failed ({error})")
            if available.value == 0:
                time.sleep(0.01)
                continue
            chunk = ctypes.create_string_buffer(min(available.value, 65536))
            read = wintypes.DWORD(0)
            if not kernel32.ReadFile(self.handle, chunk, len(chunk), ctypes.byref(read), None):
                error = ctypes.get_last_error()
                if error in {ERROR_BROKEN_PIPE, ERROR_NO_DATA}:
                    raise ConnectionClosed("peer closed the pipe")
                raise ConnectionClosed(f"ReadFile failed ({error})")
            self._buffer += chunk.raw[: read.value]
            if len(self._buffer) > MAX_MESSAGE_BYTES:
                raise ConnectionClosed("message exceeds the maximum frame size")


class PipeServer:
    """One pipe instance per connection; the handler is called per request frame."""

    def __init__(
        self,
        *,
        name: str | None = None,
        handler: Callable[[Envelope, ConnectionInfo], Envelope | None],
        max_instances: int = 8,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
        on_disconnect: Callable[[ConnectionInfo], None] | None = None,
    ) -> None:
        self.name = name or pipe_name()
        self.handler = handler
        self.max_instances = max_instances
        self._stop = threading.Event()
        self._first = True
        self._lock = threading.Lock()
        # One slot per pipe instance, listening or connected: when every instance is in use the
        # accept loop waits for a connection to end instead of failing CreateNamedPipe.
        self._slots = threading.BoundedSemaphore(max_instances)
        self._pending: int | None = None
        self._accept_done = threading.Event()
        self._accept_done.set()
        self._threads: list[threading.Thread] = []
        self._on_event = on_event
        self._on_disconnect = on_disconnect

    def _log(self, kind: str, payload: dict[str, Any]) -> None:
        if self._on_event is not None:
            self._on_event(kind, payload)

    def claim(self) -> None:
        """Create the first pipe instance now; raises if another server already owns the name."""
        if self._pending is not None:
            return
        self._slots.acquire()
        handle = self._create_instance()
        if not handle:
            error = ctypes.get_last_error()
            self._slots.release()
            raise ctypes.WinError(error, f"could not create pipe {self.name}")
        self._pending = handle

    def serve_forever(self) -> None:
        self._accept_done.clear()
        try:
            while not self._stop.is_set():
                handle, self._pending = self._pending, None
                if handle is None:
                    if not self._slots.acquire(timeout=0.25):
                        continue
                    handle = self._create_instance()
                    if not handle:  # instance exhaustion waits on a slot above, so this is fatal
                        error = ctypes.get_last_error()
                        self._slots.release()
                        self._log("pipe_create_failed", {"error": error})
                        break
                connected = kernel32.ConnectNamedPipe(handle, None)
                error = ctypes.get_last_error()
                if self._stop.is_set() or (not connected and error != ERROR_PIPE_CONNECTED):
                    self._close_instance(handle)
                    continue
                thread = threading.Thread(target=self._serve, args=(handle,), daemon=True, name="pipe-conn")
                thread.start()
                self._threads.append(thread)
        finally:
            if self._pending is not None:
                self._close_instance(self._pending)
                self._pending = None
            self._accept_done.set()
        self._join_connections()

    def shutdown(self) -> None:
        """Stop accepting connections without waiting; safe to call from a request handler."""
        self._stop.set()
        threading.Thread(target=self._wake_accept, daemon=True, name="pipe-wake").start()

    def stop(self, *, timeout: float = 5.0) -> None:
        """Stop accepting connections and wait for the in-flight ones to finish."""
        self.shutdown()
        self._accept_done.wait(timeout)
        self._join_connections(timeout=timeout)

    def _wake_accept(self) -> None:
        # ConnectNamedPipe blocks until a client arrives, so connect throwaway clients until the
        # accept loop has seen the stop flag.
        while not self._accept_done.wait(0.05):
            handle = kernel32.CreateFileW(
                self.name, GENERIC_READ | GENERIC_WRITE, 0, None, OPEN_EXISTING, SECURITY_SQOS_PRESENT, None
            )
            if handle and handle != INVALID_HANDLE_VALUE:
                kernel32.CloseHandle(handle)

    def _join_connections(self, *, timeout: float = 0.0) -> None:
        current = threading.current_thread()
        for thread in list(self._threads):
            if thread is not current:
                thread.join(timeout=timeout)
        self._threads = [thread for thread in self._threads if thread.is_alive()]

    def _close_instance(self, handle: int) -> None:
        kernel32.CloseHandle(handle)
        self._slots.release()

    def _create_instance(self) -> int:
        with self._lock:
            open_mode = PIPE_ACCESS_DUPLEX
            if self._first:
                open_mode |= FILE_FLAG_FIRST_PIPE_INSTANCE
            pipe_mode = PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS
            with SecurityAttributes() as attributes:
                handle = kernel32.CreateNamedPipeW(
                    self.name,
                    open_mode,
                    pipe_mode,
                    self.max_instances,
                    MAX_MESSAGE_BYTES,
                    MAX_MESSAGE_BYTES,
                    0,
                    ctypes.byref(attributes),
                )
            self._first = False
            return int(handle) if handle and handle != INVALID_HANDLE_VALUE else 0

    def _serve(self, handle: int) -> None:
        stream = _FramedHandle(handle)
        info = ConnectionInfo(peer_pid=client_process_id(handle), session=session_id())
        monitor_done = threading.Event()

        def monitor_disconnect() -> None:
            # The request thread may be inside a model call. Detect a vanished client
            # independently, so its lease is invalid before the next dispatch guard.
            while not monitor_done.wait(0.05):
                if not kernel32.PeekNamedPipe(handle, None, 0, None, None, None):
                    if self._on_disconnect is not None:
                        self._on_disconnect(info)
                    return

        monitor = threading.Thread(target=monitor_disconnect, daemon=True, name="pipe-disconnect")
        monitor.start()
        try:
            if not peer_session_matches(handle):
                self._log("rejected_peer", {"pid": info.peer_pid})
                return
            while not self._stop.is_set():
                line = stream.read_line()
                if line is None:
                    break
                if not line.strip():
                    continue
                response: Envelope | None
                try:
                    envelope = Envelope.from_json(json.loads(line.decode("utf-8")))
                except (ContractError, ValueError) as exc:
                    response = Envelope.failure("req:000000000000", "bad_request", f"invalid envelope: {exc}")
                else:
                    try:
                        response = self.handler(envelope, info)
                    except Exception as exc:
                        response = Envelope.failure(
                            envelope.request_id,
                            type(exc).__name__,
                            str(exc),
                            {"method": envelope.method},
                        )
                if response is None:
                    break
                frame = json.dumps(response.to_json(), ensure_ascii=False).encode("utf-8") + b"\n"
                if len(frame) > MAX_MESSAGE_BYTES:
                    # The client drops the connection on an oversized frame; send a small error instead.
                    too_large = Envelope.failure(
                        response.request_id, "response_too_large", "response exceeds the maximum frame size"
                    )
                    frame = json.dumps(too_large.to_json()).encode("utf-8") + b"\n"
                stream.write_line(frame)
        except ConnectionClosed as exc:
            self._log("connection_closed", {"error": str(exc)})
        finally:
            monitor_done.set()
            monitor.join(timeout=1.0)
            if self._on_disconnect is not None:
                try:
                    self._on_disconnect(info)
                except Exception as exc:
                    self._log("disconnect_handler_failed", {"error": str(exc)})
            kernel32.FlushFileBuffers(handle)  # DisconnectNamedPipe discards a reply the client has not read
            kernel32.DisconnectNamedPipe(handle)
            self._close_instance(handle)


class PipeClient:
    def __init__(self, *, name: str | None = None, timeout_s: float = 30.0) -> None:
        self.name = name or pipe_name()
        self.timeout_s = timeout_s
        self._handle: int | None = None
        self._stream: _FramedHandle | None = None
        self._request_lock = threading.RLock()

    def connect(self, *, timeout_s: float | None = None) -> None:
        if self._handle is not None:
            return
        deadline = time.monotonic() + (timeout_s if timeout_s is not None else self.timeout_s)
        while True:
            if kernel32.WaitNamedPipeW(self.name, 250):
                handle = kernel32.CreateFileW(
                    self.name,
                    GENERIC_READ | GENERIC_WRITE,
                    0,
                    None,
                    OPEN_EXISTING,
                    SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION,
                    None,
                )
                if handle and handle != INVALID_HANDLE_VALUE:
                    try:
                        verify_server(int(handle))
                    except UntrustedServer:
                        kernel32.CloseHandle(handle)
                        raise
                    self._handle = int(handle)
                    self._stream = _FramedHandle(self._handle)
                    return
            if time.monotonic() >= deadline:
                raise ConnectionClosed(f"could not connect to {self.name}")
            time.sleep(0.05)

    def request(self, envelope: Envelope, *, timeout_s: float | None = None) -> Envelope:
        with self._request_lock:
            try:
                self.connect()
                assert self._stream is not None
                self._stream.write_line(json.dumps(envelope.to_json(), ensure_ascii=False).encode("utf-8") + b"\n")
                deadline = time.monotonic() + (timeout_s if timeout_s is not None else self.timeout_s)
                line = self._stream.read_line(deadline=deadline)
                if line is None:
                    raise TimeoutError("broker response timed out; input may have executed")
                response = Envelope.from_json(json.loads(line.decode("utf-8")))
                if response.kind != "response" or response.request_id != envelope.request_id:
                    raise ConnectionClosed("broker response request ID does not match")
                return response
            except Exception:
                self.close()
                raise

    def close(self) -> None:
        if self._handle is not None:
            kernel32.CloseHandle(self._handle)
            self._handle = None
        self._stream = None
