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
