from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from push_relay.app import APNsResult, Device, PushService, RelayStore, Settings, create_app
from push_relay import attestation as att


class FakeAPNs:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, *, device: Device, payload: dict, collapse_id: str | None) -> APNsResult:
        self.sent.append({"device": device, "payload": payload, "collapse_id": collapse_id})
        return APNsResult(ok=True, status=200, reason=None, should_prune=False)


def _client(**overrides) -> tuple[TestClient, FakeAPNs]:
    tmp = tempfile.TemporaryDirectory()
    db_path = Path(tmp.name) / "relay.db"
    params = dict(
        database_path=db_path,
        apns_key_pem=None,
        apns_key_id=None,
        apns_team_id=None,
        registration_token=None,
        allow_custom_body=True,
        allow_open_registration=True,
    )
    params.update(overrides)
    settings = Settings(**params)  # type: ignore[arg-type]
    store = RelayStore(
        db_path,
        secret_pepper=settings.secret_pepper,
        max_devices_per_agent=settings.max_devices_per_agent,
    )
    fake_apns = FakeAPNs()
    service = PushService(store=store, settings=settings, apns=fake_apns)  # type: ignore[arg-type]
    app = create_app(settings=settings, store=store, push_service=service)
    app.state.tmp = tmp
    return TestClient(app), fake_apns


def _register_agent(client: TestClient) -> dict:
    res = client.post("/v1/agents/register", json={"app": "test"})
    assert res.status_code == 200
    agent = res.json()
    return {
        "X-Hermes-Agent-Id": agent["agent_id"],
        "Authorization": f"Bearer {agent['agent_secret']}",
    }


def test_agent_device_push_flow_uses_custom_copy_by_default() -> None:
    client, fake_apns = _client()
    headers = _register_agent(client)

    device_res = client.post(
        "/v1/devices/register",
        headers=headers,
        json={
            "token": "device-token",
            "platform": "ios",
            "environment": "sandbox",
            "bundle_id": "com.brentwarner.fetch",
            "preferences": {"replies": True, "attention": False, "sound": False},
        },
    )
    assert device_res.status_code == 200

    push_res = client.post(
        "/v1/push/events",
        headers=headers,
        json={
            "type": "replies",
            "session_id": "s1",
            "title": "Assistant replied",
            "body": "The report is ready.",
        },
    )
    assert push_res.status_code == 200
    assert push_res.json()["sent"] == 1
    # No apns-collapse-id: each push is its own notification (grouping comes from
    # aps thread-id). See test_collapse.py for the per-session regression check.
    assert fake_apns.sent[0]["collapse_id"] is None
    assert fake_apns.sent[0]["payload"]["aps"]["alert"]["title"] == "Assistant replied"
    assert fake_apns.sent[0]["payload"]["aps"]["alert"]["body"] == "The report is ready."
    assert "sound" not in fake_apns.sent[0]["payload"]["aps"]

    attention_res = client.post(
        "/v1/push/events",
        headers=headers,
        json={"type": "attention", "session_id": "s2"},
    )
    assert attention_res.status_code == 200
    assert attention_res.json()["sent"] == 0


def test_push_flow_uses_generic_copy_when_custom_body_disabled() -> None:
    client, fake_apns = _client(allow_custom_body=False)
    headers = _register_agent(client)

    device_res = client.post(
        "/v1/devices/register",
        headers=headers,
        json={
            "token": "device-token",
            "platform": "ios",
            "environment": "sandbox",
            "bundle_id": "com.brentwarner.fetch",
            "preferences": {"replies": True},
        },
    )
    assert device_res.status_code == 200

    push_res = client.post(
        "/v1/push/events",
        headers=headers,
        json={
            "type": "replies",
            "session_id": "s1",
            "title": "Assistant replied",
            "body": "The report is ready.",
        },
    )
    assert push_res.status_code == 200
    assert push_res.json()["sent"] == 1
    assert fake_apns.sent[0]["payload"]["aps"]["alert"]["title"] == "Fetch replied"
    assert fake_apns.sent[0]["payload"]["aps"]["alert"]["body"] == "Open Fetch to continue."


def test_agent_auth_required() -> None:
    client, _fake_apns = _client()
    res = client.post(
        "/v1/devices/register",
        json={
            "token": "device-token",
            "platform": "ios",
            "environment": "sandbox",
            "bundle_id": "com.brentwarner.fetch",
            "preferences": {},
        },
    )
    assert res.status_code == 401


def test_open_registration_disabled_when_configured() -> None:
    # Without a registration token AND without explicitly allowing open
    # registration, the relay must fail closed rather than minting anonymous
    # identities.
    client, _ = _client(allow_open_registration=False)
    res = client.post("/v1/agents/register", json={"app": "test"})
    assert res.status_code == 503


