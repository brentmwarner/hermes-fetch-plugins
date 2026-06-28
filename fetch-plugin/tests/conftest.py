"""Pytest bootstrap for the fetch-plugin tests.

The plugin's ``__init__.py`` loads sibling modules (``_inbox.py``, ``_pairing.py``,
``_runtime.py``, ``_tunnel.py``) by path at import time, and those import
hermes-agent-only modules (``gateway.config``, ``gateway.platforms.base``,
``hermes_cli.config``, ``hermes_state``) which are not installed outside the
running agent. We install lightweight stand-ins here — at conftest import, i.e.
before any test module is collected — using ``setdefault`` so real modules win
when present (e.g. inside the agent's own test environment).
"""

import sys
import types
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))


def _stub_agent_modules() -> None:
    for name in (
        "gateway",
        "gateway.config",
        "gateway.platforms",
        "gateway.platforms.base",
        "hermes_cli",
        "hermes_cli.config",
        "hermes_cli.cli_output",
        "hermes_cli.push_notifications",
        "hermes_state",
    ):
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules["hermes_cli"].__path__ = []
    sys.modules["gateway.config"].Platform = lambda *a, **k: None
    sys.modules["gateway.config"].PlatformConfig = object
    sys.modules["gateway.platforms.base"].BasePlatformAdapter = object
    sys.modules["gateway.platforms.base"].SendResult = object
    # Sibling modules import get_hermes_home at the top level.
    sys.modules["hermes_cli.config"].get_hermes_home = lambda: Path.home() / ".hermes"
    sys.modules["hermes_cli.cli_output"].prompt_yes_no = lambda *a, **k: False
    sys.modules["hermes_cli.cli_output"].print_header = print
    sys.modules["hermes_cli.cli_output"].print_info = print
    sys.modules["hermes_cli.cli_output"].print_success = print
    sys.modules["hermes_cli.cli_output"].print_warning = print
    # SessionDB stub: tests that need source-lookup behavior monkeypatch this.
    class _StubSessionDB:
        def __init__(self, *a, **kw): pass
        def get_session(self, session_id): return None
        def close(self): pass
    sys.modules["hermes_state"].SessionDB = _StubSessionDB


_stub_agent_modules()
