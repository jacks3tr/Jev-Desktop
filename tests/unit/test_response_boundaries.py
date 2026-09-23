"""Regressions for response correlation, screenshot duplication and request bounds."""

import base64
import json
from types import SimpleNamespace

import pytest

from jev_desktop.contracts import Envelope
from jev_desktop.ipc import ConnectionClosed, PipeClient
from jev_desktop.transports.mcp_stdio import _content


def test_mcp_image_is_not_duplicated_in_text():
    encoded = base64.b64encode(b"image" * 200).decode()
    payload = {"screenshot": {"base64": encoded, "evidence_id": "ev:one", "scale": 0.5}}
    blocks = _content(payload)
    assert encoded not in blocks[0].text
    assert json.loads(blocks[0].text)["screenshot"]["scale"] == 0.5
    assert blocks[1].data == encoded
    assert payload["screenshot"]["base64"] == encoded


def test_mcp_text_is_compact_json():
    text = _content({"elements": [{"name": "Save", "operations": ["CLICK"]}]})[0].text
    assert text == '{"elements":[{"name":"Save","operations":["CLICK"]}]}'


def test_mismatched_reply_invalidates_pipe(monkeypatch):
    client = PipeClient()
    request = Envelope.request("status", {})
    other = Envelope.request("status", {})
    client._stream = SimpleNamespace(
        write_line=lambda _: None, read_line=lambda **_: json.dumps(other.to_json()).encode()
    )
    monkeypatch.setattr(client, "connect", lambda: None)
    closed = []
    monkeypatch.setattr(client, "close", lambda: closed.append(True))
    with pytest.raises(ConnectionClosed, match="request"):
        client.request(request)
    assert closed


def test_timeout_discards_stream_before_next_request(monkeypatch):
    client = PipeClient()
    client._stream = SimpleNamespace(write_line=lambda _: None, read_line=lambda **_: None)
    monkeypatch.setattr(client, "connect", lambda: None)
    with pytest.raises(TimeoutError):
        client.request(Envelope.request("act", {}))
    assert client._stream is None
