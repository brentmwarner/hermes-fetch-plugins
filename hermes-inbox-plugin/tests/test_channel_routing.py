"""Per-agent DM channel routing for the Hermes Inbox plugin.

`hermes_inbox:<channel>` delivery targets produce per-agent sessions
(`inbox_researcher`, `inbox_coder`) so each agent gets its own Fetch DM instead
of one pooled `inbox_default` thread. These tests cover the channel parsing,
the per-agent session id, and the store-home override that keeps a worker
profile's deliveries visible to the relay-paired home the phone reads.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]


def _load_plugin():
    spec = importlib.util.spec_from_file_location(
        "hermes_inbox_plugin_channels", PLUGIN_DIR / "__init__.py"
    )
    assert spec is not None and spec.loader is not None
    plugin = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = plugin
    spec.loader.exec_module(plugin)
    return plugin


def test_channel_target_routes_to_named_session(monkeypatch):
    plugin = _load_plugin()
    # Stub SessionDB so no real db is opened; capture the session_id + source used.
    captured = {}

    class _FakeDB:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def create_session(self, **kw): captured["create"] = kw
        def reopen_session(self, sid): pass
        def set_session_title(self, sid, title): pass
        def append_message(self, **kw): return 1
        def close(self): pass

    monkeypatch.setattr(plugin, "SessionDB", lambda **kw: _FakeDB())
    monkeypatch.setattr(plugin, "_notify_proactive", lambda **kw: None)

    delivery = plugin.deliver_to_inbox(channel="researcher", content="standup ready", title="Researcher")
    assert delivery.session_id == "inbox_researcher"
    assert captured["create"]["source"] == "inbox"
    assert captured["create"]["user_id"] == "researcher"


def test_default_channel_unchanged(monkeypatch):
    plugin = _load_plugin()

    class _FakeDB:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def create_session(self, **kw): pass
        def reopen_session(self, sid): pass
        def set_session_title(self, sid, title): pass
        def append_message(self, **kw): return 1
        def close(self): pass

    monkeypatch.setattr(plugin, "SessionDB", lambda **kw: _FakeDB())
    monkeypatch.setattr(plugin, "_notify_proactive", lambda **kw: None)

    delivery = plugin.deliver_to_inbox(channel="default", content="hi", title="Fetch Inbox")
    assert delivery.session_id == "inbox_default"


def test_default_cron_delivery_routes_to_job_thread(monkeypatch):
    plugin = _load_plugin()
    captured = {}
    body = "Cronjob Response: Morning Brief\n(job_id: abc123)\n\nWeather and inbox summary"

    class _FakeDB:
        def create_session(self, **kw): captured["create"] = kw
        def reopen_session(self, sid): pass
        def set_session_title(self, sid, title): captured["title"] = (sid, title)
        def append_message(self, **kw): return 1
        def close(self): pass

    notify_calls = []
    monkeypatch.setattr(plugin, "SessionDB", lambda **kw: _FakeDB())
    monkeypatch.setattr(plugin, "_notify_proactive", lambda **kw: notify_calls.append(kw))

    delivery = plugin.deliver_to_inbox(channel="default", content=body, title="Fetch Inbox")

    assert delivery.session_id == "inbox_cron-abc123"
    assert captured["create"]["user_id"] == "cron-abc123"
    assert captured["title"] == ("inbox_cron-abc123", "Morning Brief")
    assert notify_calls[0]["title"] == "Morning Brief"


def test_explicit_agent_channel_ignores_cron_wrapper(monkeypatch):
    plugin = _load_plugin()
    captured = {}
    body = "Cronjob Response: Morning Brief\n(job_id: abc123)\n\nWeather and inbox summary"

    class _FakeDB:
        def create_session(self, **kw): captured["create"] = kw
        def reopen_session(self, sid): pass
        def set_session_title(self, sid, title): captured["title"] = (sid, title)
        def append_message(self, **kw): return 1
        def close(self): pass

    monkeypatch.setattr(plugin, "SessionDB", lambda **kw: _FakeDB())
    monkeypatch.setattr(plugin, "_notify_proactive", lambda **kw: None)

    delivery = plugin.deliver_to_inbox(
        channel="hermes_inbox:researcher",
        content=body,
        title="Researcher",
    )

    assert delivery.session_id == "inbox_researcher"
    assert captured["create"]["user_id"] == "researcher"
    assert captured["title"] == ("inbox_researcher", "Researcher")


@pytest.mark.parametrize("chat_id,expected", [
    ("researcher", "inbox_researcher"),
    ("hermes_inbox:researcher", "inbox_researcher"),  # defensive: unsplit target
    ("", "inbox_default"),                             # bare empty → home
    ("hermes_inbox", "inbox_default"),                 # bare platform → home
    (None, "inbox_default"),
])
def test_channel_from_chat_id_parsing(chat_id, expected):
    plugin = _load_plugin()
    assert plugin._session_id_for_channel(plugin._channel_from_chat_id(chat_id)) == expected


def test_channel_from_chat_id_respects_configured_home_channel(monkeypatch):
    plugin = _load_plugin()
    monkeypatch.setenv("HERMES_INBOX_HOME_CHANNEL", "researcher")
    assert plugin._session_id_for_channel(plugin._channel_from_chat_id(None)) == "inbox_researcher"
    assert plugin._session_id_for_channel(plugin._channel_from_chat_id("hermes_inbox:")) == "inbox_researcher"


def test_normalize_channel_strips_platform_prefix(monkeypatch):
    plugin = _load_plugin()
    monkeypatch.setenv("HERMES_INBOX_HOME_CHANNEL", "default")
    assert plugin._normalize_channel("hermes_inbox:researcher") == "researcher"


def test_store_home_override_writes_to_relay_home(monkeypatch, tmp_path):
    """A delivery under a worker profile must persist into the override home,
    not the worker's HERMES_HOME, so it's visible to the relay-paired phone."""
    plugin = _load_plugin()
    relay_home = tmp_path / "relay_home"
    relay_home.mkdir()
    worker_home = tmp_path / "worker_home"
    worker_home.mkdir()

    monkeypatch.setattr(plugin, "get_hermes_home", lambda: worker_home)
    monkeypatch.setenv("HERMES_INBOX_STORE_HOME", str(relay_home))

    opened_at = []

    class _FakeDB:
        def __init__(self, **kw):
            opened_at.append(kw.get("db_path"))
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def create_session(self, **kw): pass
        def reopen_session(self, sid): pass
        def set_session_title(self, sid, title): pass
        def append_message(self, **kw): return 1
        def close(self): pass

    monkeypatch.setattr(plugin, "SessionDB", lambda **kw: _FakeDB(**kw))
    monkeypatch.setattr(plugin, "_notify_proactive", lambda **kw: None)

    plugin.deliver_to_inbox(channel="researcher", content="hi", title="Researcher")
    assert opened_at == [relay_home / "state.db"], \
        "delivery must open the override home's state.db, not the worker's"


def test_deliver_to_inbox_with_prefixed_channel_uses_named_session(monkeypatch):
    plugin = _load_plugin()
    captured = {}

    class _FakeDB:
        def create_session(self, **kw): captured["create"] = kw
        def reopen_session(self, sid): pass
        def set_session_title(self, sid, title): pass
        def append_message(self, **kw): return 1
        def close(self): pass

    monkeypatch.setattr(plugin, "SessionDB", lambda **kw: _FakeDB())
    monkeypatch.setattr(plugin, "_notify_proactive", lambda **kw: None)

    delivery = plugin.deliver_to_inbox(
        channel="hermes_inbox:researcher",
        content="hi",
        title="Researcher",
    )
    assert delivery.session_id == "inbox_researcher"
    assert captured["create"]["user_id"] == "researcher"


def test_store_home_defaults_to_hermes_home_when_unset(monkeypatch, tmp_path):
    plugin = _load_plugin()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(plugin, "get_hermes_home", lambda: home)
    monkeypatch.delenv("HERMES_INBOX_STORE_HOME", raising=False)
    assert plugin._store_home() == home


def test_seed_alias_includes_per_agent_channels(monkeypatch, tmp_path):
    """When profiles exist under the store home, the alias seed lists one
    target per profile so the agent sees each as an addressable DM."""
    import json
    plugin = _load_plugin()
    home = tmp_path
    (home / "profiles").mkdir()
    (home / "profiles" / "researcher").mkdir()
    (home / "profiles" / "coder").mkdir()
    monkeypatch.setattr(plugin, "get_hermes_home", lambda: home)
    monkeypatch.setattr(plugin, "_store_home", lambda: home)
    monkeypatch.setenv("HERMES_INBOX_HOME_CHANNEL", "default")

    plugin._seed_channel_alias()

    data = json.loads((home / "channel_aliases.json").read_text())
    entries = data["hermes_inbox"]
    assert entries["default"] == "Fetch"
    assert entries["researcher"] == "Researcher"
    assert entries["coder"] == "Coder"


def test_seed_alias_is_non_destructive_for_custom_profile_names(monkeypatch, tmp_path):
    """A user-renamed profile alias is preserved, not clobbered."""
    import json
    plugin = _load_plugin()
    home = tmp_path
    (home / "profiles").mkdir()
    (home / "profiles" / "researcher").mkdir()
    monkeypatch.setattr(plugin, "get_hermes_home", lambda: home)
    monkeypatch.setattr(plugin, "_store_home", lambda: home)
    monkeypatch.setenv("HERMES_INBOX_HOME_CHANNEL", "default")
    existing = {"hermes_inbox": {"default": "Fetch", "researcher": "My Researcher"}}
    (home / "channel_aliases.json").write_text(json.dumps(existing))

    plugin._seed_channel_alias()

    data = json.loads((home / "channel_aliases.json").read_text())
    assert data["hermes_inbox"]["researcher"] == "My Researcher", "custom name preserved"


def test_profile_label_title_cases_slug():
    plugin = _load_plugin()
    assert plugin._profile_label("researcher") == "Researcher"
    assert plugin._profile_label("code_reviewer") == "Code Reviewer"


def test_title_from_metadata_ignores_thread_id():
    """_title_from_metadata must NOT fall back to metadata['thread_id'];
    that value is handled by _thread_id_from_metadata and
    _default_title_for_delivery which produce a cleaned label."""
    plugin = _load_plugin()
    assert plugin._title_from_metadata({"thread_id": "my-thread"}) is None
    assert plugin._title_from_metadata({"title": "My Title"}) == "My Title"
    assert plugin._title_from_metadata({"title": "My Title", "thread_id": "t1"}) == "My Title"


def test_standalone_send_preserves_cron_channel_across_chunks(monkeypatch):
    """When Hermes chunks a long cron response, only the first chunk has the
    'Cronjob Response...' header. Subsequent chunks must still route to the
    same cron thread via the process-level cache."""
    import asyncio
    plugin = _load_plugin()
    calls = []
    monkeypatch.setattr(
        plugin,
        "deliver_to_inbox",
        lambda **kw: calls.append(kw) or plugin.InboxDelivery(session_id="inbox_cron-abc123", message_id=1),
    )

    first_chunk = "Cronjob Response: Morning Brief\n(job_id: abc123)\n\nStart of long summary..."
    second_chunk = "...continuation without a cron header..."

    asyncio.run(plugin._standalone_send(None, "default", first_chunk))
    asyncio.run(plugin._standalone_send(None, "default", second_chunk))

    assert len(calls) == 2
    assert calls[0]["channel"] == "cron-abc123"
    assert calls[1]["channel"] == "cron-abc123", (
        "second chunk must reuse the cached cron channel, not fall back to home"
    )
