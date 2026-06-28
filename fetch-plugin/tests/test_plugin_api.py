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
    def __init__(self, *, needs=False, pairing="pairing-token", credentials_path=None):
        self.needs = needs
        self.pairing = pairing
        self.credentials_path = credentials_path
    async def _credentials(self):
        return types.SimpleNamespace(relay_url="https://relay.test", agent_id="agent-1", pairing=self.pairing)
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


def test_diagnostics_reports_tunnel_owner_and_provider(monkeypatch, tmp_path):
    runtime = api._load_sibling("fetch_plugin_runtime_diag_test", "_runtime.py")
    tunnel = api._load_sibling("fetch_plugin_tunnel_diag_test", "_tunnel.py")
    owner = tunnel.TunnelOwnerLock(agent_id="agent-1", lock_dir=tmp_path / "run")
    assert owner.acquire() is True

    fake_config = types.ModuleType("hermes_cli.config")
    fake_config.load_config = lambda: {"model": {"default": "gpt-test", "provider": "openai"}}
    fake_config.get_hermes_home = lambda: str(tmp_path)
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.config", fake_config)
    monkeypatch.setattr(runtime, "_runtime_dir", lambda: tmp_path / "run")
    monkeypatch.setattr(api, "_load_sibling", lambda module_name, filename: runtime if filename == "_runtime.py" else tunnel)

    try:
        c = _client(_FakeClient())
        body = c.get("/diagnostics").json()
    finally:
        owner.release()

    assert body["relay"]["configured"] is True
    assert body["relay"]["owner_pid"] == owner.owner_pid
    assert body["relay"]["owner"]["state"] == "owned"
    assert body["relay"]["pairing"]["state"] == "present"
    assert body["provider"] == {"state": "configured", "model": "gpt-test", "provider": "openai"}


def test_diagnostics_uses_relay_credentials_home_and_explains_shared_owner(monkeypatch, tmp_path):
    runtime = api._load_sibling("fetch_plugin_runtime_shared_owner_test", "_runtime.py")
    tunnel = api._load_sibling("fetch_plugin_tunnel_shared_owner_test", "_tunnel.py")
    paired_home = tmp_path / "paired-home"
    owner = tunnel.TunnelOwnerLock(agent_id="agent-1", lock_dir=paired_home / "run")
    owner.path.parent.mkdir(parents=True)
    owner.path.write_text("4242", encoding="utf-8")
    monkeypatch.setattr(tunnel, "_process_alive", lambda pid: True)
    monkeypatch.setattr(
        tunnel,
        "_process_command",
        lambda pid: "python -m hermes_cli.main dashboard",
    )
    monkeypatch.setattr(runtime, "_runtime_dir", lambda: tmp_path / "wrong-run")
    monkeypatch.setattr(api, "_load_sibling", lambda module_name, filename: runtime if filename == "_runtime.py" else tunnel)

    c = _client(_FakeClient(credentials_path=paired_home / "push" / "fetch-relay.json"))
    body = c.get("/diagnostics").json()

    assert body["relay"]["owner_pid"] == 4242
    assert body["relay"]["owner"]["path"] == str(owner.path)
    assert body["relay"]["owner"]["owner_current_process"] is False
    assert body["relay"]["pairing"]["state"] == "present"
    codes = {item["code"] for item in body["relay"]["troubleshooting"]}
    assert "shared_tunnel_owner" in codes
    assert "stale_pairing" in codes