def test_registration_token_enforced() -> None:
    client, _ = _client(registration_token="enroll-secret", allow_open_registration=False)
    bad = client.post("/v1/agents/register", json={"app": "test"})
    assert bad.status_code == 401
    good = client.post(
        "/v1/agents/register",
        json={"app": "test"},
        headers={"X-Hermes-Relay-Registration-Token": "enroll-secret"},
    )
    assert good.status_code == 200


def test_device_cannot_be_hijacked_by_another_agent() -> None:
    client, _ = _client()
    a = _register_agent(client)
    b = _register_agent(client)
    body = {
        "token": "shared-token",
        "platform": "ios",
        "environment": "sandbox",
        "bundle_id": "com.brentwarner.fetch",
        "preferences": {},
    }
    assert client.post("/v1/devices/register", headers=a, json=body).status_code == 200
    # Agent B tries to claim agent A's device token -> rejected.
    hijack = client.post("/v1/devices/register", headers=b, json=body)
    assert hijack.status_code == 409
    # A re-registering (refreshing prefs) still works.
    assert client.post("/v1/devices/register", headers=a, json=body).status_code == 200


def test_unsupported_bundle_id_rejected() -> None:
    client, _ = _client()
    headers = _register_agent(client)
    res = client.post(
        "/v1/devices/register",
        headers=headers,
        json={
            "token": "device-token",
            "platform": "ios",
            "environment": "sandbox",
            "bundle_id": "com.brentwarner.hermes",  # stale pre-rename id
            "preferences": {},
        },
    )
    assert res.status_code == 400


def test_legacy_secret_hash_accepted_and_migrated_after_pepper_added() -> None:
    # Agent registered before a pepper existed -> stored as a bare SHA-256 hash.
    tmp = tempfile.TemporaryDirectory()
    db_path = Path(tmp.name) / "relay.db"
    legacy_store = RelayStore(db_path)
    agent_id, secret = legacy_store.register_agent(app="test", agent_version=None)

    # Relay later upgraded with a pepper: the old credential must keep working...
    peppered = RelayStore(db_path, secret_pepper="s3cr3t-pepper")
    assert peppered.authenticate_agent(agent_id=agent_id, secret=secret) is True
    # ...while a wrong secret must still be rejected.
    assert peppered.authenticate_agent(agent_id=agent_id, secret="nope") is False

    # The successful auth should have rewritten the stored hash to the peppered
    # form, so a fresh store with the same pepper still authenticates.
    reopened = RelayStore(db_path, secret_pepper="s3cr3t-pepper")
    assert reopened.authenticate_agent(agent_id=agent_id, secret=secret) is True


def test_settings_attestation_fields(monkeypatch):
    from push_relay.app import Settings
    monkeypatch.delenv("HERMES_RELAY_REQUIRE_ATTESTATION", raising=False)
    assert Settings.from_env().require_attestation is False
    monkeypatch.setenv("HERMES_RELAY_REQUIRE_ATTESTATION", "true")
    monkeypatch.setenv("HERMES_RELAY_APPLE_APP_ID", "GCYB5LT4QQ.com.brentwarner.fetch")
    s = Settings.from_env()
    assert s.require_attestation is True
    assert s.apple_app_id == "GCYB5LT4QQ.com.brentwarner.fetch"
    assert s.max_agents_per_attest_key == 5      # default
    assert s.attest_challenge_ttl_s == 300         # default
    assert s.attest_production is True             # default (real devices)


def test_device_cap_enforced() -> None:
    client, _ = _client(max_devices_per_agent=1)
    headers = _register_agent(client)
    base = {
        "platform": "ios",
        "environment": "sandbox",
        "bundle_id": "com.brentwarner.fetch",
        "preferences": {},
    }
    assert client.post("/v1/devices/register", headers=headers, json={**base, "token": "t1"}).status_code == 200
    over = client.post("/v1/devices/register", headers=headers, json={**base, "token": "t2"})
    assert over.status_code == 429


def test_challenge_is_single_use_and_expires(tmp_path):
    from push_relay.app import RelayStore
    store = RelayStore(tmp_path / "relay.db", secret_pepper="p", max_devices_per_agent=50)
    c = store.create_challenge()
    assert isinstance(c, str) and len(c) >= 32
    assert store.consume_challenge(c, ttl_s=300) is True     # first use ok
    assert store.consume_challenge(c, ttl_s=300) is False    # single-use
    c2 = store.create_challenge()
    assert store.consume_challenge(c2, ttl_s=0) is False      # already older than ttl=0


