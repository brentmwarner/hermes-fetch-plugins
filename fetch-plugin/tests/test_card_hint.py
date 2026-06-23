"""The platform_hint registered for the Fetch platform teaches the agent (via
the cached system prompt, Fetch sessions only) to emit generative-UI ``card``
fences the iOS app renders natively — see ``MarkdownText.swift`` (fence
parsing) and ``GenerativeCard.swift`` (``CardSpec`` + ``GenerativeCardView``).

Without this hint the agent has no instruction to emit card fences, so the
app's entire generative-UI surface goes unused. These tests guard the schema
fields, a worked example, the skill pointer, and the fallback-safety note so
the capability survives future edits to ``register()``.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]


def _load_plugin():
    spec = importlib.util.spec_from_file_location(
        "fetch_plugin_hint_test", PLUGIN_DIR / "__init__.py"
    )
    assert spec is not None and spec.loader is not None
    plugin = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = plugin
    spec.loader.exec_module(plugin)
    return plugin


@pytest.fixture
def registered_platform(monkeypatch):
    """Load the fetch plugin and capture the kwargs passed to register_platform."""
    monkeypatch.delenv("HERMES_FETCH_TUNNEL_ENABLED", raising=False)
    plugin = _load_plugin()
    # is_delivery_enabled() runs after register_platform inside register();
    # stub it False so seed_channel_alias is skipped and registration completes
    # cleanly even though the inbox adapter isn't wired in a test context.
    monkeypatch.setattr(plugin._inbox, "is_delivery_enabled", lambda: False)
    captured = []
    ctx = types.SimpleNamespace(
        register_hook=lambda *a, **kw: None,
        register_platform=lambda **kw: captured.append(kw),
    )
    plugin.register(ctx)
    assert len(captured) == 1, "register_platform must be called exactly once"
    return captured[0]


def test_hint_identifies_fetch_surface(registered_platform):
    hint = registered_platform["platform_hint"]
    assert "Fetch iOS" in hint, "must tell the agent it's on the Fetch iOS app"


def test_hint_documents_card_fence_syntax(registered_platform):
    hint = registered_platform["platform_hint"]
    assert "```card" in hint, "must show the ```card fence language"
    # The agent needs to know the fence closes with ``` too.
    assert hint.count("```") >= 2, "must include a complete fenced example"


def test_hint_lists_every_cardspec_field(registered_platform):
    """Every CardSpec field the renderer supports must appear in the hint so
    the agent can emit valid cards without loading the skill. `symbol` and
    `emoji` are intentionally omitted from the documented schema in the hint
    to steer toward typographic cards — they're still accepted by the
    renderer but the agent shouldn't reach for them."""
    hint = registered_platform["platform_hint"]
    for field in ("title", "subtitle", "image", "url", "footer",
                  "stats", "items", "cards"):
        assert field in hint, f"platform_hint missing card field '{field}'"


def test_hint_carries_a_worked_example(registered_platform):
    hint = registered_platform["platform_hint"]
    # The daily-brief example teaches fence + JSON together — typographic
    # (no symbol field).
    assert '"title"' in hint
    assert '"stats"' in hint
    assert '"items"' in hint


def test_hint_points_to_fetch_cards_skill(registered_platform):
    hint = registered_platform["platform_hint"]
    assert "fetch-cards" in hint, \
        "must point to the fetch-cards skill for richer patterns"


def test_hint_mentions_malformed_card_fallback(registered_platform):
    """The agent should know a bad card is safe — it falls back to a code
    block, never breaks the chat. This lowers the barrier to attempting a card."""
    hint = registered_platform["platform_hint"]
    lower = hint.lower()
    assert "code block" in lower or "fallback" in lower, \
        "must mention that malformed cards fall back to a code block"


def test_hint_directs_minimal_iconography(registered_platform):
    """The Fetch card aesthetic is typographic, not decorative. The hint must
    steer the agent away from `symbol`/`emoji` and toward text-only cards."""
    hint = registered_platform["platform_hint"]
    lower = hint.lower()
    assert "omit" in lower and ("symbol" in lower or "icon" in lower), \
        "must instruct the agent to omit symbols/icons by default"
    assert "emoji" in lower, \
        "must mention emoji is not needed"
