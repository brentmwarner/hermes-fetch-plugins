"""Stub Hermes-only imports before pytest loads this package's __init__.py."""

from pathlib import Path
import sys

_tests = Path(__file__).resolve().parent / "tests"
if str(_tests) not in sys.path:
    sys.path.insert(0, str(_tests))

from agent_stubs import stub_agent_modules

stub_agent_modules()
