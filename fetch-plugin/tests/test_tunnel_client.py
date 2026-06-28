import asyncio
import base64
import importlib.util
import json
import sys
from pathlib import Path

import httpx

# Load _tunnel.py by path the same way the plugin does.
_p = Path(__file__).resolve().parent.parent / "_tunnel.py"
_spec = importlib.util.spec_from_file_location("fetch_plugin_tunnel_test", _p)
tunnel = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = tunnel
_spec.loader.exec_module(tunnel)


_STOP = object()


class FakeRelayWS:
    """Captures frames the client sends back to the relay (parsed)."""

    def __init__(self):
        self.sent = []

    async def send(self, text):
        self.sent.append(json.loads(text))


class FakeLocalConn:
    """A stand-in for a local /api/ws connection: async-iterable inbound queue
    plus a captured outbound list."""

    def __init__(self):
        self._q = asyncio.Queue()
        self.sent = []
        self.closed = False

    async def send(self, text):
        self.sent.append(text)

    async def close(self):
        self.closed = True
        self._q.put_nowait(_STOP)

    def feed(self, message):
        self._q.put_nowait(message)

    def __aiter__(self):
        return self

    async def __anext__(self):
        msg = await self._q.get()
        if msg is _STOP:
            raise StopAsyncIteration
        return msg


def _http_factory(handler):
    transport = httpx.MockTransport(handler)
    return lambda: httpx.AsyncClient(transport=transport, timeout=30.0)


def _client(**kw):
    return tunnel.AgentTunnel(relay_url="https://relay.test", agent_id="a1", agent_secret="s1", **kw)


# --- helpers ---

def test_http_to_ws_scheme():
    assert tunnel._http_to_ws("https://push.tryfetchapp.com") == "wss://push.tryfetchapp.com"
    assert tunnel._http_to_ws("http://127.0.0.1:9119") == "ws://127.0.0.1:9119"


def test_relay_ws_url_built():
    t = _client()
    assert t.relay_ws_url == "wss://relay.test/v1/tunnel/agent"


def test_tunnel_owner_lock_rejects_second_owner(tmp_path):
    first = tunnel.TunnelOwnerLock(agent_id="agent/one", lock_dir=tmp_path)
    second = tunnel.TunnelOwnerLock(agent_id="agent/one", lock_dir=tmp_path)

    try:
        assert first.acquire() is True
        assert second.acquire() is False
        assert second.owner_pid == first.owner_pid
    finally:
        first.release()


def test_tunnel_owner_lock_status_explains_shared_owner(monkeypatch, tmp_path):
    lock = tunnel.TunnelOwnerLock(agent_id="agent/one", lock_dir=tmp_path)
    lock.path.write_text("4242", encoding="utf-8")
    monkeypatch.setattr(tunnel, "_process_alive", lambda pid: True)

    status = lock.status()

    assert status["state"] == "owned"
    assert status["owner_pid"] == 4242
    assert status["owner_current_process"] is False
    assert "not a device or app-client limit" in status["meaning"]
    assert lock.acquire() is False
    assert lock.owner_pid == 4242


def test_tunnel_owner_lock_reclaims_stale_owner(monkeypatch, tmp_path):
    lock = tunnel.TunnelOwnerLock(agent_id="agent/one", lock_dir=tmp_path)
    lock.path.write_text("4242", encoding="utf-8")
    monkeypatch.setattr(tunnel, "_process_alive", lambda pid: False)

    try:
        assert lock.status()["state"] == "stale"
        assert lock.acquire() is True
        assert lock.status()["owner_current_process"] is True
    finally:
        lock.release()


def test_loop_health_marks_unhealthy_after_rapid_events():
    health = tunnel._LoopHealth(window_s=30, threshold=2)

    assert health.record(reason="relay reconnect loop") is False
    assert health.record(reason="relay reconnect loop") is True
    assert health.unhealthy is True
    assert health.unhealthy_reason == "relay reconnect loop"


# --- REST forwarding ---

async def test_rest_text_round_trip():
    def handler(request):
        assert request.url.path == "/api/status"
        assert request.headers.get("x-hermes-session-token") == "tok"
        return httpx.Response(200, json={"ok": True})

    t = _client(dashboard_token="tok", http_client_factory=_http_factory(handler))
    ws = FakeRelayWS()
    await t._handle_rest(ws, {"t": "rest-req", "cid": "c1", "sid": 1, "method": "GET", "path": "/api/status"})

    assert len(ws.sent) == 1
    r = ws.sent[0]
    assert r["t"] == "rest-resp" and r["cid"] == "c1" and r["sid"] == 1
    assert r["status"] == 200 and r["body_b64"] is False
    assert json.loads(r["body"])["ok"] is True


