"""Pytest bootstrap for the hermes-inbox plugin tests.

The plugin directory ships a top-level ``__init__.py`` (it *is* the plugin
module), so pytest treats the directory as a package and imports that
``__init__.py`` during collection. That import pulls in hermes-agent-only
modules (``gateway.config``, ``gateway.platforms.base``, ``hermes_cli.config``,
``hermes_state``) which are not installed outside the running agent and would
crash collection. We install lightweight stand-ins here — at conftest import,
i.e. before any test module is collected — so both the collection-time import
and the test's own by-path load of ``__init__.py`` succeed.
"""

import sys
import types
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent
# Let the by-path load of __init__.py resolve against the plugin dir in the dev tree.
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
        "hermes_cli.push_notifications",
        "hermes_state",
    ):
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules["gateway.config"].Platform = lambda *a, **k: None
    sys.modules["gateway.config"].PlatformConfig = object
    sys.modules["gateway.platforms.base"].BasePlatformAdapter = object
    sys.modules["gateway.platforms.base"].SendResult = object
    sys.modules["hermes_cli.config"].get_hermes_home = lambda: PLUGIN_DIR
    # Only referenced by the pre-cutover plugin; harmless once the import is gone.
    sys.modules["hermes_cli.push_notifications"].dispatcher = lambda: None
    sys.modules["hermes_state"].SessionDB = object


_stub_agent_modules()
