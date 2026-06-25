import importlib.util
import sys
import types
from pathlib import Path
from fastapi import FastAPI
from fastapi.testclient import TestClient

_p = Path(__file__).resolve().parent.parent / "dashboard" / "plugin_api.py"
_spec = importlib.util.spec_from_file_location("fetch_plugin_api_test", _p)
api = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = api
_spec.loader.exec_module(api)


class _FakeClient:
    def __init__(self, *, needs=False): self.needs = needs
    async def get_attest_challenge(self): return "ch123"
    async def register_device(self, **kw):
        if self.needs and not kw.get("attestation"):
            raise api._relay.NeedsAttestation("need it")
    async def unregister_device(self, **kw): pass


def _client(fake):
    api._relay.relay_client = lambda: fake          # monkeypatch the factory
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


def test_challenge_proxy():
    c = _client(_FakeClient())
    assert c.get("/attest/challenge").json() == {"challenge": "ch123"}


def test_register_returns_428_when_attestation_needed():
    c = _client(_FakeClient(needs=True))
    res = c.post("/register", json={"token": "t", "environment": "sandbox",
                                    "bundle_id": "com.brentwarner.fetch"})
    assert res.status_code == 428


def test_register_ok_with_attestation():
    c = _client(_FakeClient(needs=True))
    res = c.post("/register", json={"token": "t", "environment": "sandbox",
                                    "bundle_id": "com.brentwarner.fetch",
                                    "attestation": "AAAA", "key_id": "k1", "challenge": "ch123"})
    assert res.status_code == 200


def test_register_422_on_partial_attestation():
    c = _client(_FakeClient())
    res = c.post("/register", json={"token": "t", "environment": "sandbox",
                                    "bundle_id": "com.brentwarner.fetch", "attestation": "AAAA"})  # missing key_id+challenge
    assert res.status_code == 422


# --- Inbox delivery endpoints (migrated from the old hermes-inbox dashboard) ---


class _FakeInbox:
    PLATFORM_NAME = "fetch"
    DEFAULT_CHANNEL = "default"
    HOME_CHANNEL_ENV = "HERMES_FETCH_HOME_CHANNEL"

    def __init__(self, *, enabled=False):
        self.enabled = enabled
        self.delivered = []
        self.enable_calls = []

    def is_delivery_enabled(self):
        return self.enabled

    def set_delivery_enabled(self, enabled, *, channel=None):
        self.enable_calls.append((enabled, channel))
        self.enabled = enabled

    def deliver_to_inbox(self, **kw):
        self.delivered.append(kw)
        return types.SimpleNamespace(session_id="inbox_default", message_id=42)


def _inbox_client(fake_inbox):
    api._load_inbox = lambda: fake_inbox     # monkeypatch the lazy loader
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


def test_inbox_status_reports_target_and_channel(monkeypatch):
    monkeypatch.setenv("HERMES_FETCH_HOME_CHANNEL", "leads")
    c = _inbox_client(_FakeInbox(enabled=True))
    body = c.get("/inbox/status").json()
    assert body == {
        "installed": True,
        "enabled": True,
        "delivery_target": "fetch",
        "home_channel": "leads",
        "home_channel_env": "HERMES_FETCH_HOME_CHANNEL",
    }


def test_inbox_enable_persists_channel_and_flags_restart():
    fake = _FakeInbox()
    c = _inbox_client(fake)
    res = c.post("/inbox/enable", json={"enabled": True, "channel": "leads"})
    assert res.status_code == 200
    body = res.json()
    assert fake.enable_calls == [(True, "leads")]
    assert body["delivery_target"] == "fetch"
    assert body["home_channel"] == "leads"
    assert body["restart_required"] is True


def test_inbox_test_delivers_via_fetch_when_enabled():
    fake = _FakeInbox(enabled=True)
    c = _inbox_client(fake)
    res = c.post("/inbox/test", json={"channel": "default", "message": "ping"})
    assert res.status_code == 200
    assert fake.delivered and fake.delivered[0]["content"] == "ping"
    assert res.json() == {"ok": True, "session_id": "inbox_default", "message_id": 42}


def test_inbox_test_400_when_delivery_disabled():
    c = _inbox_client(_FakeInbox(enabled=False))
    res = c.post("/inbox/test", json={"channel": "default", "message": "ping"})
    assert res.status_code == 400