def test_diagnostics_reports_foreign_tunnel_lock(monkeypatch, tmp_path):
    runtime = api._load_sibling("fetch_plugin_runtime_foreign_owner_test", "_runtime.py")
    tunnel = api._load_sibling("fetch_plugin_tunnel_foreign_owner_test", "_tunnel.py")
    paired_home = tmp_path / "paired-home"
    owner = tunnel.TunnelOwnerLock(agent_id="agent-1", lock_dir=paired_home / "run")
    owner.path.parent.mkdir(parents=True)
    owner.path.write_text("4242", encoding="utf-8")
    monkeypatch.setattr(tunnel, "_process_alive", lambda pid: True)
    monkeypatch.setattr(
        tunnel,
        "_process_command",
        lambda pid: "python /tmp/fetch_runtime_restart.py",
    )
    monkeypatch.setattr(runtime, "_runtime_dir", lambda: tmp_path / "wrong-run")
    monkeypatch.setattr(
        api,
        "_load_sibling",
        lambda module_name, filename: runtime if filename == "_runtime.py" else tunnel,
    )

    c = _client(_FakeClient(credentials_path=paired_home / "push" / "fetch-relay.json"))
    body = c.get("/diagnostics").json()

    assert body["relay"]["owner_pid"] == 4242
    assert body["relay"]["owner"]["state"] == "foreign"
    assert body["relay"]["owner"]["owner_valid"] is False
    codes = {item["code"] for item in body["relay"]["troubleshooting"]}
    assert "foreign_tunnel_owner_lock" in codes
    assert "shared_tunnel_owner" not in codes


def test_diagnostics_reports_missing_pairing_separately_from_owner(monkeypatch, tmp_path):
    runtime = api._load_sibling("fetch_plugin_runtime_pairing_missing_test", "_runtime.py")
    tunnel = api._load_sibling("fetch_plugin_tunnel_pairing_missing_test", "_tunnel.py")
    monkeypatch.setattr(runtime, "_runtime_dir", lambda: tmp_path / "run")
    monkeypatch.setattr(api, "_load_sibling", lambda module_name, filename: runtime if filename == "_runtime.py" else tunnel)

    c = _client(_FakeClient(pairing=None))
    body = c.get("/diagnostics").json()

    assert body["relay"]["owner"]["state"] == "unowned"
    assert body["relay"]["pairing"]["state"] == "missing"
    codes = {item["code"] for item in body["relay"]["troubleshooting"]}
    assert "pairing_missing" in codes
    assert "shared_tunnel_owner" not in codes


def test_provider_check_reports_recent_auth_failure(monkeypatch, tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "fetch-relay-runtime.log").write_text(
        "AuthenticationError [HTTP 401]\n"
        "Provider: custom  Model: kimi-k2.6-fast\n"
        "Error: Invalid API key\n",
        encoding="utf-8",
    )
    fake_config = types.ModuleType("hermes_cli.config")
    fake_config.load_config = lambda: {"model": {"default": "kimi-k2.6-fast", "provider": "custom"}}
    fake_config.get_hermes_home = lambda: str(tmp_path)
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.config", fake_config)

    c = _client(_FakeClient())
    body = c.get("/provider/check").json()

    assert body["ok"] is False
    assert body["category"] == "provider_auth_failed"
    assert body["provider"] == "custom"
    assert body["model"] == "kimi-k2.6-fast"
    assert "authentication" in body["message"]


def test_provider_check_ignores_auth_failure_before_latest_ready(monkeypatch, tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "fetch-relay-runtime.log").write_text(
        "AuthenticationError [HTTP 401]\n"
        "Provider: custom  Model: kimi-k2.6-fast\n"
        "Error: Invalid API key\n"
        "HERMES_DASHBOARD_READY port=9119\n",
        encoding="utf-8",
    )
    fake_config = types.ModuleType("hermes_cli.config")
    fake_config.load_config = lambda: {"model": {"default": "gpt-5.5", "provider": "openai-codex"}}
    fake_config.get_hermes_home = lambda: str(tmp_path)
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.config", fake_config)

    c = _client(_FakeClient())
    body = c.get("/provider/check").json()

    assert body["ok"] is True
    assert body["category"] == "ok"
    assert body["provider"] == "openai-codex"
    assert body["model"] == "gpt-5.5"


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