def test_register_agent_records_key_and_counts(tmp_path):
    from push_relay.app import RelayStore
    store = RelayStore(tmp_path / "relay.db", secret_pepper="p", max_devices_per_agent=50)
    assert store.count_agents_for_key("k1") == 0
    a1, s1 = store.register_agent(app="fetch-ios", agent_version=None, attest_key_id="k1")
    a2, s2 = store.register_agent(app="fetch-ios", agent_version=None, attest_key_id="k1")
    assert a1 != a2 and s1 != s2
    assert store.count_agents_for_key("k1") == 2
    assert store.count_agents_for_key("other") == 0


# ---------------------------------------------------------------------------
# Attested enrollment tests
# ---------------------------------------------------------------------------


class _FakeVerifier:
    def __init__(self, ok=True): self.ok = ok
    def verify(self, *, platform, payload, challenge):
        if not self.ok:
            raise att.AttestationError("nope")
        return att.AttestedIdentity(platform="ios", key_id=str(payload["key_id"]))


def _attest_app(tmp_path, verifier, max_per_key=5):
    db = tmp_path / "relay.db"
    settings = Settings(
        database_path=db,
        apns_key_pem=None,
        apns_key_id=None,
        apns_team_id=None,
        registration_token=None,
        allow_custom_body=False,
        require_attestation=True,
        apple_app_id="GCYB5LT4QQ.com.brentwarner.fetch",
        max_agents_per_attest_key=max_per_key,
        attest_challenge_ttl_s=300,
    )
    store = RelayStore(db, secret_pepper="p", max_devices_per_agent=50)
    return create_app(settings=settings, store=store, verifier=verifier), store


def test_attested_enrollment_mints_credentials(tmp_path):
    app, store = _attest_app(tmp_path, _FakeVerifier(ok=True))
    client = TestClient(app)
    challenge = client.get("/v1/attest/challenge").json()["challenge"]
    res = client.post("/v1/agents/register", json={
        "app": "fetch-ios", "attestation": "AAAA", "key_id": "k1", "challenge": challenge})
    assert res.status_code == 200
    assert res.json()["agent_id"] and res.json()["agent_secret"]


def test_enrollment_requires_attestation_when_flagged(tmp_path):
    app, _ = _attest_app(tmp_path, _FakeVerifier(ok=True))
    res = TestClient(app).post("/v1/agents/register", json={"app": "fetch-ios"})
    assert res.status_code == 400  # missing attestation fields


def test_enrollment_rejects_replayed_challenge(tmp_path):
    app, _ = _attest_app(tmp_path, _FakeVerifier(ok=True))
    client = TestClient(app)
    challenge = client.get("/v1/attest/challenge").json()["challenge"]
    body = {"app": "fetch-ios", "attestation": "AAAA", "key_id": "k1", "challenge": challenge}
    assert client.post("/v1/agents/register", json=body).status_code == 200
    assert client.post("/v1/agents/register", json=body).status_code == 400  # reused challenge


def test_enrollment_rejects_invalid_attestation(tmp_path):
    app, _ = _attest_app(tmp_path, _FakeVerifier(ok=False))
    client = TestClient(app)
    challenge = client.get("/v1/attest/challenge").json()["challenge"]
    res = client.post("/v1/agents/register", json={
        "app": "fetch-ios", "attestation": "AAAA", "key_id": "k1", "challenge": challenge})
    assert res.status_code == 403


def test_enrollment_enforces_per_key_cap(tmp_path):
    app, _ = _attest_app(tmp_path, _FakeVerifier(ok=True), max_per_key=1)
    client = TestClient(app)
    for expected in (200, 429):
        challenge = client.get("/v1/attest/challenge").json()["challenge"]
        res = client.post("/v1/agents/register", json={
            "app": "fetch-ios", "attestation": "AAAA", "key_id": "k1", "challenge": challenge})
        assert res.status_code == expected


def test_push_service_routes_by_platform():
    from push_relay.app import PushService
    class StubSender:
        async def send(self, device, payload): pass
    svc = PushService.__new__(PushService)          # bypass __init__
    svc._senders = {"ios": StubSender()}
    assert svc.sender_for("ios") is svc._senders["ios"]
    assert svc.sender_for("android") is None         # no sender yet → None (caller skips + logs)


def test_pairing_endpoint_requires_auth_and_rotates() -> None:
    client, _ = _client()
    reg = client.post("/v1/agents/register", json={"app": "test"}).json()
    headers = {
        "X-Hermes-Agent-Id": reg["agent_id"],
        "Authorization": f"Bearer {reg['agent_secret']}",
    }
    assert reg["pairing_secret"]  # minted alongside the agent at registration

    # Unauthenticated callers can't mint a capability token.
    assert client.post("/v1/agents/pairing").status_code == 401

    # Authenticated → mints a fresh token, distinct from the registration one.
    res = client.post("/v1/agents/pairing", headers=headers)
    assert res.status_code == 200
    rotated = res.json()["pairing_secret"]
    assert rotated and rotated != reg["pairing_secret"]
