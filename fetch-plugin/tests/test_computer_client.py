from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import uuid
from pathlib import Path

import pytest

_path = Path(__file__).resolve().parent.parent / "_computer.py"
_spec = importlib.util.spec_from_file_location("fetch_plugin_computer_test", _path)
computer = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = computer
_spec.loader.exec_module(computer)


_STOP = object()


class FakeRelay:
    def __init__(self) -> None:
        self.sent: list[bytes | str] = []

    async def send(self, payload: bytes | str) -> None:
        self.sent.append(payload)


class FakeLocal:
    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.closed = False
        self._queue: asyncio.Queue = asyncio.Queue()

    async def send(self, payload: bytes) -> None:
        self.sent.append(payload)

    async def close(self) -> None:
        self.closed = True
        self._queue.put_nowait(_STOP)

    def feed(self, payload: bytes | str) -> None:
        self._queue.put_nowait(payload)

    def __aiter__(self):
        return self

    async def __anext__(self):
        payload = await self._queue.get()
        if payload is _STOP:
            raise StopAsyncIteration
        return payload


def _client(**overrides):
    params = {
        "relay_url": "https://relay.test",
        "agent_id": "agent-1",
        "agent_secret": "secret-1",
        "local_target": "ws://127.0.0.1:6080/websockify",
        "computer_name": "Studio Mac",
        "computer_platform": "macOS",
        "computer_kind": "Mac desktop",
    }
    params.update(overrides)
    return computer.AgentComputer(**params)


@pytest.mark.parametrize(
    "url",
    [
        "ws://127.0.0.1:6080/websockify",
        "wss://localhost:6080/websockify",
        "ws://[::1]:6080/websockify",
        "ws://127.9.8.7:6080/websockify",
        "tcp://127.0.0.1:5900",
        "tcp://[::1]:5901",
    ],
)
def test_local_computer_target_accepts_only_loopback(url: str) -> None:
    assert computer.validate_local_target(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:6080/websockify",
        "ws://10.0.0.2:6080/websockify",
        "ws://vps.example.com:6080/websockify",
        "ws://localhost.evil.example:6080/websockify",
        "ws://user:password@localhost:6080/websockify",
        "tcp://10.0.0.2:5900",
        "tcp://127.0.0.1",
        "tcp://127.0.0.1:5900/path",
        "tcp://127.0.0.1:5900?password=secret",
    ],
)
def test_local_computer_target_rejects_unsafe_targets(url: str) -> None:
    with pytest.raises(ValueError):
        computer.validate_local_target(url)


def test_relay_websocket_url_and_credentials() -> None:
    client = _client()

    assert client.relay_ws_url == "wss://relay.test/v1/computer/agent"
    assert client._headers == {
        "X-Hermes-Agent-Id": "agent-1",
        "Authorization": "Bearer secret-1",
        "X-Fetch-Computer-Name": "Studio Mac",
        "X-Fetch-Computer-Platform": "macOS",
        "X-Fetch-Computer-Kind": "Mac desktop",
    }


async def test_default_connections_apply_binary_protocol_and_frame_limits(monkeypatch) -> None:
    calls: list[dict[str, object]] = []
    marker = object()

    async def fake_connect(url, headers=None, *, subprotocols=None, max_size=None):
        calls.append(
            {
                "url": url,
                "headers": headers,
                "subprotocols": subprotocols,
                "max_size": max_size,
            }
        )
        return marker

    monkeypatch.setattr(computer, "_ws_connect", fake_connect)
    client = _client(max_frame_bytes=4096)

    relay = await client._default_relay_connect(client.relay_ws_url, client._headers)
    local = await client._default_local_connect(client.local_target)

    assert relay is marker
    assert local is marker
    assert calls == [
        {
            "url": "wss://relay.test/v1/computer/agent",
            "headers": client._headers,
            "subprotocols": None,
            "max_size": 4096 + computer.CID_BYTES,
        },
        {
            "url": "ws://127.0.0.1:6080/websockify",
            "headers": None,
            "subprotocols": ["binary"],
            "max_size": 4096,
        },
    ]


async def test_direct_tcp_target_opens_loopback_stream(monkeypatch) -> None:
    reader = asyncio.StreamReader()

    class FakeWriter:
        def __init__(self) -> None:
            self.sent: list[bytes] = []
            self.closed = False

        def write(self, data: bytes) -> None:
            self.sent.append(data)

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            self.closed = True

        async def wait_closed(self) -> None:
            return None

    writer = FakeWriter()

    async def fake_open_connection(host: str, port: int):
        assert (host, port) == ("127.0.0.1", 5900)
        return reader, writer

    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)
    client = _client(local_target="tcp://127.0.0.1:5900", max_frame_bytes=4096)
    conn = await client._default_local_connect(client.local_target)

    await conn.send(b"RFB 003.008\n")
    assert writer.sent == [b"RFB 003.008\n"]
    await conn.close()
    assert writer.closed is True


async def test_binary_frames_bridge_in_both_directions() -> None:
    local = FakeLocal()

    async def local_connect(url: str):
        assert url == "ws://127.0.0.1:6080/websockify"
        return local

    client = _client(local_connect=local_connect)
    relay = FakeRelay()
    cid = uuid.uuid4().hex

    await client._dispatch_control(relay, {"t": "open", "cid": cid})
    assert cid in client._sessions

    await client._dispatch_bytes(relay, uuid.UUID(hex=cid).bytes + b"viewer-to-vnc")
    assert local.sent == [b"viewer-to-vnc"]

    local.feed(b"vnc-to-viewer")
    await asyncio.sleep(0)
    assert relay.sent == [uuid.UUID(hex=cid).bytes + b"vnc-to-viewer"]

    await client._dispatch_control(relay, {"t": "close", "cid": cid})
    assert cid not in client._sessions
    assert local.closed is True


async def test_local_connection_error_is_generic_and_scoped_to_viewer() -> None:
    async def local_connect(_url: str):
        raise OSError("sensitive local detail")

    client = _client(local_connect=local_connect)
    relay = FakeRelay()
    cid = uuid.uuid4().hex

    await client._dispatch_control(relay, {"t": "open", "cid": cid})

    assert len(relay.sent) == 1
    error = json.loads(relay.sent[0])
    assert error == {"t": "error", "cid": cid, "reason": "local_unavailable"}
    assert "sensitive" not in relay.sent[0]


async def test_unknown_session_does_not_forward_bytes() -> None:
    client = _client()
    relay = FakeRelay()
    cid = uuid.uuid4().hex

    await client._dispatch_bytes(relay, uuid.UUID(hex=cid).bytes + b"data")

    assert json.loads(relay.sent[0]) == {
        "t": "error",
        "cid": cid,
        "reason": "session_unavailable",
    }
