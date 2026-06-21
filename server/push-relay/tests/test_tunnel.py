from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from push_relay import tunnel
from push_relay.app import RelayStore, Settings, create_app


def _build(**overrides) -> tuple[TestClient, RelayStore]:
    tmp = tempfile.TemporaryDirectory()
    db = Path(tmp.name) / "relay.db"
    params = dict(
        database_path=db,
        apns_key_pem=None,
        apns_key_id=None,
        apns_team_id=None,
        registration_token=None,
        allow_custom_body=True,
        allow_open_registration=True,
        enable_tunnel=True,
        max_apps_per_agent=2,
        max_transit_per_agent=3,
        max_transit_bytes_per_agent=10_000,
        transit_ttl_s=900,
    )
    params.update(overrides)
    settings = Settings(**params)
    store = RelayStore(db, secret_pepper=settings.secret_pepper, max_devices_per_agent=settings.max_devices_per_agent)
    app = create_app(settings=settings, store=store)
    app.state.tmp = tmp
    return TestClient(app), store


def _register(client: TestClient) -> tuple[str, str, str]:
    res = client.post("/v1/agents/register", json={"app": "test"})
    assert res.status_code == 200
    body = res.json()
    return body["agent_id"], body["agent_secret"], body["pairing_secret"]


def _agent_headers(agent_id: str, secret: str) -> dict[str, str]:
    return {"x-hermes-agent-id": agent_id, "authorization": f"Bearer {secret}"}


# --- transit buffer (store-level) ---

def test_transit_enqueue_peek_delete_order():
    _, store = _build()
    aid, _ = store.register_agent(app="t", agent_version=None)
    for p in (b"a", b"b", b"c"):
        assert store.enqueue_transit(agent_id=aid, payload=p, ttl_s=900, max_per_agent=5)
    rows = store.peek_transit(aid)
    assert [p for _, p in rows] == [b"a", b"b", b"c"]   # FIFO with rowid tiebreak
    assert store.count_transit(aid) == 3                # peek does not delete
    store.delete_transit([rows[0][0]])
    assert store.count_transit(aid) == 2


def test_transit_respects_count_cap():
    _, store = _build()
    aid, _ = store.register_agent(app="t", agent_version=None)
    for _ in range(3):
        assert store.enqueue_transit(agent_id=aid, payload=b"x", ttl_s=900, max_per_agent=3)
    assert store.enqueue_transit(agent_id=aid, payload=b"y", ttl_s=900, max_per_agent=3) is False


def test_transit_respects_byte_budget():
    _, store = _build()
    aid, _ = store.register_agent(app="t", agent_version=None)
    assert store.enqueue_transit(agent_id=aid, payload=b"x" * 80, ttl_s=900, max_per_agent=99, max_bytes=100)
    assert store.enqueue_transit(agent_id=aid, payload=b"x" * 80, ttl_s=900, max_per_agent=99, max_bytes=100) is False


def test_transit_expired_rows_evicted():
    _, store = _build()
    aid, _ = store.register_agent(app="t", agent_version=None)
    assert store.enqueue_transit(agent_id=aid, payload=b"old", ttl_s=-1, max_per_agent=5)
    assert store.peek_transit(aid) == []


def test_transit_cipher_round_trip():
    cipher = tunnel.TransitCipher("a-secret-key")
    blob = cipher.encrypt(b"hello")
    assert blob != b"hello"
    assert cipher.decrypt(blob) == b"hello"
    assert tunnel.TransitCipher(None).encrypt(b"x") == b"x"


# --- pairing auth (store-level) ---

def test_pairing_authenticates_only_with_correct_secret():
    client, store = _build()
    aid, _secret, pairing = _register(client)
    assert store.authenticate_pairing(agent_id=aid, pairing_secret=pairing)
    assert not store.authenticate_pairing(agent_id=aid, pairing_secret="wrong")
    assert not store.authenticate_pairing(agent_id="nope", pairing_secret=pairing)


# --- WS broker (integration) ---

def test_tunnel_rest_round_trip():
    client, _ = _build()
    aid, secret, pairing = _register(client)
    with client.websocket_connect("/v1/tunnel/agent", headers=_agent_headers(aid, secret)) as agent_ws:
        with client.websocket_connect("/v1/tunnel/app", headers=_agent_headers(aid, pairing)) as app_ws:
            opened = agent_ws.receive_json()           # app connect announces a ws-open
            assert opened["t"] == "ws-open"
            cid = opened["cid"]
            app_ws.send_json({"t": "rest-req", "sid": 1, "method": "GET", "path": "/api/status"})
            fwd = agent_ws.receive_json()
            assert fwd["t"] == "rest-req"
            assert fwd["cid"] == cid and fwd["sid"] == 1
            agent_ws.send_json({"t": "rest-resp", "cid": cid, "sid": 1, "status": 200, "body": "ok"})
            resp = app_ws.receive_json()
            assert resp["status"] == 200 and resp["sid"] == 1
        closed = agent_ws.receive_json()               # app disconnect announces a ws-close
        assert closed["t"] == "ws-close" and closed["cid"] == cid


