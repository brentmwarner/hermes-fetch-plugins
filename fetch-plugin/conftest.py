"""Stub Hermes-only imports before pytest loads this package's __init__.py."""

import os
import sys
from pathlib import Path

_tests = Path(__file__).resolve().parent / "tests"
if str(_tests) not in sys.path:
    sys.path.insert(0, str(_tests))

from agent_stubs import stub_agent_modules

stub_agent_modules()

# Tests must never launch real relay/computer runtimes: leaked children connect
# to the production relay with this host's credentials and fight the installed
# agent for its single uplink slot. Tests that exercise the spawn path delete
# these explicitly.
os.environ.setdefault("HERMES_FETCH_TUNNEL_DISABLE_DASHBOARD_AUTOSTART", "1")
os.environ.setdefault("HERMES_FETCH_COMPUTER_DISABLE_AUTOSTART", "1")
