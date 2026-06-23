"""Tests for Fetch owning the inbox delivery target."""

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
    monkeypatch.setenv("HERMES_INBOX_HOME_CHANNEL", "default")

    inbox.seed_channel_alias()

    data = json.loads((tmp_path / ALIASES).read_text(encoding="utf-8"))
    assert data == {"fetch": {"default": "Fetch"}}


def test_seed_prunes_legacy_auto_alias(tmp_path, monkeypatch):
    inbox = _load_inbox()
    monkeypatch.setattr(inbox, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_INBOX_HOME_CHANNEL", "default")
    existing = {
        "telegram": {"6927549812": "Brent"},
        "hermes_inbox": {"default": "Fetch", "custom": "My Phone"},
    }
    (tmp_path / ALIASES).write_text(json.dumps(existing), encoding="utf-8")

    inbox.seed_channel_alias()

    data = json.loads((tmp_path / ALIASES).read_text(encoding="utf-8"))
    assert data == {
        "telegram": {"6927549812": "Brent"},
        "hermes_inbox": {"custom": "My Phone"},
        "fetch": {"default": "Fetch"},
    }


def test_seed_keeps_legacy_alias_when_legacy_platform_enabled(tmp_path, monkeypatch):
    inbox = _load_inbox()
    monkeypatch.setattr(inbox, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_INBOX_HOME_CHANNEL", "default")
    monkeypatch.setenv("HERMES_INBOX_REGISTER_LEGACY_PLATFORM", "1")
    existing = {"hermes_inbox": {"default": "Fetch"}}
    (tmp_path / ALIASES).write_text(json.dumps(existing), encoding="utf-8")

    inbox.seed_channel_alias()

    data = json.loads((tmp_path / ALIASES).read_text(encoding="utf-8"))
    assert data == {
        "hermes_inbox": {"default": "Fetch"},
        "fetch": {"default": "Fetch"},
    }


def test_env_enablement_uses_fetch_home_channel(monkeypatch):
    inbox = _load_inbox()
    monkeypatch.setenv("HERMES_INBOX_ENABLED", "1")
    monkeypatch.setenv("HERMES_INBOX_HOME_CHANNEL", "leads")

    assert inbox.env_enablement() == {
        "home_channel": {"chat_id": "leads", "name": "Fetch"},
        "channel": "leads",
    }


def test_standalone_send_delivers_to_fetch_inbox(monkeypatch):
    inbox = _load_inbox()
    calls = []
    monkeypatch.setattr(
        inbox,
        "deliver_to_inbox",
        lambda **kw: calls.append(kw) or inbox.InboxDelivery(session_id="inbox_default", message_id=7),
    )

    result = asyncio.run(inbox.standalone_send(None, "default", "hello"))

    assert calls == [{"channel": "default", "content": "hello", "title": "Fetch"}]
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

    assert calls == [{"channel": "researcher", "content": "standup", "title": "Researcher"}]
    assert result["session_id"] == "inbox_researcher"


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


def test_deliver_to_inbox_uses_store_home_override(monkeypatch, tmp_path):
    """A delivery under a worker profile persists into the override home's db."""
    inbox = _load_inbox()
    relay_home = tmp_path / "relay"
    relay_home.mkdir()
    monkeypatch.setattr(inbox, "get_hermes_home", lambda: tmp_path / "worker")
    monkeypatch.setenv("HERMES_INBOX_STORE_HOME", str(relay_home))
    opened = []
    monkeypatch.setattr(inbox, "SessionDB", lambda **kw: opened.append(kw.get("db_path")) or _FakeDB())
    monkeypatch.setattr(inbox, "_notify_proactive", lambda **kw: None)

    inbox.deliver_to_inbox(channel="researcher", content="hi", title="Researcher")

    assert opened == [relay_home / "state.db"]


def test_label_for_channel_titles_profile_names():
    inbox = _load_inbox()
    assert inbox._label_for_channel("default") == "Fetch"
    assert inbox._label_for_channel("researcher") == "Researcher"
    assert inbox._label_for_channel("code_reviewer") == "Code Reviewer"


def test_seed_includes_per_agent_profile_aliases(monkeypatch, tmp_path):
    inbox = _load_inbox()
    home = tmp_path
    (home / "profiles").mkdir()
    (home / "profiles" / "researcher").mkdir()
    (home / "profiles" / "coder").mkdir()
    monkeypatch.setattr(inbox, "get_hermes_home", lambda: home)
    monkeypatch.setattr(inbox, "_store_home", lambda: home)
    monkeypatch.setenv("HERMES_INBOX_HOME_CHANNEL", "default")

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
