"""Stub HTTP server for retry, authentication, and transport checks. Does not call Jev."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class _Handler(BaseHTTPRequestHandler):
    server_version = "LocalTypeSafeServer/1"
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except ValueError:
            payload = {"_unparsed": raw.decode("utf-8", errors="replace")}
        owner: LocalTypeSafeServer = self.server.owner  # type: ignore[attr-defined]
        owner.record(payload, dict(self.headers))
        status, body = owner.next_response()
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: Any) -> None:
        """Silence per-request logging; the tests assert on the recorded requests instead."""


class LocalTypeSafeServer:
    """Serves queued responses on 127.0.0.1 and records every request it received."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self._responses: list[tuple[int, Any]] = []
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.owner = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True, name="local-typesafe")
        self._thread.start()

    @property
    def endpoint(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/v1/systemone"

    def queue(self, status: int, body: Any) -> None:
        with self._lock:
            self._responses.append((status, body))

    def next_response(self) -> tuple[int, Any]:
        with self._lock:
            if self._responses:
                return self._responses.pop(0)
        # An unqueued request is a test bug worth failing loudly rather than hanging.
        return 500, {"error": "no queued response for this request"}

    def record(self, payload: dict[str, Any], headers: dict[str, str]) -> None:
        with self._lock:
            self.requests.append(payload)
            self.headers.append(headers)

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def __enter__(self) -> LocalTypeSafeServer:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def unused_port_endpoint() -> str:
    """An endpoint nothing listens on, for exercising the real connection-failure path."""
    return "http://127.0.0.1:1/v1/systemone"
