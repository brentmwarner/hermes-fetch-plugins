import importlib.util
import json
import sys
from pathlib import Path
import httpx
import pytest

# Load _relay.py by path the same way the plugin does.
_p = Path(__file__).resolve().parent.parent / "_relay.py"
_spec = importlib.util.spec_from_file_location("fetch_plugin_relay_test", _p)
relay = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = relay
_spec.loader.exec_module(relay)


def test_hermes_home_honors_fetch_store_home(monkeypatch, tmp_path):
    """The relay client resolves credentials under HERMES_FETCH_STORE_HOME when
    set, so a proactive push fired under a worker profile still uses the
    relay-paired home's agent identity (matches _inbox._store_home())."""
    monkeypatch.setenv("HERMES_FETCH_STORE_HOME", str(tmp_path))
    assert relay._hermes_home() == tmp_path


def _patch_transport(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient
    def factory(*a, **k):
        k.pop("http2", None)
        return real(*a, transport=transport, **k)
    monkeypatch.setattr(relay.httpx, "AsyncClient", factory)


async def test_get_attest_challenge(monkeypatch, tmp_path):
    def handler(request):
        assert request.url.path == "/v1/attest/challenge"
        return httpx.Response(200, json={"challenge": "deadbeef"})
    _patch_transport(monkeypatch, handler)
    client = relay.RelayClient(relay_url="https://relay.test",
                               credentials_path=tmp_path / "c.json")
    assert await client.get_attest_challenge() == "deadbeef"


async def test_enroll_with_attestation_sends_fields(monkeypatch, tmp_path):
    seen = {}
    def handler(request):
        if request.url.path == "/v1/agents/register":
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"agent_id": "a1", "agent_secret": "s1"})
        if request.url.path == "/v1/devices/register":
            return httpx.Response(200, json={"ok": True, "device_id": "d1"})
        return httpx.Response(404)
    _patch_transport(monkeypatch, handler)
    client = relay.RelayClient(relay_url="https://relay.test", credentials_path=tmp_path / "c.json")
    await client.register_device(token="t", platform="ios", environment="sandbox",
                                 bundle_id="com.brentwarner.fetch", preferences={},
                                 attestation={"attestation": "AAAA", "key_id": "k1", "challenge": "ch"})
    assert seen["body"]["attestation"] == "AAAA"
    assert seen["body"]["key_id"] == "k1"
    assert seen["body"]["challenge"] == "ch"


async def test_enroll_without_attestation_when_required_raises(monkeypatch, tmp_path):
    def handler(request):
        if request.url.path == "/v1/agents/register":
            return httpx.Response(400, json={"detail": "attestation required"})
        return httpx.Response(404)
    _patch_transport(monkeypatch, handler)
    client = relay.RelayClient(relay_url="https://relay.test", credentials_path=tmp_path / "c.json")
    with pytest.raises(relay.NeedsAttestation):
        await client.register_device(token="t", platform="ios", environment="sandbox",
                                     bundle_id="com.brentwarner.fetch", preferences={})


async def test_existing_creds_skip_enrollment(monkeypatch, tmp_path):
    posted = []
    def handler(request):
        posted.append(request.url.path)
        if request.url.path == "/v1/devices/register":
            return httpx.Response(200, json={"ok": True, "device_id": "d1"})
        return httpx.Response(200, json={"agent_id": "a1", "agent_secret": "s1"})
    _patch_transport(monkeypatch, handler)
    creds_path = tmp_path / "c.json"
    # Pre-write valid creds in the format _read_credentials expects (match relay_url):
    client = relay.RelayClient(relay_url="https://relay.test", credentials_path=creds_path)
    client._write_credentials(relay.RelayCredentials(relay_url="https://relay.test",
                                                     agent_id="a1", agent_secret="s1"))
    await client.register_device(token="t", platform="ios", environment="sandbox",
                                 bundle_id="com.brentwarner.fetch", preferences={},
                                 attestation={"attestation": "AAAA", "key_id": "k1", "challenge": "ch"})
    assert "/v1/agents/register" not in posted   # enrollment skipped
    assert "/v1/devices/register" in posted


async def test_legacy_enroll_without_attestation_succeeds(monkeypatch, tmp_path):
    def handler(request):
        if request.url.path == "/v1/agents/register":
            return httpx.Response(200, json={"agent_id": "a1", "agent_secret": "s1"})
        if request.url.path == "/v1/devices/register":
            return httpx.Response(200, json={"ok": True, "device_id": "d1"})
        return httpx.Response(404)
    _patch_transport(monkeypatch, handler)
    client = relay.RelayClient(relay_url="https://relay.test", credentials_path=tmp_path / "c.json")
    await client.register_device(token="t", platform="ios", environment="sandbox",
                                 bundle_id="com.brentwarner.fetch", preferences={})  # must not raise


