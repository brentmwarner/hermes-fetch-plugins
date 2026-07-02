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

    def __call__(self, *, kind, session_id, title, body, source=None, data=None):
        self.calls.append({"kind": kind, "session_id": session_id,
                           "title": title, "body": body, "source": source,
                           "data": data or {}})


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


def test_fetch_gateway_reply_with_empty_source_does_not_push(sent, monkeypatch):
    plugin, captured = sent
    _set_source(monkeypatch, plugin, "")  # missing/legacy source is ambiguous
    plugin._on_post_llm_call(session_id="s1", assistant_response="hi")
    assert captured.calls == [], "blank source must not create a Fetch notification"


def test_fetch_app_reply_pushes_with_fetch_source(sent, monkeypatch):
    plugin, captured = sent
    _set_source(monkeypatch, plugin, "fetch")
    plugin._on_post_llm_call(session_id="s1", assistant_response="hi")
    assert len(captured.calls) == 1
    assert captured.calls[0]["kind"] == "replies"
    assert captured.calls[0]["source"] == "fetch"
    assert captured.calls[0]["session_id"] == "s1"


def test_fetch_app_reply_card_push_uses_readable_body(sent, monkeypatch):
    plugin, captured = sent
    _set_source(monkeypatch, plugin, "fetch")
    plugin._on_post_llm_call(
        session_id="s1",
        assistant_response=(
            "```card\n"
            '{"title":"World Cup Brief","subtitle":"Today at a glance",'
            '"stats":[{"label":"Matches","value":2}]}\n'
            "```"
        ),
    )

    assert len(captured.calls) == 1
    assert captured.calls[0]["body"] == "World Cup Brief, Today at a glance, Matches 2"
    assert "{" not in captured.calls[0]["body"]


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


def test_unknown_source_does_not_push(sent, monkeypatch):
    # Lookup misses are ambiguous and must not become Fetch inbox noise.
    plugin, captured = sent
    _set_source(monkeypatch, plugin, None)
    plugin._on_post_llm_call(session_id="s1", assistant_response="hi")
    assert captured.calls == []


def test_background_worker_never_pushes(sent, monkeypatch):
    plugin, captured = sent
    _set_source(monkeypatch, plugin, "")  # would normally push
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_123")
    try:
        plugin._on_post_llm_call(session_id="s1", assistant_response="hi")
    finally:
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    assert captured.calls == []


def test_kanban_completed_pushes_task_context_even_in_worker(sent, monkeypatch):
    plugin, captured = sent
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-1")
    monkeypatch.setattr(
        plugin,
        "_kanban_task_snapshot",
        lambda task_id, board: {"title": "Ship notifications", "assignee": "codex"},
    )

    plugin._on_kanban_task_completed(
        task_id="task-1",
        board="default",
        assignee=None,
        run_id=42,
        summary="Added task deep links.",
    )

    assert len(captured.calls) == 1
    assert captured.calls[0]["kind"] == "proactive"
    assert captured.calls[0]["session_id"] is None
    assert captured.calls[0]["source"] == "kanban"
    assert captured.calls[0]["title"] == "Task finished"
    assert captured.calls[0]["body"] == "Ship notifications: Added task deep links."
    assert captured.calls[0]["data"] == {
        "target": "task",
        "task_id": "task-1",
        "task_status": "done",
        "board": "default",
        "assignee": "codex",
        "run_id": "42",
    }


def test_kanban_blocked_pushes_attention_context(sent, monkeypatch):
    plugin, captured = sent
    monkeypatch.setattr(
        plugin,
        "_kanban_task_snapshot",
        lambda task_id, board: {"title": "Fix CI", "assignee": ""},
    )

    plugin._on_kanban_task_blocked(
        task_id="task-2",
        board="mobile",
        assignee="reviewer",
        run_id=7,
        reason="Needs a signing decision.",
    )

    assert len(captured.calls) == 1
    assert captured.calls[0]["kind"] == "attention"
    assert captured.calls[0]["title"] == "Task blocked"
    assert captured.calls[0]["body"] == "Fix CI: Needs a signing decision."
    assert captured.calls[0]["data"]["target"] == "task"
    assert captured.calls[0]["data"]["task_id"] == "task-2"
    assert captured.calls[0]["data"]["task_status"] == "blocked"


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


def test_fetch_channels_derives_from_app_sources(sent):
    plugin, _ = sent
    # FETCH_CHANNELS is FETCH_APP_SOURCES plus the empty-string untagged chat;
    # deriving keeps the two from drifting when a new Fetch source is added.
    assert plugin.FETCH_CHANNELS == plugin.FETCH_APP_SOURCES | {""}
    assert "" not in plugin.FETCH_APP_SOURCES


def test_session_source_caches_resolved_source(sent, monkeypatch):
    """A session's source is immutable, so it is read from state.db once and
    then served from the cache — the pre/post hooks both resolve it per turn."""
    plugin, _ = sent
    import hermes_state

    opens = []

    class _CountingDB:
        def __init__(self, *a, **kw):
            opens.append(1)

        def get_session(self, session_id):
            return {"source": "fetch"}

        def close(self):
            pass

    monkeypatch.setattr(hermes_state, "SessionDB", _CountingDB)

    assert plugin._session_source("sess-1") == "fetch"
    assert plugin._session_source("sess-1") == "fetch"
    assert opens == [1], "second lookup should hit the cache, not reopen state.db"


def test_session_source_does_not_cache_lookup_miss(sent, monkeypatch):
    """A not-yet-persisted session returns None and must stay uncached, so the
    source is picked up once the row appears."""
    plugin, _ = sent
    import hermes_state

    rows: dict[str, dict | None] = {"sess-2": None}

    class _DB:
        def __init__(self, *a, **kw):
            pass

        def get_session(self, session_id):
            return rows.get(session_id)

        def close(self):
            pass

    monkeypatch.setattr(hermes_state, "SessionDB", _DB)

    assert plugin._session_source("sess-2") is None
    rows["sess-2"] = {"source": "fetch"}
    assert plugin._session_source("sess-2") == "fetch"
