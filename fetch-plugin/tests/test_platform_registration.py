"""Regression tests for the public setup platform list."""

import importlib.util
import sys
import types
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[2]
FETCH_PLUGIN_DIR = PLUGIN_DIR if (PLUGIN_DIR / "__init__.py").exists() else REPO_ROOT / "fetch-plugin"
INBOX_PLUGIN_DIR = PLUGIN_DIR.parent / "hermes-inbox" if (PLUGIN_DIR.parent / "hermes-inbox" / "__init__.py").exists() else REPO_ROOT / "hermes-inbox-plugin"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_fetch_is_the_only_default_fetch_setup_platform(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_FETCH_TUNNEL_ENABLED", raising=False)
    monkeypatch.delenv("HERMES_INBOX_REGISTER_LEGACY_PLATFORM", raising=False)
    monkeypatch.setenv("HERMES_INBOX_ENABLED", "1")

    fetch = _load_module("fetch_plugin_register_test", FETCH_PLUGIN_DIR / "__init__.py")
    inbox = _load_module("hermes_inbox_register_test", INBOX_PLUGIN_DIR / "__init__.py")
    monkeypatch.setattr(fetch._inbox, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(inbox, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(inbox, "_fetch_platform_already_registered", lambda: True)
    registered = []
    ctx = types.SimpleNamespace(
        register_hook=lambda *args, **kwargs: None,
        register_platform=lambda **kwargs: registered.append(kwargs),
    )

    fetch.register(ctx)
    inbox.register(ctx)

    assert [entry["name"] for entry in registered] == ["fetch"]
    assert registered[0]["label"] == "Fetch"
    assert callable(registered[0]["setup_fn"])
    assert registered[0]["cron_deliver_env_var"] == "HERMES_INBOX_HOME_CHANNEL"