def test_offline_chat_send_buffered_then_routed_to_still_connected_app():
    """The headline round-trip: send to a sleeping agent, and when it wakes the
    reply reaches the app that stayed connected (cid survives agent reconnect)."""
    client, store = _build()
    aid, secret, pairing = _register(client)
    with client.websocket_connect("/v1/tunnel/app", headers=_agent_headers(aid, pairing)) as app_ws:
        app_ws.send_json({"t": "ws-frame", "sid": 7, "method": "prompt.submit", "data": "hi"})
        ack = app_ws.receive_json()
        assert ack["t"] == "buffered" and ack["sid"] == 7
        assert store.count_transit(aid) == 1

        # Agent comes online while the app is STILL connected.
        with client.websocket_connect("/v1/tunnel/agent", headers=_agent_headers(aid, secret)) as agent_ws:
            opened = agent_ws.receive_json()
            assert opened["t"] == "ws-open"            # agent told about the waiting app
            cid = opened["cid"]
            assert agent_ws.receive_json()["t"] == "buffer-drain-begin"
            drained = agent_ws.receive_json()
            assert drained["t"] == "ws-frame" and drained["method"] == "prompt.submit"
            assert drained["cid"] == cid               # buffered frame carries the live cid
            assert agent_ws.receive_json()["t"] == "buffer-drain-end"
            assert store.count_transit(aid) == 0       # delete-after-send

            # Reply on that cid reaches the still-connected app.
            agent_ws.send_json({"t": "rest-resp", "cid": cid, "sid": 7, "status": 200, "body": "done"})
            reply = app_ws.receive_json()
            assert reply["status"] == 200 and reply["sid"] == 7


def test_offline_nonbufferable_fails_fast():
    client, store = _build()
    aid, _secret, pairing = _register(client)
    with client.websocket_connect("/v1/tunnel/app", headers=_agent_headers(aid, pairing)) as app_ws:
        app_ws.send_json({"t": "rest-req", "sid": 3, "method": "GET", "path": "/api/sessions"})
        resp = app_ws.receive_json()
        assert resp["t"] == "agent-offline" and resp["sid"] == 3
    assert store.count_transit(aid) == 0


def test_malformed_frame_does_not_tear_down_uplink():
    client, _ = _build()
    aid, secret, pairing = _register(client)
    with client.websocket_connect("/v1/tunnel/agent", headers=_agent_headers(aid, secret)) as agent_ws:
        agent_ws.send_text("not json at all")          # garbage frame: skipped, not fatal
        with client.websocket_connect("/v1/tunnel/app", headers=_agent_headers(aid, pairing)) as app_ws:
            opened = agent_ws.receive_json()            # uplink still alive and routing
            assert opened["t"] == "ws-open"
            cid = opened["cid"]
            app_ws.send_json({"t": "rest-req", "sid": 1, "method": "GET", "path": "/x"})
            assert agent_ws.receive_json()["cid"] == cid


def test_app_cap_rejects_excess_connections():
    client, _ = _build(max_apps_per_agent=1)
    aid, _secret, pairing = _register(client)
    with client.websocket_connect("/v1/tunnel/app", headers=_agent_headers(aid, pairing)):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/v1/tunnel/app", headers=_agent_headers(aid, pairing)) as app2:
                app2.receive_json()


def test_agent_ws_rejects_bad_credentials():
    client, _ = _build()
    aid, _secret, _pairing = _register(client)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/v1/tunnel/agent", headers=_agent_headers(aid, "wrong-secret")) as ws:
            ws.receive_json()


def test_app_ws_rejects_without_pairing_token():
    client, _ = _build()
    aid, _secret, _pairing = _register(client)
    # Bare agent_id with no pairing secret (the old hijack vector) is rejected.
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/v1/tunnel/app", headers={"x-hermes-agent-id": aid}) as ws:
            ws.receive_json()
    # Wrong pairing secret is rejected.
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/v1/tunnel/app", headers=_agent_headers(aid, "not-the-pairing")) as ws:
            ws.receive_json()


def test_tunnel_routes_absent_when_flag_disabled():
    client, _ = _build(enable_tunnel=False)
    aid, secret, _pairing = _register(client)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/v1/tunnel/agent", headers=_agent_headers(aid, secret)) as ws:
            ws.receive_json()