async def test_400_without_attestation_word_does_not_raise_needs_attestation(monkeypatch, tmp_path):
    def handler(request):
        if request.url.path == "/v1/agents/register":
            return httpx.Response(400, json={"detail": "invalid or expired challenge"})
        return httpx.Response(404)
    _patch_transport(monkeypatch, handler)
    client = relay.RelayClient(relay_url="https://relay.test", credentials_path=tmp_path / "c.json")
    with pytest.raises(httpx.HTTPStatusError):
        await client.register_device(token="t", platform="ios", environment="sandbox",
                                     bundle_id="com.brentwarner.fetch", preferences={},
                                     attestation={"attestation": "AAAA", "key_id": "k1", "challenge": "ch"})


async def test_relay_pairing_mints_fresh_after_registration(monkeypatch, tmp_path):
    posted = []
    def handler(request):
        posted.append(request.url.path)
        if request.url.path == "/v1/agents/register":
            return httpx.Response(200, json={"agent_id": "a1", "agent_secret": "s1", "pairing_secret": "p1"})
        if request.url.path == "/v1/agents/pairing":
            return httpx.Response(200, json={"pairing_secret": "p2"})
        return httpx.Response(404)
    _patch_transport(monkeypatch, handler)
    creds_path = tmp_path / "c.json"
    client = relay.RelayClient(relay_url="https://relay.test", credentials_path=creds_path)

    relay_url, agent_id, pairing = await client.relay_pairing()

    assert (relay_url, agent_id, pairing) == ("https://relay.test", "a1", "p2")
    assert "/v1/agents/register" in posted
    assert "/v1/agents/pairing" in posted
    assert json.loads(creds_path.read_text())["pairing"] == "p2"


async def test_relay_pairing_rotates_cached_pairing(monkeypatch, tmp_path):
    posted = []
    def handler(request):
        posted.append(request.url.path)
        if request.url.path == "/v1/agents/pairing":
            return httpx.Response(200, json={"pairing_secret": "fresh"})
        return httpx.Response(404)
    _patch_transport(monkeypatch, handler)
    creds_path = tmp_path / "c.json"
    client = relay.RelayClient(relay_url="https://relay.test", credentials_path=creds_path)
    client._write_credentials(relay.RelayCredentials(
        relay_url="https://relay.test", agent_id="a1", agent_secret="s1", pairing="stale"))

    relay_url, agent_id, pairing = await client.relay_pairing()

    assert (relay_url, agent_id, pairing) == ("https://relay.test", "a1", "fresh")
    assert "/v1/agents/register" not in posted
    assert posted == ["/v1/agents/pairing"]
    assert json.loads(creds_path.read_text())["pairing"] == "fresh"


async def test_relay_pairing_mints_on_demand_when_missing(monkeypatch, tmp_path):
    posted = []
    def handler(request):
        posted.append(request.url.path)
        if request.url.path == "/v1/agents/pairing":
            return httpx.Response(200, json={"pairing_secret": "p2"})
        return httpx.Response(404)   # registration must NOT be hit — identity is cached
    _patch_transport(monkeypatch, handler)
    creds_path = tmp_path / "c.json"
    client = relay.RelayClient(relay_url="https://relay.test", credentials_path=creds_path)
    # Agent enrolled before pairing capture existed: identity present, no token.
    client._write_credentials(relay.RelayCredentials(
        relay_url="https://relay.test", agent_id="a1", agent_secret="s1"))

    relay_url, agent_id, pairing = await client.relay_pairing()

    assert (relay_url, agent_id, pairing) == ("https://relay.test", "a1", "p2")
    assert "/v1/agents/register" not in posted       # used cached identity
    assert "/v1/agents/pairing" in posted            # minted on demand
    assert json.loads(creds_path.read_text())["pairing"] == "p2"


async def test_tunnel_status_uses_agent_credentials(monkeypatch, tmp_path):
    seen = {}
    def handler(request):
        seen["path"] = request.url.path
        seen["agent_id"] = request.headers.get("X-Hermes-Agent-Id")
        seen["auth"] = request.headers.get("Authorization")
        if request.url.path == "/v1/agents/tunnel/status":
            return httpx.Response(503, json={"ok": False, "agent_online": False, "reason": "agent_offline"})
        return httpx.Response(404)
    _patch_transport(monkeypatch, handler)
    creds_path = tmp_path / "c.json"
    client = relay.RelayClient(relay_url="https://relay.test", credentials_path=creds_path)
    client._write_credentials(relay.RelayCredentials(
        relay_url="https://relay.test", agent_id="a1", agent_secret="s1", pairing="p1"))

    status = await client.tunnel_status()

    assert seen == {
        "path": "/v1/agents/tunnel/status",
        "agent_id": "a1",
        "auth": "Bearer s1",
    }
    assert status["reason"] == "agent_offline"


async def test_wait_for_tunnel_online_polls_until_ready(monkeypatch, tmp_path):
    statuses = [
        {"ok": False, "agent_online": False, "reason": "agent_offline"},
        {"ok": True, "agent_online": True},
    ]
    client = relay.RelayClient(relay_url="https://relay.test", credentials_path=tmp_path / "c.json")

    async def fake_status():
        return statuses.pop(0)

    monkeypatch.setattr(client, "tunnel_status", fake_status)

    status = await client.wait_for_tunnel_online(timeout_s=1.0, interval_s=0.1)

    assert status["ok"] is True
    assert statuses == []
