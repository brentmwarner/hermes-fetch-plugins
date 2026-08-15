"""Opt-in integration coverage against an installed Hermes Agent checkout."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


_HERMES_AGENT_PATH_VALUE = os.environ.get("HERMES_AGENT_PATH")
HERMES_AGENT_PATH = (
    Path(_HERMES_AGENT_PATH_VALUE) if _HERMES_AGENT_PATH_VALUE else None
)


@pytest.mark.skipif(
    HERMES_AGENT_PATH is None or not HERMES_AGENT_PATH.is_dir(),
    reason="set HERMES_AGENT_PATH to an installed Hermes Agent checkout",
)
def test_real_computer_use_dispatch_accepts_visible_delivery_contract():
    script = r'''import json
from tools.computer_use import schema, tool

properties = schema.COMPUTER_USE_SCHEMA["parameters"]["properties"]
assert "delivery_mode" in properties
assert "bring_to_front" in properties
assert "raise_window" in properties

backend = tool._NoopBackend()
tool._backend = backend
result = json.loads(tool.handle_computer_use({
    "action": "click",
    "coordinate": [10, 20],
    "delivery_mode": "foreground",
    "bring_to_front": True,
}))
assert result["ok"] is True
call_name, call_args = backend.calls[-1]
assert call_name == "click"
assert call_args["delivery_mode"] == "foreground"
assert call_args["bring_to_front"] is True
tool.reset_backend_for_tests()
'''
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(HERMES_AGENT_PATH), environment.get("PYTHONPATH", "")]
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
