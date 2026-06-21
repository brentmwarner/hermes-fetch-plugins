from __future__ import annotations

import pytest

from push_relay.app import APNsResult, Device, PushService, RelayStore, Settings


class _CapturingSender:
    """Fake ios sender that records the collapse_id it is handed per push."""

    def __init__(self) -> None:
        self.collapse_ids: list[str | None] = []

    async def send(self, *, device: Device, payload: dict, collapse_id: str | None) -> APNsResult:
        self.collapse_ids.append(collapse_id)
        return APNsResult(ok=True, status=200, reason=None, should_prune=False)


@pytest.fixture
def service(tmp_path):
    settings = Settings(
        database_path=tmp_path / "relay.db",
        apns_key_pem=None,
        apns_key_id=None,
        apns_team_id=None,
        registration_token=None,
        allow_custom_body=True,
    )
    store = RelayStore(settings.database_path)
    agent_id, _secret = store.register_agent(app="fetch-ios", agent_version=None)
    store.upsert_device(
        agent_id=agent_id,
        token="tok-1",
        platform="ios",
        environment="sandbox",
        bundle_id="com.brentwarner.fetch",
        preferences={"replies": True, "sound": True},
    )
    # apns= registers the fake as the "ios" sender (same wiring _client uses).
    sender = _CapturingSender()
    svc = PushService(store=store, settings=settings, apns=sender)  # type: ignore[arg-type]
    return svc, sender, agent_id


@pytest.mark.asyncio
async def test_two_replies_same_session_do_not_collapse(service):
    svc, sender, agent_id = service
    for body in ("first reply", "second reply"):
        await svc.send_event(
            agent_id=agent_id,
            kind="replies",
            session_id="20260616_120000_abcd1234",
            title="Fetch replied",
            body=body,
        )
    # Each reply must be its own notification: APNs coalesces pushes that share an
    # apns-collapse-id, so a per-session collapse id makes only the first reply
    # alert. Grouping is handled by aps "thread-id" instead (see _payload), so the
    # relay must pass collapse_id=None for every push.
    assert sender.collapse_ids == [None, None]
