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
import threading
import time
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

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

_desktop_wake_lock = threading.Lock()
_desktop_wake_started = False
_desktop_wake_process: subprocess.Popen | None = None
_display_wait_started = False
_CONTAINER_VNC_PORT_LOW = 5901
_CONTAINER_VNC_PORT_HIGH = 5916


def _desktop_bootstrap_command() -> list[str] | None:
    plugin_dir = Path(__file__).resolve().parent
    script = plugin_dir / "linux-computer" / "manage-computer.sh"
    manager = plugin_dir / "linux-computer" / "manage.py"
    if script.is_file():
        return [str(script), "bootstrap"]
    if manager.is_file():
        return [sys.executable, str(manager), "bootstrap"]
    return None


def _desktop_autostart_disabled() -> bool:
    return os.environ.get(DISABLE_AUTOSTART_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _host_desktop_opt_in() -> bool:
    try:
        from _computer_displays import host_desktop_opt_in

        return host_desktop_opt_in()
    except Exception:
        return False


def _load_computer_manager():
    existing = sys.modules.get("fetch_plugin_linux_computer")
    if existing is not None:
        return existing
    path = Path(__file__).resolve().parent / "linux-computer" / "manage.py"
    spec = importlib.util.spec_from_file_location("fetch_plugin_linux_computer", path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _apply_container_profile_target(environment: dict[str, str] | None) -> int | None:
    """Pin this Hermes profile to DISPLAY=:N and stream that VNC port."""

    if _host_desktop_opt_in():
        return None
    try:
        from _computer_displays import computer_target_for, display_name
    except Exception:
        return None
    display_num = None
    try:
        manager = _load_computer_manager()
        if manager is not None:
            display_num = manager.pin_and_start_display()
    except Exception:
        log.debug("Fetch could not start this bot's Ubuntu display", exc_info=True)
    if display_num is None:
        try:
            from _computer_displays import allocate_display

            display_num = allocate_display()
        except Exception:
            return None
    target = computer_target_for(display_num)
    os.environ[TARGET_ENV] = target
    if environment is not None:
        environment.setdefault(TARGET_ENV, target)
        environment.setdefault("DISPLAY", display_name(display_num))
    return display_num


def _pin_known_displays() -> list[int]:
    """Persist DISPLAY=:N for every Hermes bot profile, even before Docker is up."""

    pinned: list[int] = []
    try:
        from _computer_displays import allocate_display, hermes_profile_names

        for profile in hermes_profile_names():
            pinned.append(allocate_display(profile))
    except Exception:
        log.debug("Fetch could not pin bot DISPLAY=:N mappings", exc_info=True)
    return pinned


def _start_profile_displays_now() -> bool:
    manager = _load_computer_manager()
    if manager is None:
        return False
    engine = getattr(manager, "ENGINE", "docker")
    if not manager.container_running(engine):
        return False
    manager.start_profile_displays(engine)
    return True


def _schedule_profile_displays() -> None:
    """Start extra bot screens now, or once fetch-computer answers."""

    global _display_wait_started
    try:
        if _start_profile_displays_now():
            return
    except Exception:
        log.debug("Fetch could not start isolated bot desktops yet", exc_info=True)
        return
    with _desktop_wake_lock:
        if _display_wait_started:
            return
        _display_wait_started = True

    def _run() -> None:
        deadline = time.time() + 180
        while time.time() < deadline:
            try:
                if _start_profile_displays_now():
                    return
            except Exception:
                log.debug("Fetch stopped waiting for isolated bot desktops", exc_info=True)
                return
            time.sleep(2)

    threading.Thread(
        target=_run, daemon=True, name="fetch-computer-displays"
    ).start()


def ensure_isolated_desktops() -> str:
    """Plugin load/install: pin bots to DISPLAY=:N and boot those screens."""

    if _desktop_autostart_disabled():
        return "disabled"
    if _host_desktop_opt_in():
        return "host-opt-in"
    _pin_known_displays()
    state = wake_desktop_container()
    _schedule_profile_displays()
    return state


def _loopback_vnc_port(target: str) -> int | None:
    prefix = "tcp://127.0.0.1:"
    if not target.startswith(prefix):
        return None
    try:
        return int(target[len(prefix) :])
    except ValueError:
        return None


def _targets_match_for_keeper(live: str, persisted: str) -> bool:
    """True when the keeper should still own this host's computer bridge.

    Isolated bots share one Ubuntu box on 5901–5916. The persisted default is
    :1 / 5901; a researcher agent on :2 / 5902 is the same computer, not a
    stale reconfigure. Host-opt-in desktops still require an exact match.
    """

    if live == persisted:
        return True
    if _host_desktop_opt_in():
        return False
    live_port = _loopback_vnc_port(live)
    persisted_port = _loopback_vnc_port(persisted)
    if live_port is None or persisted_port is None:
        return False
    try:
        from _computer_displays import FIRST_DISPLAY, MAX_DISPLAYS, vnc_port_for

        low = vnc_port_for(FIRST_DISPLAY)
        high = vnc_port_for(MAX_DISPLAYS)
    except Exception:
        low, high = _CONTAINER_VNC_PORT_LOW, _CONTAINER_VNC_PORT_HIGH
    return low <= live_port <= high and low <= persisted_port <= high


def wake_desktop_container() -> str:
    """Start fetch-computer in the background when a viewer opens.

    Docker Desktop is often stopped. Tapping Watch should boot the Ubuntu
    box instead of waiting for a manual bootstrap command.
    """

    global _desktop_wake_started, _desktop_wake_process
    if _desktop_autostart_disabled():
        return "disabled"
    if _host_desktop_opt_in():
        return "host-opt-in"
    try:
        manager = _load_computer_manager()
        engine = getattr(manager, "ENGINE", "docker") if manager is not None else None
        if manager is not None and engine and manager.container_running(engine):
            try:
                manager.start_profile_displays(engine)
            except Exception:
                log.debug("Fetch could not start extra bot displays", exc_info=True)
            return "already-running"
    except Exception:
        log.debug("Fetch could not inspect the computer container", exc_info=True)
    command = _desktop_bootstrap_command()
    if command is None:
        return "unavailable"
    with _desktop_wake_lock:
        if _desktop_wake_process is not None and _desktop_wake_process.poll() is None:
            return "already-waking"
        try:
            _desktop_wake_process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
        except OSError:
            log.warning("Fetch could not start the computer container", exc_info=True)
            return "failed"
        _desktop_wake_started = True
        return "started"


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
    if not _targets_match_for_keeper(_target(), _persisted_target()):
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
    if _desktop_autostart_disabled():
        return "disabled"
    if os.environ.get(AUTOSTART_ENV, "").strip() == "1":
        return "self"
    _apply_container_profile_target(environment)
    if not _host_desktop_opt_in():
        wake_desktop_container()
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