async def test_rest_binary_is_base64():
    png = b"\x89PNG\r\n\x1a\n"

    def handler(request):
        return httpx.Response(200, content=png, headers={"content-type": "image/png"})

    t = _client(http_client_factory=_http_factory(handler))
    ws = FakeRelayWS()
    await t._handle_rest(ws, {"t": "rest-req", "cid": "c1", "sid": 2, "method": "GET", "path": "/api/media"})

    r = ws.sent[0]
    assert r["body_b64"] is True
    assert base64.b64decode(r["body"]) == png


async def test_rest_error_returns_502():
    def handler(request):
        raise httpx.ConnectError("boom")

    t = _client(http_client_factory=_http_factory(handler))
    ws = FakeRelayWS()
    await t._handle_rest(ws, {"t": "rest-req", "cid": "c1", "sid": 3, "method": "GET", "path": "/api/x"})

    assert ws.sent[0]["status"] == 502


async def test_rest_only_does_not_open_local_websocket():
    calls = {"local": 0}

    async def local_connect(base, token):
        calls["local"] += 1
        return FakeLocalConn()

    def handler(request):
        return httpx.Response(200, json={"ok": True})

    t = _client(local_ws_connect=local_connect, http_client_factory=_http_factory(handler))
    ws = FakeRelayWS()
    await t._handle_rest(ws, {"t": "rest-req", "cid": "c1", "sid": 1, "method": "GET", "path": "/api/status"})

    assert calls["local"] == 0


# --- WS session bridging ---

async def test_ws_open_pumps_local_frames_back():
    fake = FakeLocalConn()

    async def local_connect(base, token):
        return fake

    t = _client(local_ws_connect=local_connect)
    ws = FakeRelayWS()
    await t._ensure_session(ws, "c1")
    fake.feed(json.dumps({"jsonrpc": "2.0", "method": "event", "params": {"type": "gateway.ready"}}))

    # Bounded wait: poll until the pump task delivers the expected frame.
    loop = asyncio.get_event_loop()
    deadline = loop.time() + 2.0
    while not any(f["t"] == "ws-frame" for f in ws.sent):
        if loop.time() >= deadline:
            break
        await asyncio.sleep(0)

    frames = [f for f in ws.sent if f["t"] == "ws-frame"]
    assert frames and frames[0]["cid"] == "c1"
    assert frames[0]["data"]["params"]["type"] == "gateway.ready"
    await t._close_session("c1")
    assert fake.closed


async def test_multiple_fetch_app_clients_share_one_agent_tunnel():
    local_connections = []

    async def local_connect(base, token):
        conn = FakeLocalConn()
        local_connections.append(conn)
        return conn

    t = _client(local_ws_connect=local_connect)
    ws = FakeRelayWS()

    await t._ensure_session(ws, "phone-1")
    await t._ensure_session(ws, "phone-2")
    await t._ensure_session(ws, "phone-1")

    assert set(t._sessions) == {"phone-1", "phone-2"}
    assert len(local_connections) == 2
    await t._close_all_sessions()
    assert all(conn.closed for conn in local_connections)


async def test_ws_frame_lazy_opens_session_for_drained_send():
    fake = FakeLocalConn()
    calls = {"n": 0}

    async def local_connect(base, token):
        calls["n"] += 1
        return fake

    t = _client(local_ws_connect=local_connect)
    ws = FakeRelayWS()
    # No preceding ws-open (a drained buffered prompt) — must lazily open.
    await t._handle_ws_frame(ws, {"t": "ws-frame", "cid": "c2",
                                  "data": {"method": "prompt.submit", "params": {"text": "hi"}}})
    assert calls["n"] == 1
    assert any(json.loads(s).get("method") == "prompt.submit" for s in fake.sent)
    await t._close_session("c2")


async def test_dispatch_routes_by_type():
    fake = FakeLocalConn()

    async def local_connect(base, token):
        return fake

    t = _client(local_ws_connect=local_connect)
    ws = FakeRelayWS()
    await t._dispatch(ws, {"t": "ws-open", "cid": "c3"})
    assert "c3" in t._sessions
    await t._dispatch(ws, {"t": "ws-close", "cid": "c3"})
    assert "c3" not in t._sessions


async def test_run_forever_exits_when_stopped():
    t = _client()
    t.stop()
    await asyncio.wait_for(t.run_forever(), timeout=1)
