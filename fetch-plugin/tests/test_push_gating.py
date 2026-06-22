"""Push gating + source passthrough for the Fetch plugin.

`post_llm_call` is gated to Fetch-channel sessions (the phone-owned inbox model):
a reply on Telegram/Discord/CLI is not a Fetch conversation, so it must not push
to the phone. `pre_approval_request` always pushes (the agent needs the user
regardless of surface). Both carry `source` so the device can route inbox
membership agent-agnostically. ``conftest.py`` stubs the hermes-agent modules
the plugin imports at load; here we load ``__init__.py`` by file path and
monkeypatch the relay client to capture pushes.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]


def _load_plugin():
    spec = importlib.util.spec_from_file_location(
        "fetch_plugin_gating_test", PLUGIN_DIR / "__init__.py"
    )
    assert spec is not None and spec.loader is not None
    plugin = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = plugin
    spec.loader.exec_module(plugin)
    return plugin


class _Sent:
    def __init__(self):
        self.calls = []

    def __call__(self, *, kind, session_id, title, body, source=None):
        self.calls.append({"kind": kind, "session_id": session_id,
                           "title": title, "body": body, "source": source})


@pytest.fixture
def sent(monkeypatch):
    captured = _Sent()
    plugin = _load_plugin()
    monkeypatch.setattr(plugin._relay, "send_event_background", captured)
    return plugin, captured


def _set_source(monkeypatch, plugin, source):
    """Stub the SessionDB source lookup to return a fixed source."""
    monkeypatch.setattr(plugin, "_session_source", lambda sid: source)


def test_telegram_reply_does_not_push(sent, monkeypatch):
    plugin, captured = sent
    _set_source(monkeypatch, plugin, "telegram")
    plugin._on_post_llm_call(session_id="s1", assistant_response="hi")
    assert captured.calls == [], "non-Fetch channel must not push"


def test_fetch_gateway_reply_pushes_with_empty_source(sent, monkeypatch):
    plugin, captured = sent
    _set_source(monkeypatch, plugin, "")  # untagged gateway chat = Fetch conversation
    plugin._on_post_llm_call(session_id="s1", assistant_response="hi")
    assert len(captured.calls) == 1
    assert captured.calls[0]["kind"] == "replies"
    assert captured.calls[0]["source"] == ""
    assert captured.calls[0]["session_id"] == "s1"


def test_inbox_channel_reply_pushes(sent, monkeypatch):
    plugin, captured = sent
    _set_source(monkeypatch, plugin, "inbox")
    plugin._on_post_llm_call(session_id="s1", assistant_response="hi")
    assert len(captured.calls) == 1
    assert captured.calls[0]["source"] == "inbox"


def test_cron_run_does_not_push(sent, monkeypatch):
    plugin, captured = sent
    _set_source(monkeypatch, plugin, "cron")  # a cron RUN, not a Fetch delivery
    plugin._on_post_llm_call(session_id="s1", assistant_response="done")
    assert captured.calls == [], "cron runs are not Fetch conversations"


def test_unknown_source_falls_back_to_pushing(sent, monkeypatch):
    # Lookup miss must NOT suppress a real Fetch notification.
    plugin, captured = sent
    _set_source(monkeypatch, plugin, None)
    plugin._on_post_llm_call(session_id="s1", assistant_response="hi")
    assert len(captured.calls) == 1
    assert captured.calls[0]["source"] is None


def test_background_worker_never_pushes(sent, monkeypatch):
    plugin, captured = sent
    _set_source(monkeypatch, plugin, "")  # would normally push
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_123")
    try:
        plugin._on_post_llm_call(session_id="s1", assistant_response="hi")
    finally:
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    assert captured.calls == []


def test_approval_always_pushes_with_platform_source(sent, monkeypatch):
    plugin, captured = sent
    # Approvals notify regardless of surface; source parsed from session_key.
    plugin._on_pre_approval_request(
        command="rm -rf /", description="destructive",
        session_key="agent:main:telegram:private:123456789",
    )
    assert len(captured.calls) == 1
    assert captured.calls[0]["kind"] == "attention"
    assert captured.calls[0]["source"] == "telegram"
    assert captured.calls[0]["session_id"] == "agent:main:telegram:private:123456789"


def test_platform_from_session_key_parses_third_segment(sent):
    plugin, _ = sent
    assert plugin._platform_from_session_key("agent:main:telegram:private:123") == "telegram"
    assert plugin._platform_from_session_key("agent:main::private:123") == ""
    assert plugin._platform_from_session_key("") is None
    assert plugin._platform_from_session_key("not-a-session-key") is None


def test_fetch_channels_set_is_app_identity_not_hermes_internals(sent):
    plugin, _ = sent
    # The set is the app's own channel names, not a denylist of Hermes job types.
    assert "" in plugin.FETCH_CHANNELS
    assert "inbox" in plugin.FETCH_CHANNELS
    # Hermes job-type names are deliberately absent — they're simply not Fetch channels.
    assert "dispatch" not in plugin.FETCH_CHANNELS
    assert "weixin" not in plugin.FETCH_CHANNELS
    assert "cron" not in plugin.FETCH_CHANNELS
