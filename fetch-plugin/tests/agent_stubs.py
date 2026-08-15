"""Lightweight stand-ins for Hermes-only modules used by plugin imports."""

from __future__ import annotations

import sys
import types
from pathlib import Path


def stub_agent_modules() -> None:
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
    try:
        import httpx  # noqa: F401
    except ImportError:
        sys.modules.setdefault("httpx", types.ModuleType("httpx"))
    sys.modules["hermes_cli"].__path__ = []
    sys.modules["gateway.config"].Platform = lambda *a, **k: None
    sys.modules["gateway.config"].PlatformConfig = object
    sys.modules["gateway.platforms.base"].BasePlatformAdapter = object
    sys.modules["gateway.platforms.base"].SendResult = object
    sys.modules["hermes_cli.config"].get_hermes_home = lambda: Path.home() / ".hermes"
    sys.modules["hermes_cli.cli_output"].prompt_yes_no = lambda *a, **k: False
    sys.modules["hermes_cli.cli_output"].print_header = print
    sys.modules["hermes_cli.cli_output"].print_info = print
    sys.modules["hermes_cli.cli_output"].print_success = print
    sys.modules["hermes_cli.cli_output"].print_warning = print

    class _StubSessionDB:
        def __init__(self, *a, **kw):
            pass

        def get_session(self, session_id):
            return None

        def close(self):
            pass

    sys.modules["hermes_state"].SessionDB = _StubSessionDB
