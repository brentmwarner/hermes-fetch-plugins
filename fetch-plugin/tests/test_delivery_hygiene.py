"""Delivery hygiene for the Fetch inbox.

Four behaviors that keep the phone-owned inbox trustworthy:

* The chunk-carrying cron-channel cache must be a short-lived carry, not a
  process-lifetime redirect — after the TTL, plain home deliveries go home.
* Gateway lifecycle notices ("⚠️ Gateway shutting down…") are transport
  control-flow, not messages someone sent the user; they must not be
  persisted into a thread or pushed to the phone.
* Approval pushes must carry the app's own source ("fetch") for Fetch-channel
  session keys, so the device can thread them, while foreign surfaces keep
  their platform tag.
* The per-process session-source memo must stay bounded.
"""

import asyncio
import importlib.util
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]


def _load_inbox():
    spec = importlib.util.spec_from_file_location(
        "fetch_plugin_inbox_hygiene_test", PLUGIN_DIR / "_inbox.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_plugin():
    spec = importlib.util.spec_from_file_location(
        "fetch_plugin_hygiene_test", PLUGIN_DIR / "__init__.py"
    )
    assert spec is not None and spec.loader is not None
    plugin = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = plugin
    spec.loader.exec_module(plugin)
    return plugin


class _FakeDB:
    def __init__(self):
        self.created = []
        self.reopened = []
        self.titles = []
        self.appended = []

    def create_session(self, **kw):
        self.created.append(kw)

    def reopen_session(self, session_id):
        self.reopened.append(session_id)

    def set_session_title(self, session_id, title):
        self.titles.append((session_id, title))

    def append_message(self, **kw):
        self.appended.append(kw)
        return len(self.appended)

    def close(self):
        pass


# --- cron-channel cache TTL -------------------------------------------------

_CRON_CHUNK1 = "Cronjob Response: Morning Brief\n(job_id: abc123)\n\nStart of a long summary..."
_PLAIN = "A plain proactive message with no cron header."


def _capture_deliveries(monkeypatch, inbox):
    calls = []
    monkeypatch.setattr(
        inbox,
        "deliver_to_inbox",
        lambda **kw: calls.append(kw)
        or inbox.InboxDelivery(session_id="inbox_test", message_id=1),
    )
    return calls


def test_cron_channel_cache_expires_after_ttl(monkeypatch):
    """A cron delivery must not permanently capture the home channel: once the
    chunk-carry TTL has passed, a headerless home delivery goes to the home
    thread, not the last cron thread."""
    inbox = _load_inbox()
    calls = _capture_deliveries(monkeypatch, inbox)
    now = [1000.0]
    monkeypatch.setattr(inbox, "_now", lambda: now[0], raising=False)

    asyncio.run(inbox.standalone_send(None, "default", _CRON_CHUNK1))
    now[0] += getattr(inbox, "_CRON_CACHE_TTL", 180.0) + 1.0
    asyncio.run(inbox.standalone_send(None, "default", _PLAIN))

    assert calls[0]["channel"] == "cron-abc123"
    assert calls[1]["channel"] == "default", (
        "a stale cron-channel cache entry must not reroute later home deliveries"
    )


def test_cron_channel_cache_carries_chunks_within_ttl(monkeypatch):
    """Within the TTL the cache still does its job: a headerless second chunk
    of the same delivery follows the first chunk's cron thread."""
    inbox = _load_inbox()
    calls = _capture_deliveries(monkeypatch, inbox)
    now = [1000.0]
    monkeypatch.setattr(inbox, "_now", lambda: now[0], raising=False)

    asyncio.run(inbox.standalone_send(None, "default", _CRON_CHUNK1))
    now[0] += 2.0
    asyncio.run(inbox.standalone_send(None, "default", "...continuation without header..."))

    assert calls[0]["channel"] == "cron-abc123"
    assert calls[1]["channel"] == "cron-abc123"


# --- gateway lifecycle notice suppression ------------------------------------

_SHUTDOWN_NOTICE = "⚠️ Gateway shutting down — Your current task will be interrupted."
_RESTART_NOTICE = (
    "⚠️ Gateway restarting — Your current task will be interrupted. "
    "Send any message after restart and I'll try to resume where you left off."
)


def _wire_fake_db_and_relay(monkeypatch, inbox):
    db = _FakeDB()
    relay_calls = []
    fake_relay = type(
        "R", (), {"send_event_background": staticmethod(lambda **kw: relay_calls.append(kw))}
    )
    monkeypatch.setattr(inbox, "SessionDB", lambda **kw: db)
    monkeypatch.setattr(inbox, "_load_relay", lambda: fake_relay)
    return db, relay_calls


def test_gateway_shutdown_notice_is_not_persisted_or_pushed(monkeypatch):
    inbox = _load_inbox()
    db, relay_calls = _wire_fake_db_and_relay(monkeypatch, inbox)

    result = asyncio.run(inbox.standalone_send(None, "default", _SHUTDOWN_NOTICE))

    assert result["success"] is True, "the gateway send must still report success"
    assert db.appended == [], "a shutdown notice must not become a thread message"
    assert relay_calls == [], "a shutdown notice must not ring the phone"


def test_gateway_restart_notice_is_not_persisted_or_pushed(monkeypatch):
    inbox = _load_inbox()
    db, relay_calls = _wire_fake_db_and_relay(monkeypatch, inbox)

    result = asyncio.run(inbox.standalone_send(None, "default", _RESTART_NOTICE))

    assert result["success"] is True
    assert db.appended == []
    assert relay_calls == []


def test_other_warning_messages_still_deliver(monkeypatch):
    """Only the gateway lifecycle notices are control-flow; any other ⚠️
    message is real content and must deliver + push as usual."""
    inbox = _load_inbox()
    db, relay_calls = _wire_fake_db_and_relay(monkeypatch, inbox)

    result = asyncio.run(
        inbox.standalone_send(None, "default", "⚠️ Disk almost full on the build machine.")
    )

    assert result["success"] is True
    assert len(db.appended) == 1
    assert len(relay_calls) == 1


# --- approval push source mapping --------------------------------------------


def _capture_pushes(monkeypatch, plugin):
    captured = []
    monkeypatch.setattr(
        plugin._relay,
        "send_event_background",
        lambda **kw: captured.append(kw),
    )
    return captured


def test_approval_push_maps_untagged_gateway_to_fetch_source(monkeypatch):
    """An untagged gateway session (empty platform segment) is a Fetch app
    conversation per FETCH_CHANNELS; its approval push must say source=fetch
    so the device can thread it into the inbox."""
    plugin = _load_plugin()
    captured = _capture_pushes(monkeypatch, plugin)

    plugin._on_pre_approval_request(
        command="rm -rf build", description="Delete build dir",
        session_key="agent:main::private:abc123",
    )

    assert len(captured) == 1
    assert captured[0]["source"] == "fetch"


def test_approval_push_keeps_foreign_platform_source(monkeypatch):
    """A Telegram approval keeps its platform tag — the device decides what to
    do with foreign surfaces; we must not launder them into Fetch threads."""
    plugin = _load_plugin()
    captured = _capture_pushes(monkeypatch, plugin)

    plugin._on_pre_approval_request(
        command="ls", description="List files",
        session_key="agent:main:telegram:private:6927549812",
    )

    assert len(captured) == 1
    assert captured[0]["source"] == "telegram"


# --- session-source memo bound ------------------------------------------------


def test_session_source_cache_is_bounded(monkeypatch):
    """The per-process source memo must not grow without bound in a long-lived
    gateway process."""
    plugin = _load_plugin()

    class _DB:
        def __init__(self, *a, **kw):
            pass

        def get_session(self, session_id):
            return {"source": "fetch"}

        def close(self):
            pass

    monkeypatch.setattr(sys.modules["hermes_state"], "SessionDB", _DB)
    plugin._SESSION_SOURCE_CACHE.clear()

    for i in range(600):
        plugin._session_source(f"session-{i}")

    assert len(plugin._SESSION_SOURCE_CACHE) <= 512
