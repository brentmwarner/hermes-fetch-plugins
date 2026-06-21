"""The plugin must advertise the inbox as a named channel via channel_aliases.json.

A send-only platform never gets a channel from inbound traffic, and Hermes hides
platforms with no channels from the agent's target list. Seeding the home channel
as an alias is what lets the agent *discover* and proactively message Fetch.
These tests verify the seed is written, honors the configured home channel, is
non-destructive to other platforms' aliases, never clobbers a name the user has
customized, and prunes stale auto-generated aliases when the home channel
changes. ``conftest.py`` stubs the hermes-agent modules the plugin imports
at load; here we point ``get_hermes_home`` at a tmp dir per test.
"""

import importlib.util
import json
import sys
import types
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
ALIASES = "channel_aliases.json"


def _load_plugin():
    spec = importlib.util.spec_from_file_location(
        "hermes_inbox_plugin", PLUGIN_DIR / "__init__.py"
    )
    assert spec is not None and spec.loader is not None
    plugin = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = plugin
    spec.loader.exec_module(plugin)
    return plugin


def test_seed_creates_named_channel(tmp_path, monkeypatch):
    plugin = _load_plugin()
    monkeypatch.setattr(plugin, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_INBOX_HOME_CHANNEL", "default")

    plugin._seed_channel_alias()

    data = json.loads((tmp_path / ALIASES).read_text())
    assert data == {"hermes_inbox": {"default": "Fetch"}}


def test_seed_honors_configured_home_channel(tmp_path, monkeypatch):
    plugin = _load_plugin()
    monkeypatch.setattr(plugin, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_INBOX_HOME_CHANNEL", "leads")

    plugin._seed_channel_alias()

    data = json.loads((tmp_path / ALIASES).read_text())
    assert data == {"hermes_inbox": {"leads": "Fetch"}}


def test_seed_preserves_other_platforms_and_custom_name(tmp_path, monkeypatch):
    plugin = _load_plugin()
    monkeypatch.setattr(plugin, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_INBOX_HOME_CHANNEL", "default")
    # Another platform's aliases, plus a home channel the user already renamed.
    existing = {
        "telegram": {"6927549812": "Brent"},
        "hermes_inbox": {"default": "My Phone"},
    }
    (tmp_path / ALIASES).write_text(json.dumps(existing))

    plugin._seed_channel_alias()

    data = json.loads((tmp_path / ALIASES).read_text())
    # Telegram untouched; user's custom inbox name preserved (not overwritten).
    assert data == existing


def test_reseed_prunes_stale_auto_alias_when_home_channel_changes(tmp_path, monkeypatch):
    plugin = _load_plugin()
    monkeypatch.setattr(plugin, "get_hermes_home", lambda: tmp_path)

    monkeypatch.setenv("HERMES_INBOX_HOME_CHANNEL", "default")
    plugin._seed_channel_alias()
    assert json.loads((tmp_path / ALIASES).read_text()) == {"hermes_inbox": {"default": "Fetch"}}

    # Home channel changes; the stale auto-generated "default" alias must not linger.
    monkeypatch.setenv("HERMES_INBOX_HOME_CHANNEL", "leads")
    plugin._seed_channel_alias()

    data = json.loads((tmp_path / ALIASES).read_text())
    assert data == {"hermes_inbox": {"leads": "Fetch"}}


def test_reseed_keeps_user_renamed_alias_when_home_channel_changes(tmp_path, monkeypatch):
    plugin = _load_plugin()
    monkeypatch.setattr(plugin, "get_hermes_home", lambda: tmp_path)
    # User renamed the old home channel; that alias is not auto-generated, so keep it.
    existing = {"hermes_inbox": {"default": "My Phone"}}
    (tmp_path / ALIASES).write_text(json.dumps(existing))

    monkeypatch.setenv("HERMES_INBOX_HOME_CHANNEL", "leads")
    plugin._seed_channel_alias()

    data = json.loads((tmp_path / ALIASES).read_text())
    assert data == {"hermes_inbox": {"default": "My Phone", "leads": "Fetch"}}


def test_seed_skips_when_aliases_top_level_is_not_dict(tmp_path, monkeypatch):
    plugin = _load_plugin()
    monkeypatch.setattr(plugin, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_INBOX_HOME_CHANNEL", "default")
    original = ["unexpected", "shape"]
    (tmp_path / ALIASES).write_text(json.dumps(original))

    plugin._seed_channel_alias()

    data = json.loads((tmp_path / ALIASES).read_text())
    assert data == original


def test_seed_skips_when_platform_aliases_entry_is_not_dict(tmp_path, monkeypatch):
    plugin = _load_plugin()
    monkeypatch.setattr(plugin, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_INBOX_HOME_CHANNEL", "default")
    existing = {"telegram": {"6927549812": "Brent"}, "hermes_inbox": ["unexpected", "shape"]}
    (tmp_path / ALIASES).write_text(json.dumps(existing))

    plugin._seed_channel_alias()

    data = json.loads((tmp_path / ALIASES).read_text())
    assert data == existing


def test_register_hides_legacy_platform_by_default(tmp_path, monkeypatch):
    plugin = _load_plugin()
    monkeypatch.setattr(plugin, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_INBOX_ENABLED", "true")
    monkeypatch.setenv("HERMES_INBOX_HOME_CHANNEL", "default")
    captured = {}
    ctx = types.SimpleNamespace(register_platform=lambda **kw: captured.update(kw))

    plugin.register(ctx)

    assert not (tmp_path / ALIASES).exists()
    assert captured == {}


def test_register_seeds_and_registers_when_legacy_platform_enabled(tmp_path, monkeypatch):
    plugin = _load_plugin()
    monkeypatch.setattr(plugin, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_INBOX_ENABLED", "true")
    monkeypatch.setenv("HERMES_INBOX_HOME_CHANNEL", "default")
    monkeypatch.setenv("HERMES_INBOX_REGISTER_LEGACY_PLATFORM", "1")
    captured = {}
    ctx = types.SimpleNamespace(register_platform=lambda **kw: captured.update(kw))

    plugin.register(ctx)

    assert (tmp_path / ALIASES).exists()
    assert captured["name"] == "hermes_inbox"


def test_register_skips_seed_when_disabled(tmp_path, monkeypatch):
    plugin = _load_plugin()
    monkeypatch.setattr(plugin, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_INBOX_ENABLED", "false")
    ctx = types.SimpleNamespace(register_platform=lambda **kw: None)

    plugin.register(ctx)

    assert not (tmp_path / ALIASES).exists()
