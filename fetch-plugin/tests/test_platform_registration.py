"""Regression tests for the public setup platform list.

Fetch is the single first-class plugin, so it must register exactly one
platform — ``fetch`` — with the relay pairing ``setup_fn`` and the
``HERMES_FETCH_HOME_CHANNEL`` cron delivery bridge.
"""

import importlib.util
import sys
import types
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[2]
FETCH_PLUGIN_DIR = PLUGIN_DIR if (PLUGIN_DIR / "__init__.py").exists() else REPO_ROOT / "fetch-plugin"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_fetch_registers_exactly_one_platform(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_FETCH_TUNNEL_ENABLED", raising=False)
    monkeypatch.setenv("HERMES_FETCH_DELIVERY_ENABLED", "1")

    fetch = _load_module("fetch_plugin_register_test", FETCH_PLUGIN_DIR / "__init__.py")
    monkeypatch.setattr(fetch._inbox, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(sys.modules["hermes_cli.config"], "get_hermes_home", lambda: tmp_path)
    registered = []
    ctx = types.SimpleNamespace(
        register_hook=lambda *args, **kwargs: None,
        register_platform=lambda **kwargs: registered.append(kwargs),
    )

    fetch.register(ctx)

    assert [entry["name"] for entry in registered] == ["fetch"]
    assert registered[0]["label"] == "Fetch"
    assert callable(registered[0]["setup_fn"])
    assert registered[0]["cron_deliver_env_var"] == "HERMES_FETCH_HOME_CHANNEL"
    skill_dir = tmp_path / "skills" / "fetch-cards"
    assert (skill_dir / "SKILL.md").is_file()
    # A first install is plugin-managed and is marked so it can be refreshed.
    assert (skill_dir / fetch._MANAGED_MARKER).is_file()


def test_fetch_tunnel_autostarts_after_pairing_without_env(monkeypatch):
    monkeypatch.delenv("HERMES_FETCH_TUNNEL_ENABLED", raising=False)
    fetch = _load_module("fetch_plugin_tunnel_auto_test", FETCH_PLUGIN_DIR / "__init__.py")
    monkeypatch.setattr(fetch._pairing, "is_pairing_configured", lambda: True)

    assert fetch._should_start_tunnel() is True
    assert fetch._tunnel_start_reason() == (
        "auto-started: relay pairing present; "
        "set HERMES_FETCH_TUNNEL_ENABLED=0 to disable"
    )


def test_fetch_tunnel_false_env_overrides_pairing(monkeypatch):
    monkeypatch.setenv("HERMES_FETCH_TUNNEL_ENABLED", "0")
    fetch = _load_module("fetch_plugin_tunnel_disable_test", FETCH_PLUGIN_DIR / "__init__.py")
    monkeypatch.setattr(fetch._pairing, "is_pairing_configured", lambda: True)

    assert fetch._should_start_tunnel() is False
    assert fetch._tunnel_start_reason() is None


def test_fetch_cards_skill_never_overwrites_custom_skill(tmp_path, monkeypatch):
    fetch = _load_module("fetch_plugin_skill_install_test", FETCH_PLUGIN_DIR / "__init__.py")
    target = tmp_path / "skills" / "fetch-cards"
    target.mkdir(parents=True)
    existing = target / "SKILL.md"
    existing.write_text("custom skill", encoding="utf-8")
    monkeypatch.setattr(sys.modules["hermes_cli.config"], "get_hermes_home", lambda: tmp_path)

    fetch._ensure_fetch_cards_skill()

    assert existing.read_text(encoding="utf-8") == "custom skill"
    # A custom skill has no plugin sentinel, so it stays user-owned.
    assert not (target / fetch._MANAGED_MARKER).exists()


def _make_bundled_skill(root: Path, body: str) -> Path:
    bundled = root / "bundled" / "fetch-cards"
    bundled.mkdir(parents=True)
    (bundled / "SKILL.md").write_text(body, encoding="utf-8")
    (bundled / "patterns.md").write_text("patterns", encoding="utf-8")
    return bundled


def test_fetch_cards_skill_refreshes_plugin_managed_copy_on_update(tmp_path):
    fetch = _load_module("fetch_plugin_skill_refresh_test", FETCH_PLUGIN_DIR / "__init__.py")
    bundled = _make_bundled_skill(tmp_path, "version 2")

    # A previously auto-installed (plugin-managed) older copy.
    target = tmp_path / "skills" / "fetch-cards"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("version 1", encoding="utf-8")
    (target / "stale_file.md").write_text("gone in the new bundle", encoding="utf-8")
    (target / fetch._MANAGED_MARKER).write_text("", encoding="utf-8")

    fetch._install_managed_skill(bundled, target)

    assert (target / "SKILL.md").read_text(encoding="utf-8") == "version 2"
    assert (target / "patterns.md").read_text(encoding="utf-8") == "patterns"
    # The refresh replaces the whole tree, so stale files are dropped.
    assert not (target / "stale_file.md").exists()
    assert (target / fetch._MANAGED_MARKER).is_file()
