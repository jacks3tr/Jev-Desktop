"""Local pipe transport: server identity, instance exhaustion, shutdown, and session naming."""

from __future__ import annotations

import threading
import time
import uuid

import pytest

from jev_desktop import ipc
from jev_desktop.contracts import Envelope
from jev_desktop.ipc import ConnectionClosed, PipeClient, PipeServer, UntrustedServer
from jev_desktop.security import logon_session_id


def _echo(envelope: Envelope, _info: ipc.ConnectionInfo) -> Envelope:
    return Envelope.success(envelope.request_id, {"method": envelope.method})


@pytest.fixture()
def serve():
    started: list[tuple[PipeServer, threading.Thread]] = []

    def start(**kwargs) -> tuple[PipeServer, threading.Thread]:
        server = PipeServer(name=f"\\\\.\\pipe\\jev-ipc-{uuid.uuid4().hex[:12]}", handler=_echo, **kwargs)
        thread = threading.Thread(target=server.serve_forever, daemon=True, name="test-pipe-server")
        thread.start()
        started.append((server, thread))
        return server, thread

    yield start
    for server, thread in started:
        server.stop(timeout=5)
        thread.join(5)
        assert not thread.is_alive(), "stop must unblock the accept loop"


def test_logon_session_id_reads_the_authentication_luid():
    # AuthenticationId.HighPart is 0 for ordinary logons; reading it named every pipe "...-0".
    assert logon_session_id() != 0


def test_client_refuses_a_server_running_as_another_user(serve, monkeypatch):
    server, _ = serve()
    monkeypatch.setattr(ipc, "process_user_sid", lambda _pid: "S-1-5-18")
    client = PipeClient(name=server.name)
    with pytest.raises(UntrustedServer, match="current user"):
        client.connect(timeout_s=5)
    assert client._handle is None


def test_client_refuses_a_server_in_another_session(serve, monkeypatch):
    server, _ = serve()
    monkeypatch.setattr(ipc, "session_id", lambda: -1)
    client = PipeClient(name=server.name)
    with pytest.raises(UntrustedServer, match="different session"):
        client.connect(timeout_s=5)
    assert client._handle is None


def test_instance_exhaustion_waits_instead_of_stopping_the_server(serve):
    server, thread = serve(max_instances=2)
    first, second, third = (PipeClient(name=server.name, timeout_s=5) for _ in range(3))
    try:
        first.connect(timeout_s=5)
        second.connect(timeout_s=5)
        assert second.request(Envelope.request("ping", {})).ok
        with pytest.raises(ConnectionClosed):
            third.connect(timeout_s=0.5)
        assert thread.is_alive(), "every instance in use must not shut the server down"
        first.close()
        third.connect(timeout_s=5)
        assert third.request(Envelope.request("ping", {})).ok
    finally:
        for client in (first, second, third):
            client.close()


def test_stop_from_a_handler_thread_ends_the_accept_loop(serve):
    holder: dict[str, PipeServer] = {}

    def shutdown(envelope: Envelope, _info: ipc.ConnectionInfo) -> Envelope:
        holder["server"].shutdown()
        return Envelope.success(envelope.request_id, {"stopping": True})

    server, thread = serve()
    server.handler = shutdown
    holder["server"] = server
    client = PipeClient(name=server.name, timeout_s=5)
    try:
        assert client.request(Envelope.request("shutdown", {})).result == {"stopping": True}
        started = time.monotonic()
        thread.join(5)
        assert not thread.is_alive() and time.monotonic() - started < 5
    finally:
        client.close()
