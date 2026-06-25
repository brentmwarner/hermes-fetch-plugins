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

import pytest

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
    assert data == {"fetch": {"default": "Fetch"}}


def test_seed_honors_configured_home_channel(tmp_path, monkeypatch):
    plugin = _load_plugin()
    monkeypatch.setattr(plugin, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_INBOX_HOME_CHANNEL", "leads")

    plugin._seed_channel_alias()

    data = json.loads((tmp_path / ALIASES).read_text())
    assert data == {"fetch": {"leads": "Fetch"}}


def test_seed_preserves_other_platforms_and_custom_name(tmp_path, monkeypatch):
    plugin = _load_plugin()
    monkeypatch.setattr(plugin, "get_hermes_home", lambda: tmp_path)
    (tmp_path / "profiles" / "researcher").mkdir(parents=True)
    monkeypatch.setenv("HERMES_INBOX_HOME_CHANNEL", "default")
    # Another platform's aliases, plus legacy aliases from older versions.
    existing = {
        "telegram": {"6927549812": "Brent"},
        "hermes_inbox": {
            "default": "My Phone",
            "researcher": "Researcher",
            "custom": "My Custom Legacy Target",
            "world-cup": "World Cup",
        },
    }
    (tmp_path / ALIASES).write_text(json.dumps(existing))

    plugin._seed_channel_alias()

    data = json.loads((tmp_path / ALIASES).read_text())
    # Telegram untouched; custom legacy names preserved while auto legacy aliases are pruned.
    assert data == {
        "telegram": {"6927549812": "Brent"},
        "hermes_inbox": {"default": "My Phone", "custom": "My Custom Legacy Target", "world-cup": "World Cup"},
        "fetch": {"default": "Fetch", "researcher": "Researcher"},
    }


def test_reseed_prunes_stale_auto_alias_when_home_channel_changes(tmp_path, monkeypatch):
    plugin = _load_plugin()
    monkeypatch.setattr(plugin, "get_hermes_home", lambda: tmp_path)

    monkeypatch.setenv("HERMES_INBOX_HOME_CHANNEL", "default")
    plugin._seed_channel_alias()
    assert json.loads((tmp_path / ALIASES).read_text()) == {"fetch": {"default": "Fetch"}}

    # Home channel changes; the stale auto-generated "default" alias must not linger.
    monkeypatch.setenv("HERMES_INBOX_HOME_CHANNEL", "leads")
    plugin._seed_channel_alias()

    data = json.loads((tmp_path / ALIASES).read_text())
    assert data == {"fetch": {"leads": "Fetch"}}


def test_reseed_keeps_user_renamed_alias_when_home_channel_changes(tmp_path, monkeypatch):
    plugin = _load_plugin()
    monkeypatch.setattr(plugin, "get_hermes_home", lambda: tmp_path)
    # User renamed the old home channel; that alias is not auto-generated, so keep it.
    existing = {"hermes_inbox": {"default": "My Phone"}}
    (tmp_path / ALIASES).write_text(json.dumps(existing))

    monkeypatch.setenv("HERMES_INBOX_HOME_CHANNEL", "leads")
    plugin._seed_channel_alias()

    data = json.loads((tmp_path / ALIASES).read_text())
    assert data == {"hermes_inbox": {"default": "My Phone"}, "fetch": {"leads": "Fetch"}}


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
    assert data == {**existing, "fetch": {"default": "Fetch"}}


def test_register_exposes_canonical_fetch_by_default(tmp_path, monkeypatch):
    plugin = _load_plugin()
    monkeypatch.setattr(plugin, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(plugin, "_fetch_platform_already_registered", lambda: False)
    monkeypatch.setattr(plugin, "_productized_fetch_plugin_available", lambda: False)
    monkeypatch.setenv("HERMES_INBOX_ENABLED", "true")
    monkeypatch.setenv("HERMES_INBOX_HOME_CHANNEL", "default")
    captured = {}
    ctx = types.SimpleNamespace(register_platform=lambda **kw: captured.update(kw))

    plugin.register(ctx)

    assert json.loads((tmp_path / ALIASES).read_text()) == {"fetch": {"default": "Fetch"}}
    assert captured["name"] == "fetch"


def test_register_does_not_claim_fetch_when_productized_plugin_is_available(tmp_path, monkeypatch):
    plugin = _load_plugin()
    monkeypatch.setattr(plugin, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(plugin, "_fetch_platform_already_registered", lambda: False)
    monkeypatch.setattr(plugin, "_productized_fetch_plugin_available", lambda: True)
    monkeypatch.setenv("HERMES_INBOX_ENABLED", "true")
    monkeypatch.setenv("HERMES_INBOX_HOME_CHANNEL", "default")
    captured = []
    ctx = types.SimpleNamespace(register_platform=lambda **kw: captured.append(kw))

    plugin.register(ctx)

    assert json.loads((tmp_path / ALIASES).read_text()) == {"fetch": {"default": "Fetch"}}
    assert captured == []


def test_register_seeds_and_registers_when_legacy_platform_enabled(tmp_path, monkeypatch):
    plugin = _load_plugin()
    monkeypatch.setattr(plugin, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(plugin, "_fetch_platform_already_registered", lambda: False)
    monkeypatch.setattr(plugin, "_productized_fetch_plugin_available", lambda: False)
    monkeypatch.setenv("HERMES_INBOX_ENABLED", "true")
    monkeypatch.setenv("HERMES_INBOX_HOME_CHANNEL", "default")
    monkeypatch.setenv("HERMES_INBOX_REGISTER_LEGACY_PLATFORM", "1")
    captured = []
    ctx = types.SimpleNamespace(register_platform=lambda **kw: captured.append(kw))

    plugin.register(ctx)

    assert json.loads((tmp_path / ALIASES).read_text()) == {
        "fetch": {"default": "Fetch"},
        "hermes_inbox": {"default": "Fetch"},
    }
    assert [entry["name"] for entry in captured] == ["fetch", "hermes_inbox"]


def test_register_skips_seed_when_disabled(tmp_path, monkeypatch):
    plugin = _load_plugin()
    monkeypatch.setattr(plugin, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(plugin, "_fetch_platform_already_registered", lambda: False)
    monkeypatch.setattr(plugin, "_productized_fetch_plugin_available", lambda: False)
    monkeypatch.setenv("HERMES_INBOX_ENABLED", "false")
    captured = []
    ctx = types.SimpleNamespace(register_platform=lambda **kw: captured.append(kw))

    plugin.register(ctx)

    assert not (tmp_path / ALIASES).exists()
    assert captured == []


def test_fetch_platform_registered_returns_false_when_registry_module_missing(monkeypatch):
    plugin = _load_plugin()
    monkeypatch.setitem(sys.modules, "gateway.platform_registry", None)

    assert plugin._fetch_platform_already_registered() is False


def test_fetch_platform_registered_propagates_registry_errors(monkeypatch):
    plugin = _load_plugin()

    class _BrokenRegistry:
        def is_registered(self, _name):
            raise RuntimeError("boom")

    gateway_module = types.ModuleType("gateway")
    registry_module = types.ModuleType("gateway.platform_registry")
    setattr(registry_module, "platform_registry", _BrokenRegistry())
    setattr(gateway_module, "platform_registry", registry_module)
    monkeypatch.setitem(sys.modules, "gateway", gateway_module)
    monkeypatch.setitem(sys.modules, "gateway.platform_registry", registry_module)

    with pytest.raises(RuntimeError, match="boom"):
        plugin._fetch_platform_already_registered()
