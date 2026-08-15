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
from pathlib import Path

from agent_stubs import stub_agent_modules

PLUGIN_DIR = Path(__file__).resolve().parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

stub_agent_modules()
