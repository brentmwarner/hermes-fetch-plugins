"""The inbox plugin's proactive push must route through the Fetch relay client.

The plugin imports several hermes-agent-only modules at load time
(``gateway.config``, ``gateway.platforms.base``, ``hermes_cli.config``,
``hermes_state``); ``conftest.py`` installs stand-ins for those before
collection so the module imports standalone. Here we load ``__init__.py`` by
file path, monkeypatch the plugin's ``_load_relay`` to a fake that records
calls, and assert ``_notify_proactive`` forwards exactly one
``send_event_background`` call with ``kind="proactive"`` and the right
session_id/title/body.
"""

import importlib.util
import sys
import types
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]


def _load_plugin():
    spec = importlib.util.spec_from_file_location(
        "hermes_inbox_plugin", PLUGIN_DIR / "__init__.py"
    )
    assert spec is not None and spec.loader is not None
    plugin = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = plugin
    spec.loader.exec_module(plugin)
    return plugin


def test_notify_proactive_clamps_long_fields(monkeypatch):
    plugin = _load_plugin()

    calls = []
    fake_relay = types.SimpleNamespace(
        send_event_background=lambda **kw: calls.append(kw)
    )
    monkeypatch.setattr(plugin, "_load_relay", lambda: fake_relay)

    long_title = "x" * 200
    long_body = "y" * 1000
    plugin._notify_proactive(
        session_id="inbox_default", title=long_title, body=long_body
    )

    assert calls == [
        {
            "kind": "proactive",
            "session_id": "inbox_default",
            "title": long_title[:120],
            "body": long_body[:500],
        }
    ]


def test_notify_proactive_forwards_to_relay(monkeypatch):
    plugin = _load_plugin()

    calls = []
    fake_relay = types.SimpleNamespace(
        send_event_background=lambda **kw: calls.append(kw)
    )
    monkeypatch.setattr(plugin, "_load_relay", lambda: fake_relay)

    plugin._notify_proactive(
        session_id="inbox_default", title="Hermes Inbox", body="new lead"
    )

    assert calls == [
        {
            "kind": "proactive",
            "session_id": "inbox_default",
            "title": "Hermes Inbox",
            "body": "new lead",
        }
    ]
