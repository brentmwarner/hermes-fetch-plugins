"""Dedicated background runtime for Fetch computer streaming.

The computer bridge is intentionally independent from the Hermes chat/gateway
process. Configuring desktop access can therefore take effect immediately
without killing a live agent turn, and a chat restart cannot tear down an
active phone viewer.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

log = logging.getLogger("fetch_plugin.computer_runtime")

AUTOSTART_ENV = "HERMES_FETCH_COMPUTER_AUTOSTARTED_RUNTIME"
TARGET_ENV = "HERMES_FETCH_COMPUTER_TARGET"
LEGACY_TARGET_ENV = "HERMES_FETCH_COMPUTER_WS_URL"

_PID_FILE = "fetch-computer-runtime.pid"
_LOG_FILE = "fetch-computer-runtime.log"
_PID_ROLE = "fetch-computer-runtime"
_CONFIG_ENVS = (
    TARGET_ENV,
    LEGACY_TARGET_ENV,
    "HERMES_FETCH_COMPUTER_NAME",
    "HERMES_FETCH_COMPUTER_KIND",
    "HERMES_FETCH_RELAY_URL",
    "HERMES_FETCH_STORE_HOME",
)


def _load_runtime_module():
    existing = sys.modules.get("fetch_plugin_runtime")
    if existing is not None:
        return existing
    path = Path(__file__).resolve().parent / "_runtime.py"
    spec = importlib.util.spec_from_file_location("fetch_plugin_runtime", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _runtime_dir() -> Path:
    return _load_runtime_module()._runtime_dir()


def _pid_path() -> Path:
    return _runtime_dir() / _PID_FILE


def _credentials_path() -> Path:
    store_home = os.environ.get("HERMES_FETCH_STORE_HOME", "").strip()
    home = Path(os.path.expanduser(store_home)) if store_home else _load_runtime_module()._hermes_home()
    return home / "push" / "fetch-relay.json"


def _target() -> str:
    return os.environ.get(TARGET_ENV, os.environ.get(LEGACY_TARGET_ENV, "")).strip()


def _signature() -> str:
    values = {name: os.environ.get(name, "").strip() for name in _CONFIG_ENVS}
    try:
        credentials = json.loads(_credentials_path().read_text(encoding="utf-8"))
        values["agent_id"] = str(credentials.get("agent_id") or "")
    except (OSError, ValueError, AttributeError):
        values["agent_id"] = ""
    payload = json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _read_record(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _active_pid(*, reclaim: bool) -> int | None:
    runtime = _load_runtime_module()
    path = _pid_path()
    record = _read_record(path)
    try:
        pid = int(record.get("pid"))
    except (TypeError, ValueError):
        pid = 0
    if pid <= 0 or pid == os.getpid() or not runtime._process_alive(pid):
        if reclaim:
            try:
                path.unlink()
            except OSError:
                pass
        return None
    if record.get("role") != _PID_ROLE:
        return None
    if not reclaim or record.get("signature") == _signature():
        return pid
    if not runtime._terminate_process(pid):
        return pid
    try:
        path.unlink()
    except OSError:
        pass
    return None


def _child_script() -> str:
    plugin_dir = str(Path(__file__).resolve().parent)
    return f"""
import asyncio
import importlib.util
import os
import sys
from pathlib import Path

PLUGIN_DIR = Path({plugin_dir!r})
os.environ[{AUTOSTART_ENV!r}] = "1"

try:
    from hermes_cli.env_loader import load_hermes_dotenv
    load_hermes_dotenv()
except Exception:
    pass
os.environ[{AUTOSTART_ENV!r}] = "1"

def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module

relay = load("fetch_plugin_relay", "_relay.py")
computer = load("fetch_plugin_computer", "_computer.py")

async def main():
    client = relay.relay_client()
    creds = await client._credentials()
    target = os.environ.get(computer.COMPUTER_TARGET_ENV, os.environ.get(computer.COMPUTER_WS_URL_ENV, "")).strip()
    bridge = computer.AgentComputer(
        relay_url=creds.relay_url,
        agent_id=creds.agent_id,
        agent_secret=creds.agent_secret,
        local_target=target,
        computer_name=os.environ.get(computer.COMPUTER_NAME_ENV),
        computer_kind=os.environ.get(computer.COMPUTER_KIND_ENV),
    )
    await bridge.run_forever()

asyncio.run(main())
"""


def ensure_computer_runtime() -> str:
    if os.environ.get(AUTOSTART_ENV, "").strip() == "1":
        return "self"
    if not _target():
        return "disabled"
    if not _credentials_path().is_file():
        return "unpaired"
    if _active_pid(reclaim=True) is not None:
        return "already-running"

    runtime = _load_runtime_module()
    runtime_dir = _runtime_dir()
    log_dir = runtime._hermes_home() / "logs"
    try:
        runtime_dir.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env[AUTOSTART_ENV] = "1"
        env["PYTHONPATH"] = runtime._child_pythonpath()
        with open(log_dir / _LOG_FILE, "ab") as log_file:
            process = subprocess.Popen(
                [runtime._child_python_executable(), "-c", _child_script()],
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,
                close_fds=True,
            )
        record = {
            "pid": process.pid,
            "role": _PID_ROLE,
            "created_at": time.time(),
            "signature": _signature(),
        }
        _pid_path().write_text(json.dumps(record, separators=(",", ":")), encoding="utf-8")
        return "started"
    except Exception:
        log.warning("Fetch could not start the computer runtime", exc_info=True)
        return "failed"


def restart_computer_runtime() -> bool:
    pid = _active_pid(reclaim=False)
    if pid is None:
        return True
    runtime = _load_runtime_module()
    if not runtime._terminate_process(pid):
        return False
    try:
        _pid_path().unlink()
    except OSError:
        pass
    return True
