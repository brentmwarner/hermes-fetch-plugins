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
