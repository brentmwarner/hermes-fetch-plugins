"""Fetch turn context injection for the shared dashboard/TUI gateway.

The iOS app speaks the dashboard websocket protocol, but Fetch-owned sessions
must still receive Fetch-native UI guidance even if the underlying agent runtime
identifies the transport as TUI.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]


def _load_plugin():
    spec = importlib.util.spec_from_file_location(
        "fetch_plugin_turn_context_test", PLUGIN_DIR / "__init__.py"
    )
    assert spec is not None and spec.loader is not None
    plugin = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = plugin
    spec.loader.exec_module(plugin)
    return plugin


@pytest.fixture
def plugin():
    return _load_plugin()


def test_register_wires_pre_llm_call(monkeypatch):
    monkeypatch.delenv("HERMES_FETCH_TUNNEL_ENABLED", raising=False)
    plugin = _load_plugin()
    monkeypatch.setattr(plugin._inbox, "is_delivery_enabled", lambda: False)
    hooks = {}
    ctx = types.SimpleNamespace(
        register_hook=lambda name, cb: hooks.setdefault(name, cb),
        register_platform=lambda **kw: None,
    )

    plugin.register(ctx)

    assert hooks["pre_llm_call"] is plugin._on_pre_llm_call


@pytest.mark.parametrize("source", ["fetch", "fetch-ios", "ios", "mobile", "inbox"])
def test_pre_llm_call_injects_native_context_for_fetch_sources(plugin, monkeypatch, source):
    monkeypatch.setattr(plugin, "_session_source", lambda session_id: source)

    result = plugin._on_pre_llm_call(
        session_id="20260626_fetch",
        platform="tui",
        user_message="Generate a token usage chart",
    )

    assert isinstance(result, dict)
    context = result["context"]
    assert "Client: Fetch iOS app" in context
    assert "native mobile chat" in context
    assert "```card" in context
    assert "ASCII/text bar charts" in context
    assert "SVG links" in context


@pytest.mark.parametrize("source", ["", "tui", "cli", "telegram", "discord", "cron", None])
def test_pre_llm_call_does_not_inject_for_non_fetch_sources(plugin, monkeypatch, source):
    monkeypatch.setattr(plugin, "_session_source", lambda session_id: source)

    result = plugin._on_pre_llm_call(
        session_id="20260626_other",
        platform="tui",
        user_message="Generate a token usage chart",
    )

    assert result is None


def test_pre_llm_call_requires_session_source_lookup(plugin, monkeypatch):
    calls = []
    monkeypatch.setattr(plugin, "_session_source", lambda session_id: calls.append(session_id) or "fetch")

    plugin._on_pre_llm_call(session_id="session-1")

    assert calls == ["session-1"]
