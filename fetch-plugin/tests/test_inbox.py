"""Tests for Fetch owning the inbox delivery target.

Fetch is the single first-class plugin: the only delivery platform is ``fetch``
and the only env vars are ``HERMES_FETCH_*``. There is no separate
``hermes_inbox`` product to normalize, prune, or accept.
"""

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
ALIASES = "channel_aliases.json"


def _load_inbox():
    spec = importlib.util.spec_from_file_location(
        "fetch_plugin_inbox_test", PLUGIN_DIR / "_inbox.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_seed_creates_fetch_alias(tmp_path, monkeypatch):
    inbox = _load_inbox()
    monkeypatch.setattr(inbox, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_FETCH_HOME_CHANNEL", "default")

    inbox.seed_channel_alias()

    data = json.loads((tmp_path / ALIASES).read_text(encoding="utf-8"))
    assert data == {"fetch": {"default": "Fetch"}}


def test_seed_preserves_other_platform_aliases(tmp_path, monkeypatch):
    """Seeding only manages the ``fetch`` key — every other platform's aliases
    are left exactly as they were."""
    inbox = _load_inbox()
    monkeypatch.setattr(inbox, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_FETCH_HOME_CHANNEL", "default")
    existing = {"telegram": {"6927549812": "Brent"}}
    (tmp_path / ALIASES).write_text(json.dumps(existing), encoding="utf-8")

    inbox.seed_channel_alias()

    data = json.loads((tmp_path / ALIASES).read_text(encoding="utf-8"))
    assert data == {
        "telegram": {"6927549812": "Brent"},
        "fetch": {"default": "Fetch"},
    }


def test_seed_prunes_stale_fetch_home_alias_after_home_channel_change(tmp_path, monkeypatch):
    """A stale auto-generated ``fetch`` alias for a previous home channel
    (``leads``) is pruned when the home channel changes; user-renamed aliases
    (value != "Fetch") are preserved."""
    inbox = _load_inbox()
    monkeypatch.setattr(inbox, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_FETCH_HOME_CHANNEL", "default")
    existing = {"fetch": {"leads": "Fetch", "custom": "My Phone"}}
    (tmp_path / ALIASES).write_text(json.dumps(existing), encoding="utf-8")

    inbox.seed_channel_alias()

    data = json.loads((tmp_path / ALIASES).read_text(encoding="utf-8"))
    assert data == {"fetch": {"custom": "My Phone", "default": "Fetch"}}


def test_env_enablement_uses_fetch_home_channel(monkeypatch):
    inbox = _load_inbox()
    monkeypatch.setenv("HERMES_FETCH_DELIVERY_ENABLED", "1")
    monkeypatch.setenv("HERMES_FETCH_HOME_CHANNEL", "leads")

    assert inbox.env_enablement() == {
        "home_channel": {"chat_id": "leads", "name": "Fetch"},
        "channel": "leads",
    }


def test_set_delivery_enabled_persists_flag_and_channel(monkeypatch, tmp_path):
    """set_delivery_enabled persists both the on/off flag and the home channel,
    and seeds the fetch alias for the new home channel."""
    inbox = _load_inbox()
    # Register the keys so monkeypatch deletes them on teardown even though
    # set_delivery_enabled writes os.environ directly.
    monkeypatch.setenv("HERMES_FETCH_DELIVERY_ENABLED", "0")
    monkeypatch.setenv("HERMES_FETCH_HOME_CHANNEL", "default")
    monkeypatch.setattr(inbox, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(inbox, "_store_home", lambda: tmp_path)
    saved = {}
    import hermes_cli.config as cfg
    # conftest stubs hermes_cli.config; inject save_env_value so the production
    # `from hermes_cli.config import save_env_value` resolves to our spy.
    monkeypatch.setattr(cfg, "save_env_value", lambda k, v: saved.__setitem__(k, v), raising=False)

    inbox.set_delivery_enabled(True, channel="leads")

    assert inbox.is_delivery_enabled() is True
    assert saved == {
        "HERMES_FETCH_DELIVERY_ENABLED": "1",
        "HERMES_FETCH_HOME_CHANNEL": "leads",
    }
    data = json.loads((tmp_path / ALIASES).read_text(encoding="utf-8"))
    assert data["fetch"]["leads"] == "Fetch"


def test_standalone_send_delivers_to_fetch_inbox(monkeypatch):
    inbox = _load_inbox()
    calls = []
    monkeypatch.setattr(
        inbox,
        "deliver_to_inbox",
        lambda **kw: calls.append(kw) or inbox.InboxDelivery(session_id="inbox_default", message_id=7),
    )

    result = asyncio.run(inbox.standalone_send(None, "default", "hello"))

    assert calls == [{"channel": "default", "content": "hello", "title": "Fetch", "thread_id": None}]
    assert result == {"success": True, "message_id": "7", "session_id": "inbox_default"}


def test_standalone_send_routes_named_channel(monkeypatch):
    """`fetch:researcher` routes to the researcher DM with a real title."""
    inbox = _load_inbox()
    calls = []
    monkeypatch.setattr(
        inbox,
        "deliver_to_inbox",
        lambda **kw: calls.append(kw) or inbox.InboxDelivery(session_id="inbox_researcher", message_id=9),
    )

    result = asyncio.run(inbox.standalone_send(None, "fetch:researcher", "standup"))

    assert calls == [{"channel": "researcher", "content": "standup", "title": "Researcher", "thread_id": None}]
    assert result["session_id"] == "inbox_researcher"


def test_adapter_get_chat_info_returns_basic_descriptor(monkeypatch):
    inbox = _load_inbox()
    adapter = object.__new__(inbox.FetchInboxAdapter)
    monkeypatch.setenv("HERMES_FETCH_HOME_CHANNEL", "default")

    default = asyncio.run(adapter.get_chat_info("fetch"))
    researcher = asyncio.run(adapter.get_chat_info("fetch:researcher"))

    assert default == {"name": "Fetch", "type": "dm"}
    assert researcher == {"name": "Researcher", "type": "dm"}


def test_adapter_connect_accepts_reconnect_keyword():
    inbox = _load_inbox()
    adapter = object.__new__(inbox.FetchInboxAdapter)
    calls = []
    adapter._mark_connected = lambda: calls.append("connected")

    assert asyncio.run(adapter.connect(is_reconnect=True)) is True
    assert calls == ["connected"]


def test_adapter_get_chat_info_preserves_title_for_custom_home_channel(monkeypatch):
    """When HERMES_FETCH_HOME_CHANNEL is a non-default slug, get_chat_info should
    still return DEFAULT_TITLE (not the title-cased slug) for the home channel."""
    inbox = _load_inbox()
    adapter = object.__new__(inbox.FetchInboxAdapter)
    monkeypatch.setenv("HERMES_FETCH_HOME_CHANNEL", "leads")

    home = asyncio.run(adapter.get_chat_info("fetch"))
    researcher = asyncio.run(adapter.get_chat_info("fetch:researcher"))

    assert home == {"name": "Fetch", "type": "dm"}
    assert researcher == {"name": "Researcher", "type": "dm"}


def test_standalone_send_titles_home_cron_delivery_from_job_name(monkeypatch):
    inbox = _load_inbox()
    calls = []
    body = "Cronjob Response: Morning Brief\n(job_id: abc123)\n\nWeather and inbox summary"
    monkeypatch.setattr(
        inbox,
        "deliver_to_inbox",
        lambda **kw: calls.append(kw) or inbox.InboxDelivery(session_id="inbox_cron-abc123", message_id=11),
    )

    result = asyncio.run(inbox.standalone_send(None, "default", body))

    assert calls == [{"channel": "cron-abc123", "content": body, "title": "Morning Brief", "thread_id": None}]
    assert result["session_id"] == "inbox_cron-abc123"


def test_standalone_send_preserves_cron_channel_across_chunks(monkeypatch):
    """When Hermes chunks a long cron response, only the first chunk has the
    'Cronjob Response...' header. Subsequent chunks must still route to the
    same cron thread via the process-level cache."""
    inbox = _load_inbox()
    calls = []
    monkeypatch.setattr(
        inbox,
        "deliver_to_inbox",
        lambda **kw: calls.append(kw) or inbox.InboxDelivery(session_id="inbox_cron-abc123", message_id=1),
    )

    first_chunk = "Cronjob Response: Morning Brief\n(job_id: abc123)\n\nStart of a very long summary..."
    second_chunk = "...continuation of the summary without a cron header..."

    asyncio.run(inbox.standalone_send(None, "default", first_chunk))
    asyncio.run(inbox.standalone_send(None, "default", second_chunk))

    assert len(calls) == 2
    assert calls[0]["channel"] == "cron-abc123"
    assert calls[1]["channel"] == "cron-abc123", (
        "second chunk must reuse the cached cron channel, not fall back to home"
    )


def test_title_from_metadata_ignores_thread_id():
    """_title_from_metadata must NOT fall back to metadata['thread_id'];
    that value is handled by _thread_id_from_metadata and
    _default_title_for_delivery which produce a cleaned label."""
    inbox = _load_inbox()
    assert inbox._title_from_metadata({"thread_id": "my-thread"}) is None
    assert inbox._title_from_metadata({"title": "My Title"}) == "My Title"
    assert inbox._title_from_metadata({"title": "My Title", "thread_id": "t1"}) == "My Title"


def test_deliver_to_inbox_passes_source_inbox(monkeypatch):
    """The proactive push must carry source='inbox' so the device routes it
    into the phone-owned inbox (iOS inboxSources allowlist)."""
    inbox = _load_inbox()
    relay_calls = []
    fake_relay = type("R", (), {"send_event_background": staticmethod(lambda **kw: relay_calls.append(kw))})
    monkeypatch.setattr(inbox, "_load_relay", lambda: fake_relay)
    monkeypatch.setattr(inbox, "SessionDB", lambda **kw: _FakeDB())

    inbox.deliver_to_inbox(channel="default", content="hi", title="Fetch")

    assert relay_calls and relay_calls[0]["source"] == "inbox"


def test_deliver_to_inbox_card_push_body_is_readable(monkeypatch):
    inbox = _load_inbox()
    relay_calls = []
    fake_relay = type("R", (), {"send_event_background": staticmethod(lambda **kw: relay_calls.append(kw))})
    monkeypatch.setattr(inbox, "_load_relay", lambda: fake_relay)
    monkeypatch.setattr(inbox, "SessionDB", lambda **kw: _FakeDB())
    body = """
Cronjob Response: Morning World Cup
(job_id: abc123)

```card
{"title":"World Cup Brief","subtitle":"Today at a glance","stats":[{"label":"Matches","value":2}],"items":[{"title":"Spain vs Japan","subtitle":"9 AM ET"}]}
```
"""

    inbox.deliver_to_inbox(channel="default", content=body, title="Morning World Cup")

    assert relay_calls[0]["body"] == "World Cup Brief, Today at a glance, Matches 2, Spain vs Japan, 9 AM ET"
    assert "{" not in relay_calls[0]["body"]
    assert relay_calls[0]["source"] == "inbox"


def test_deliver_to_inbox_uses_store_home_override(monkeypatch, tmp_path):
    """A delivery under a worker profile persists into the override home's db."""
    inbox = _load_inbox()
    relay_home = tmp_path / "relay"
    relay_home.mkdir()
    monkeypatch.setattr(inbox, "get_hermes_home", lambda: tmp_path / "worker")
    monkeypatch.setenv("HERMES_FETCH_STORE_HOME", str(relay_home))
    opened = []
    monkeypatch.setattr(inbox, "SessionDB", lambda **kw: opened.append(kw.get("db_path")) or _FakeDB())
    monkeypatch.setattr(inbox, "_notify_proactive", lambda **kw: None)

    inbox.deliver_to_inbox(channel="researcher", content="hi", title="Researcher")

    assert opened == [relay_home / "state.db"]


def test_deliver_to_inbox_routes_home_cron_delivery_to_job_thread(monkeypatch):
    inbox = _load_inbox()
    captured = {}
    body = "Cronjob Response: Morning Brief\n(job_id: abc123)\n\nWeather and inbox summary"

    class _CaptureDB:
        def create_session(self, **kw): captured["create"] = kw
        def reopen_session(self, sid): captured["reopen"] = sid
        def set_session_title(self, sid, title): captured["title"] = (sid, title)
        def append_message(self, **kw): captured["append"] = kw; return 1
        def close(self): pass

    notify_calls = []
    monkeypatch.setattr(inbox, "SessionDB", lambda **kw: _CaptureDB())
    monkeypatch.setattr(inbox, "_notify_proactive", lambda **kw: notify_calls.append(kw))

    delivery = inbox.deliver_to_inbox(channel="default", content=body, title="Fetch")

    assert delivery.session_id == "inbox_cron-abc123"
    assert captured["create"]["user_id"] == "cron-abc123"
    assert captured["title"] == ("inbox_cron-abc123", "Morning Brief")
    assert notify_calls[0]["title"] == "Morning Brief"


def test_deliver_to_inbox_preserves_explicit_agent_channel_for_cron_body(monkeypatch):
    inbox = _load_inbox()
    captured = {}
    body = "Cronjob Response: Morning Brief\n(job_id: abc123)\n\nWeather and inbox summary"

    class _CaptureDB:
        def create_session(self, **kw): captured["create"] = kw
        def reopen_session(self, sid): pass
        def set_session_title(self, sid, title): captured["title"] = (sid, title)
        def append_message(self, **kw): return 1
        def close(self): pass

    monkeypatch.setattr(inbox, "SessionDB", lambda **kw: _CaptureDB())
    monkeypatch.setattr(inbox, "_notify_proactive", lambda **kw: None)

    delivery = inbox.deliver_to_inbox(
        channel="fetch:researcher",
        content=body,
        title="Researcher",
    )

    assert delivery.session_id == "inbox_researcher"
    assert captured["create"]["user_id"] == "researcher"
    assert captured["title"] == ("inbox_researcher", "Researcher")


def test_label_for_channel_titles_profile_names():
    inbox = _load_inbox()
    assert inbox._label_for_channel("default") == "Fetch"
    assert inbox._label_for_channel("researcher") == "Researcher"
    assert inbox._label_for_channel("code_reviewer") == "Code Reviewer"


def test_bare_fetch_routes_to_configured_home_channel(monkeypatch):
    """A bare `fetch` target uses HERMES_FETCH_HOME_CHANNEL, not hard-coded
    `default` — so a customized home channel (e.g. `leads`) receives bare
    sends, matching what env_enablement() advertises."""
    inbox = _load_inbox()
    monkeypatch.setenv("HERMES_FETCH_HOME_CHANNEL", "leads")
    assert inbox._channel_from_chat_id("fetch") == "leads"
    assert inbox._channel_from_chat_id(None) == "leads"
    assert inbox._channel_from_chat_id("") == "leads"
    assert inbox._channel_from_chat_id("fetch:") == "leads"
    assert inbox._session_id_for_channel(inbox._channel_from_chat_id("fetch")) == "inbox_leads"


def test_deliver_to_inbox_strips_platform_prefix_for_direct_callers(monkeypatch):
    """Direct callers passing `fetch:researcher` land in inbox_researcher, not
    inbox_fetch-researcher."""
    inbox = _load_inbox()
    monkeypatch.setattr(inbox, "SessionDB", lambda **kw: _FakeDB())
    monkeypatch.setattr(inbox, "_notify_proactive", lambda **kw: None)
    delivery = inbox.deliver_to_inbox(channel="fetch:researcher", content="hi", title="Researcher")
    assert delivery.session_id == "inbox_researcher"


def test_repeated_deliveries_to_same_slug_reuse_same_session(monkeypatch):
    inbox = _load_inbox()
    created = []

    class _CaptureDB(_FakeDB):
        def create_session(self, **kw):
            created.append(kw["session_id"])

    monkeypatch.setattr(inbox, "SessionDB", lambda **kw: _CaptureDB())
    monkeypatch.setattr(inbox, "_notify_proactive", lambda **kw: None)

    first = inbox.deliver_to_inbox(channel="World Cup", content="match one", title="World Cup")
    second = inbox.deliver_to_inbox(channel="World Cup", content="match two", title="World Cup")

    assert first.session_id == second.session_id == "inbox_world-cup"
    assert created == ["inbox_world-cup", "inbox_world-cup"]


def test_seed_includes_per_agent_profile_aliases(monkeypatch, tmp_path):
    inbox = _load_inbox()
    home = tmp_path
    (home / "profiles").mkdir()
    (home / "profiles" / "researcher").mkdir()
    (home / "profiles" / "coder").mkdir()
    monkeypatch.setattr(inbox, "get_hermes_home", lambda: home)
    monkeypatch.setattr(inbox, "_store_home", lambda: home)
    monkeypatch.setenv("HERMES_FETCH_HOME_CHANNEL", "default")

    inbox.seed_channel_alias()

    data = json.loads((home / ALIASES).read_text(encoding="utf-8"))
    entries = data["fetch"]
    assert entries["default"] == "Fetch"
    assert entries["researcher"] == "Researcher"
    assert entries["coder"] == "Coder"


class _FakeDB:
    def __init__(self, *a, **kw): pass
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def create_session(self, **kw): pass
    def reopen_session(self, sid): pass
    def set_session_title(self, sid, title): pass
    def append_message(self, **kw): return 1
    def close(self): pass
