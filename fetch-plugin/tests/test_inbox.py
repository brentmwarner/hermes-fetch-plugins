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
