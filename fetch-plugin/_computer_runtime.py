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
DISABLE_AUTOSTART_ENV = "HERMES_FETCH_COMPUTER_DISABLE_AUTOSTART"
TARGET_ENV = "HERMES_FETCH_COMPUTER_TARGET"
LEGACY_TARGET_ENV = "HERMES_FETCH_COMPUTER_WS_URL"
VNC_PASSWORD_ENV = "HERMES_FETCH_COMPUTER_VNC_PASSWORD"

_PID_FILE = "fetch-computer-runtime.pid"
_LOG_FILE = "fetch-computer-runtime.log"
_PID_ROLE = "fetch-computer-runtime"
_CONFIG_ENVS = (
    TARGET_ENV,
    LEGACY_TARGET_ENV,
    "HERMES_FETCH_COMPUTER_NAME",
    "HERMES_FETCH_COMPUTER_KIND",
    VNC_PASSWORD_ENV,
    "HERMES_FETCH_RELAY_URL",
    "HERMES_FETCH_STORE_HOME",
)
# Settings computer setup persists to the Hermes ``.env`` that the spawned
# bridge reads from its inherited environment. The keeper compares each one
# against the persisted file, so the set must not include keys that are
# legitimately env-only (relay URL, store home): those would read as a
# permanent mismatch and bench every keeper.
_KEEPER_PERSISTED_ENVS = (
    "HERMES_FETCH_COMPUTER_NAME",
    "HERMES_FETCH_COMPUTER_KIND",
    VNC_PASSWORD_ENV,
)
# Setup leaves an omitted ``--name`` alone rather than scrubbing it the way an
# empty VNC password is scrubbed, so a name can legitimately live only in the
# process environment. An empty persisted value for these keys expresses no
# opinion; a persisted value must still match.
_KEEPER_ENV_ONLY_OK = ("HERMES_FETCH_COMPUTER_NAME",)


def _child_environment(environment: dict[str, str] | None) -> dict[str, str]:
    """Return an explicit child environment with Fetch configuration retained.

    Desktop setup supplies a scrubbed environment to keep stale virtual-display
    state out of a physical runtime. Retain the Fetch settings the child bridge
    reads when a caller provides a narrower environment for direct use.
    """

    if environment is None:
        return os.environ.copy()
    child_environment = dict(environment)
    for key in _CONFIG_ENVS:
        if key not in child_environment and key in os.environ:
            child_environment[key] = os.environ[key]
    return child_environment


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


def _store_home() -> Path:
    store_home = os.environ.get("HERMES_FETCH_STORE_HOME", "").strip()
    if store_home:
        return Path(os.path.expanduser(store_home))
    return _load_runtime_module()._hermes_home()


def _credentials_path() -> Path:
    return _store_home() / "push" / "fetch-relay.json"


def _target() -> str:
    return os.environ.get(TARGET_ENV, os.environ.get(LEGACY_TARGET_ENV, "")).strip()


def _persisted_environment_value(path: Path, key: str) -> str:
    """Read ``key`` from the persisted Hermes ``.env`` (last assignment wins).

    Mirrors the parsing computer setup uses when it persists configuration:
    an optional ``export `` prefix and a JSON- or shell-quoted value.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        stripped = line.strip()
        candidate = stripped[7:].lstrip() if stripped.startswith("export ") else stripped
        name, separator, value = candidate.partition("=")
        if not separator or name.strip() != key:
            continue
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, str) else ""
        except (TypeError, ValueError):
            return value.strip().strip("'\"")
    return ""


def _persisted_target() -> str:
    path = _store_home() / ".env"
    for key in (TARGET_ENV, LEGACY_TARGET_ENV):
        value = _persisted_environment_value(path, key).strip()
        if value:
            return value
    return ""


def keeper_ensure_computer_runtime() -> str:
    """Ambient ensure for the runtime keeper: defer to persisted configuration.

    A keeper thread can outlive a disable or reconfigure performed in another
    process, leaving stale computer settings in this process's environment.
    Respawning from those values would resurrect a bridge the user just
    disabled, revive a rotated VNC password or old name/kind, or fight a newly
    configured bridge. Only ensure when every persisted computer setting the
    bridge consumes still matches this process's environment; a stale host
    stays passive and leaves the bridge to the process that owns the current
    configuration.
    """
    if _target() != _persisted_target():
        return "stale-config"
    environment_path = _store_home() / ".env"
    for key in _KEEPER_PERSISTED_ENVS:
        persisted = _persisted_environment_value(environment_path, key).strip()
        if not persisted and key in _KEEPER_ENV_ONLY_OK:
            continue
        if os.environ.get(key, "").strip() != persisted:
            return "stale-config"
    return ensure_computer_runtime()


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


def _unlink_pid(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _command_looks_like_computer_runtime(command: str | None) -> bool:
    if not command:
        return False
    return AUTOSTART_ENV.lower() in command.lower()


def _active_pid(*, reclaim: bool, signature_takeover: bool = False) -> int | None:
    runtime = _load_runtime_module()
    path = _pid_path()
    record = _read_record(path)
    try:
        pid = int(record.get("pid"))
    except (TypeError, ValueError):
        pid = 0
    if pid <= 0 or pid == os.getpid() or not runtime._process_alive(pid):
        if reclaim:
            _unlink_pid(path)
        return None
    if record.get("role") != _PID_ROLE:
        if reclaim:
            _unlink_pid(path)
        return None
    command = runtime._process_command(pid)
    owned = _command_looks_like_computer_runtime(command)
    if not owned:
        if command is None:
            # Match the relay runtime: a structured owner record is trusted when
            # the live command cannot be inspected (Windows / restricted ps).
            owned = True
        else:
            if reclaim:
                _unlink_pid(path)
            return None
    if not reclaim or record.get("signature") == _signature():
        return pid
    if not signature_takeover:
        # An ambient caller (gateway hook, pairing) whose environment merely
        # differs must not replace a live runtime: two configs alternately
        # killing each other's bridge drops the computer channel on every
        # swap. Only setup flows that pass an explicit environment may swap.
        return pid
    if not runtime._terminate_process(pid):
        return pid
    _unlink_pid(path)
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
        vnc_password=os.environ.get({VNC_PASSWORD_ENV!r}),
    )
    await bridge.run_forever()

asyncio.run(main())
"""


def ensure_computer_runtime(*, environment: dict[str, str] | None = None) -> str:
    if os.environ.get(DISABLE_AUTOSTART_ENV, "").strip().lower() in {"1", "true", "yes", "on"}:
        return "disabled"
    if os.environ.get(AUTOSTART_ENV, "").strip() == "1":
        return "self"
    if not _target():
        return "disabled"
    if not _credentials_path().is_file():
        return "unpaired"
    if _active_pid(reclaim=True, signature_takeover=environment is not None) is not None:
        return "already-running"

    runtime = _load_runtime_module()
    runtime_dir = _runtime_dir()
    log_dir = runtime._hermes_home() / "logs"
    try:
        runtime_dir.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)
        env = _child_environment(environment)
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
        runtime._spawn_reaper(process)
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
    _unlink_pid(_pid_path())
    return True
