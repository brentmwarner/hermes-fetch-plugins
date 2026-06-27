"""The pre_tool_call hook enforces that agent-created kanban tasks carry a real
spec (FET-16: "creates titles but not useful details").

This replaces the old KANBAN_GUIDANCE / kanban_create-schema core patches with a
plugin-owned quality gate that a `hermes update` can't silently wipe. Hermes
honors a ``{"action": "block", "message": ...}`` return from a pre_tool_call
hook (``hermes_cli.plugins.get_pre_tool_call_block_message``).
"""

import importlib.util
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]


def _load_plugin():
    spec = importlib.util.spec_from_file_location(
        "fetch_plugin_pretool_test", PLUGIN_DIR / "__init__.py"
    )
    assert spec is not None and spec.loader is not None
    plugin = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = plugin
    spec.loader.exec_module(plugin)
    return plugin


@pytest.fixture
def plugin():
    return _load_plugin()


def test_register_wires_pre_tool_call(monkeypatch):
    monkeypatch.delenv("HERMES_FETCH_TUNNEL_ENABLED", raising=False)
    plugin = _load_plugin()
    monkeypatch.setattr(plugin._inbox, "is_delivery_enabled", lambda: False)
    hooks = {}
    import types
    ctx = types.SimpleNamespace(
        register_hook=lambda name, cb: hooks.setdefault(name, cb),
        register_platform=lambda **kw: None,
    )
    plugin.register(ctx)
    assert hooks["pre_tool_call"] is plugin._on_pre_tool_call


def test_blocks_kanban_create_with_no_body(plugin):
    result = plugin._on_pre_tool_call(
        tool_name="kanban_create",
        args={"title": "Do the thing", "assignee": "coder"},
    )
    assert isinstance(result, dict)
    assert result["action"] == "block"
    assert "body" in result["message"]


def test_blocks_kanban_create_with_blank_body(plugin):
    result = plugin._on_pre_tool_call(
        tool_name="kanban_create",
        args={"title": "x", "assignee": "coder", "body": "   \n  "},
    )
    assert result is not None and result["action"] == "block"


def test_allows_kanban_create_with_body(plugin):
    result = plugin._on_pre_tool_call(
        tool_name="kanban_create",
        args={"title": "x", "assignee": "coder", "body": "Full spec: do X, accept when Y."},
    )
    assert result is None


@pytest.mark.parametrize("body", [[], {}, 0, False])
def test_blocks_kanban_create_with_non_string_body(plugin, body):
    result = plugin._on_pre_tool_call(
        tool_name="kanban_create",
        args={"title": "x", "assignee": "coder", "body": body},
    )
    assert result is not None and result["action"] == "block"


def test_allows_triage_stub_without_body(plugin):
    # A deliberate triage stub is exempt — a specifier fleshes it out later.
    for triage in (True, "true", "1"):
        result = plugin._on_pre_tool_call(
            tool_name="kanban_create",
            args={"title": "x", "assignee": "coder", "triage": triage},
        )
        assert result is None, f"triage={triage!r} stub should be allowed"


@pytest.mark.parametrize("source", ["fetch", "fetch-ios", "ios", "mobile", "inbox"])
def test_blocks_send_message_to_fetch_from_fetch_session(plugin, monkeypatch, source):
    monkeypatch.setattr(plugin, "_session_source", lambda session_id: source)

    result = plugin._on_pre_tool_call(
        tool_name="send_message",
        args={"target": "fetch", "message": "I finished."},
        session_id="s1",
    )

    assert isinstance(result, dict)
    assert result["action"] == "block"
    assert "Reply in the current thread" in result["message"]


def test_blocks_named_fetch_delivery_from_fetch_session(plugin, monkeypatch):
    monkeypatch.setattr(plugin, "_session_source", lambda session_id: "fetch")

    result = plugin._on_pre_tool_call(
        tool_name="send_message",
        args={"target": "fetch:researcher", "message": "I finished."},
        session_id="s1",
    )

    assert result is not None and result["action"] == "block"


def test_blocks_send_message_to_fetch_with_task_id_only(plugin, monkeypatch):
    seen = []

    def source_for(session_id):
        seen.append(session_id)
        return "fetch"

    monkeypatch.setattr(plugin, "_session_source", source_for)

    result = plugin._on_pre_tool_call(
        tool_name="send_message",
        args={"target": "fetch", "message": "I finished."},
        task_id="task-123",
    )

    assert seen == ["task-123"]
    assert result is not None and result["action"] == "block"


@pytest.mark.parametrize("source", ["telegram", "cli", "cron", None, ""])
def test_allows_send_message_to_fetch_from_non_fetch_or_unknown_session(plugin, monkeypatch, source):
    monkeypatch.setattr(plugin, "_session_source", lambda session_id: source)

    result = plugin._on_pre_tool_call(
        tool_name="send_message",
        args={"target": "fetch", "message": "Deliver this."},
        session_id="s1",
    )

    assert result is None


def test_allows_send_message_to_other_platform_from_fetch_session(plugin, monkeypatch):
    monkeypatch.setattr(plugin, "_session_source", lambda session_id: "fetch")

    result = plugin._on_pre_tool_call(
        tool_name="send_message",
        args={"target": "telegram", "message": "Deliver this."},
        session_id="s1",
    )

    assert result is None


def test_allows_send_message_list_from_fetch_session(plugin, monkeypatch):
    monkeypatch.setattr(plugin, "_session_source", lambda session_id: "fetch")

    result = plugin._on_pre_tool_call(
        tool_name="send_message",
        args={"action": "list"},
        session_id="s1",
    )

    assert result is None


def test_ignores_other_tools(plugin):
    assert plugin._on_pre_tool_call(tool_name="kanban_complete", args={}) is None
    assert plugin._on_pre_tool_call(tool_name="kanban_comment", args={"body": ""}) is None
    assert plugin._on_pre_tool_call(tool_name="terminal", args={}) is None


def test_tolerates_missing_or_nondict_args(plugin):
    assert plugin._on_pre_tool_call(tool_name="kanban_create") is not None
    assert plugin._on_pre_tool_call(tool_name="kanban_create", args=None) is not None
    assert plugin._on_pre_tool_call(tool_name="kanban_create", args="oops") is not None
