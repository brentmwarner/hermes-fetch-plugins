"""Headless Fetch relay runtime.

Relay pairing removes the need for the phone to reach a public dashboard, but
the agent side still needs a local Hermes API/WebSocket surface to serve through
the relay. This module starts that surface headlessly and keeps the tunnel
process alive after `hermes setup` exits.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import random
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger("fetch_plugin.runtime")

DASHBOARD_HOST = "127.0.0.1"
DASHBOARD_PORT = 9119
# How often the autostarted runtime re-checks the dashboard and its own
# ownership; long enough to stay idle-cheap, short enough that a dead
# dashboard is re-served promptly.
_CHILD_POLL_S = 5.0
TUNNEL_ENABLED_ENV = "HERMES_FETCH_TUNNEL_ENABLED"
AUTOSTART_RUNTIME_ENV = "HERMES_FETCH_TUNNEL_AUTOSTARTED_RUNTIME"
DISABLE_AUTOSTART_ENV = "HERMES_FETCH_TUNNEL_DISABLE_DASHBOARD_AUTOSTART"

_PID_FILE = "fetch-relay-runtime.pid"
_LOG_FILE = "fetch-relay-runtime.log"
_PID_ROLE = "fetch-relay-runtime"
_TUNNEL_START_GRACE_S = 15.0
_MODULE_STARTED_MONOTONIC = time.monotonic()


def truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _hermes_home() -> Path:
    try:
        from hermes_cli.config import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


def _runtime_dir() -> Path:
    return _hermes_home() / "run"


def _pid_path() -> Path:
    return _runtime_dir() / _PID_FILE


def _is_zombie(pid: int) -> bool:
    """True when ``pid`` is terminated but unreaped.

    Zombies still accept signal 0, so without this check a dead runtime whose
    parent never called wait() keeps passing liveness probes and its stale
    owner records are honored forever.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return _is_zombie_ps(pid)
    # The state field follows the parenthesised comm, which may contain spaces.
    _, _, tail = stat.rpartition(")")
    fields = tail.split()
    return bool(fields) and fields[0] == "Z"


def _is_zombie_ps(pid: int) -> bool:
    try:
        out = subprocess.check_output(
            ["ps", "-o", "state=", "-p", str(pid)],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1.0,
        )
    except Exception:
        return False
    return out.strip().upper().startswith("Z")


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except PermissionError:
        return not _is_zombie(pid)
    except OSError:
        return False
    return not _is_zombie(pid)


def _process_command(pid: int) -> str | None:
    try:
        return subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "command="],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1.0,
        ).strip()
    except Exception:
        return None


def _command_looks_like_runtime(command: str | None) -> bool:
    if not command:
        return False
    lowered = command.lower()
    if "hermes_fetch_tunnel_autostarted_runtime" in lowered:
        return True
    if "hermes_cli.main" in lowered and "dashboard" in lowered:
        return True
    if "/hermes " in lowered and "dashboard" in lowered:
        return True
    return False


def _terminate_process(pid: int, *, timeout_s: float = 1.0) -> bool:
    def wait_until_gone(deadline: float) -> bool:
        while time.monotonic() < deadline:
            if not _process_alive(pid):
                return True
            time.sleep(0.05)
        return not _process_alive(pid)

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except Exception:
        log.debug("Fetch could not terminate legacy relay runtime pid=%s", pid, exc_info=True)
        return False

    deadline = time.monotonic() + max(0.0, timeout_s)
    if wait_until_gone(deadline):
        return True

    sigkill = getattr(signal, "SIGKILL", None)
    if sigkill is not None:
        try:
            os.kill(pid, sigkill)
        except ProcessLookupError:
            return True
        except Exception:
            log.debug("Fetch could not kill legacy relay runtime pid=%s", pid, exc_info=True)
            return False
        return wait_until_gone(time.monotonic() + max(0.0, timeout_s))
    return not _process_alive(pid)


def _read_pid_record(path: Path) -> tuple[int | None, str | None]:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None, None
    if not raw:
        return None, None
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except ValueError:
            return None, None
        try:
            pid = int(data.get("pid"))
        except (TypeError, ValueError):
            return None, None
        if pid <= 0:
            return None, None
        return pid, str(data.get("role") or "")
    try:
        pid = int(raw)
    except ValueError:
        return None, None
    if pid <= 0:
        return None, None
    return pid, None


def _write_pid_record(path: Path, pid: int) -> None:
    data = {
        "pid": pid,
        "role": _PID_ROLE,
        "created_at": time.time(),
    }
    path.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")


def _hermes_project_root() -> Path | None:
    try:
        import hermes_cli

        return Path(hermes_cli.__file__).resolve().parent.parent
    except Exception:
        pass
    for entry in sys.path:
        if not entry:
            continue
        candidate = Path(entry)
        if (candidate / "hermes_cli").is_dir():
            return candidate
    candidate = _hermes_home() / "hermes-agent"
    if (candidate / "hermes_cli").is_dir():
        return candidate
    return None


def _child_pythonpath() -> str:
    entries: list[str] = []
    project_root = _hermes_project_root()
    if project_root is not None:
        entries.append(str(project_root))
    return os.pathsep.join(entries)


def _child_python_executable() -> str:
    project_root = _hermes_project_root()
    if project_root is not None:
        if os.name == "nt":
            candidate = project_root / "venv" / "Scripts" / "python.exe"
        else:
            candidate = project_root / "venv" / "bin" / "python"
        if candidate.exists():
            return str(candidate)
    return sys.executable


def _active_runtime_pid(*, reclaim_legacy: bool = False) -> int | None:
    path = _pid_path()
    pid, role = _read_pid_record(path)
    if pid is None:
        return None
    if pid == os.getpid():
        return None
    alive = _process_alive(pid)
    command = _process_command(pid) if alive else None
    if alive and role != _PID_ROLE and _command_looks_like_runtime(command):
        if not reclaim_legacy:
            return pid
        log.info(
            "Fetch relay runtime pid file is legacy for pid=%s; restarting it with current plugin code",
            pid,
        )
        if not _terminate_process(pid):
            return pid
        try:
            path.unlink()
        except OSError:
            pass
        return None
    if alive and role != _PID_ROLE and command is None and not reclaim_legacy:
        return None
    # If a legacy bare-PID record points at a live process whose command cannot
    # be inspected, do not terminate it blindly. Startup drops the untrusted PID
    # file so the current plugin can start and write a structured owner record.
    if alive and _command_looks_like_runtime(command):
        return pid
    if alive and role == _PID_ROLE and command is None:
        return pid
    if not reclaim_legacy:
        return None
    try:
        path.unlink()
    except OSError:
        pass
    return None


def _dashboard_listening(host: str = DASHBOARD_HOST, port: int = DASHBOARD_PORT) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.25):
            return True
    except OSError:
        return False


def enable_tunnel_for_future_starts() -> None:
    """Persist tunnel enablement when the user completes Fetch relay setup."""
    os.environ[TUNNEL_ENABLED_ENV] = "1"
    try:
        from hermes_cli.config import save_env_value

        save_env_value(TUNNEL_ENABLED_ENV, "1")
    except Exception:
        log.debug("Could not persist %s=1", TUNNEL_ENABLED_ENV, exc_info=True)


def _child_script() -> str:
    project_root = _hermes_project_root()
    project_root_text = str(project_root) if project_root is not None else ""
    return f"""
import json
import os
import socket
import sys
import threading
import time
import traceback

DASHBOARD_HOST = {DASHBOARD_HOST!r}
DASHBOARD_PORT = {DASHBOARD_PORT!r}
TUNNEL_ENABLED_ENV = {TUNNEL_ENABLED_ENV!r}
AUTOSTART_RUNTIME_ENV = {AUTOSTART_RUNTIME_ENV!r}
PROJECT_ROOT = {project_root_text!r}
PID_PATH = {str(_pid_path())!r}
POLL_S = {_CHILD_POLL_S!r}
MAX_START_FAILURES = 5


def dashboard_listening():
    try:
        with socket.create_connection((DASHBOARD_HOST, DASHBOARD_PORT), timeout=0.25):
            return True
    except OSError:
        return False


def superseded():
    # A missing or unreadable record is not supersession: at startup the
    # spawner writes the record moments after this process boots.
    try:
        raw = open(PID_PATH, "r", encoding="utf-8").read().strip()
    except OSError:
        return False
    if not raw:
        return False
    if raw.startswith("{{"):
        try:
            pid = int(json.loads(raw).get("pid"))
        except (ValueError, TypeError):
            return False
    else:
        try:
            pid = int(raw)
        except ValueError:
            return False
    return pid > 0 and pid != os.getpid()


os.environ[AUTOSTART_RUNTIME_ENV] = "1"
os.environ[TUNNEL_ENABLED_ENV] = "1"

if PROJECT_ROOT and PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from hermes_cli.env_loader import load_hermes_dotenv
load_hermes_dotenv()

# ~/.hermes/.env intentionally has lower priority than this child role: the
# runtime exists only to keep Fetch relay pairing live.
os.environ[AUTOSTART_RUNTIME_ENV] = "1"
os.environ[TUNNEL_ENABLED_ENV] = "1"

from hermes_cli.plugins import discover_plugins
discover_plugins()

# Keep watching instead of deciding once: a dashboard that dies after this
# process boots must be taken over, and a runtime that has been replaced in
# the pid record must exit rather than sleep forever as a leaked child.
#
# The supersede watch lives on a daemon thread because a successful takeover
# blocks the main thread inside the dashboard server until it stops; the main
# loop alone would never see the pid record change while serving.
def watch_superseded():
    while True:
        time.sleep(POLL_S)
        if superseded():
            os._exit(0)


threading.Thread(target=watch_superseded, daemon=True).start()

consecutive_failures = 0
while True:
    if dashboard_listening():
        consecutive_failures = 0
    else:
        try:
            from hermes_cli.web_server import start_server
            start_server(host=DASHBOARD_HOST, port=DASHBOARD_PORT, open_browser=False, allow_public=False)
            consecutive_failures = 0
        except Exception:
            consecutive_failures += 1
            traceback.print_exc()
            if consecutive_failures >= MAX_START_FAILURES:
                # A start that keeps failing will not heal inside this
                # interpreter: exit so the pid slot frees and a later
                # ensure_relay_runtime() boots a fresh process instead of
                # this one squatting on the record as "already-running".
                sys.exit(1)
    time.sleep(POLL_S)
"""


_keeper_lock = threading.Lock()
_keeper_running = False
_keeper_stop_event: threading.Event | None = None


def start_runtime_keeper(
    *,
    should_run,
    extra_ensures=(),
    interval_s: float = 60.0,
    jitter_s: float = 10.0,
) -> bool:
    """Keep the detached runtimes alive for the life of this process.

    Reconfigure flows stop the relay runtime and normally restart it, but the
    restart leg can be lost: the stopping process is killed mid-flow, errors
    out, or never gets to run its follow-up ensure. Without a keeper the agent
    then stays offline for every paired device until something happens to call
    ``ensure_relay_runtime()`` again. Each long-lived host process ticks this
    keeper instead, so a stopped runtime is respawned within about a minute.

    Ticks are cheap while healthy (a pid-file read). Concurrent keepers in
    several processes are safe: the ensures are pid-file guarded and a
    double-spawned runtime child exits on its own once the pid record names
    its sibling. Returns False when this process already has a keeper.
    """
    if not interval_s > 0.0:  # also rejects NaN
        raise ValueError("interval_s must be a positive number of seconds")
    global _keeper_running, _keeper_stop_event
    with _keeper_lock:
        if _keeper_running:
            return False
        _keeper_running = True
        stop = threading.Event()
        _keeper_stop_event = stop

    def _tick_forever() -> None:
        failing = False
        while not stop.wait(interval_s + random.uniform(0.0, max(0.0, jitter_s))):
            try:
                if not should_run():
                    continue
                if ensure_relay_runtime() == "started":
                    log.info("Fetch keeper restarted the relay runtime; it was not running")
                for ensure in extra_ensures:
                    if ensure() == "started":
                        log.info("Fetch keeper restarted a companion runtime; it was not running")
                if failing:
                    failing = False
                    log.info("Fetch runtime keeper recovered; ticks are healthy again")
            except Exception:
                # First failure of a streak is loud: a keeper that fails
                # silently reads as "protected" while the agent stays offline.
                if not failing:
                    failing = True
                    log.warning("Fetch runtime keeper tick failed", exc_info=True)
                else:
                    log.debug("Fetch runtime keeper tick failed", exc_info=True)

    threading.Thread(target=_tick_forever, name="fetch-runtime-keeper", daemon=True).start()
    log.info("Fetch runtime keeper active (pid=%s, interval~%ss)", os.getpid(), int(interval_s))
    return True


def _stop_runtime_keeper_for_tests() -> None:
    global _keeper_running
    with _keeper_lock:
        if _keeper_stop_event is not None:
            _keeper_stop_event.set()
        _keeper_running = False


def _sibling(module_name: str, filename: str):
    """Load a sibling plugin module lazily, reusing an already-loaded copy.

    Mirrors ``_computer_runtime._load_runtime_module`` so every caller shares
    one module object (and therefore one keeper) per process.
    """
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    path = Path(__file__).resolve().parent / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def keeper_should_run() -> bool:
    """The tunnel-autostart gate, evaluated fresh on every keeper tick.

    Same decision as the package registration gate, but importable from any
    host process (gateway adapter, timer tick) without touching the package
    ``__init__``: an explicit HERMES_FETCH_TUNNEL_ENABLED wins; otherwise a
    paired agent keeps its runtimes alive.
    """
    configured = os.environ.get(TUNNEL_ENABLED_ENV)
    if configured is not None and configured.strip():
        return truthy(configured)
    try:
        pairing = _sibling("fetch_plugin_pairing", "_pairing.py")
        return bool(pairing.is_pairing_configured())
    except Exception:
        log.debug("Fetch keeper could not evaluate pairing state", exc_info=True)
        return False


def start_default_runtime_keeper() -> bool:
    """Start the in-process keeper with the standard gate and companions.

    The computer ensure is the keeper-scoped variant so a host whose
    environment went stale after a disable/reconfigure elsewhere cannot
    resurrect the old bridge.
    """

    def _ensure_computer() -> str:
        computer = _sibling("fetch_plugin_computer_runtime", "_computer_runtime.py")
        return computer.keeper_ensure_computer_runtime()

    return start_runtime_keeper(
        should_run=keeper_should_run,
        extra_ensures=(_ensure_computer,),
    )


_KEEPER_UNIT_NAME = "fetch-runtime-keeper"
_KEEPER_TICK_FILE = "keeper_tick.py"


def _systemd_user_dir() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(config_home) if config_home else Path.home() / ".config"
    return base / "systemd" / "user"


def _keeper_unit_texts() -> dict[str, str]:
    tick_script = Path(__file__).resolve().parent / _KEEPER_TICK_FILE
    service = (
        "[Unit]\n"
        "Description=Fetch runtime keeper (respawn relay/computer runtimes)\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={_child_python_executable()} {tick_script}\n"
    )
    timer = (
        "[Unit]\n"
        "Description=Run the Fetch runtime keeper every minute\n"
        "\n"
        "[Timer]\n"
        "OnBootSec=90\n"
        "OnUnitActiveSec=60\n"
        "AccuracySec=10\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    return {
        f"{_KEEPER_UNIT_NAME}.service": service,
        f"{_KEEPER_UNIT_NAME}.timer": timer,
    }


def _systemctl_user(*args: str) -> bool:
    try:
        subprocess.run(
            ["systemctl", "--user", *args],
            check=True,
            capture_output=True,
            timeout=15.0,
        )
        return True
    except Exception:
        log.debug("systemctl --user %s failed", " ".join(args), exc_info=True)
        return False


def ensure_keeper_units() -> str:
    """Install/refresh the supervised keeper: a systemd user timer.

    In-process keepers die with their host process, and on some deployments no
    long-lived process loads this module at all (gateways only load the inbox
    adapter). A user-level timer survives every Hermes process, so a runtime
    stopped by an interrupted reconfigure heals within a minute regardless of
    which processes are running. Returns "installed", "unchanged",
    "unsupported", or "failed".
    """
    if sys.platform != "linux" or shutil.which("systemctl") is None:
        return "unsupported"
    unit_dir = _systemd_user_dir()
    try:
        unit_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        log.debug("Fetch could not create systemd user unit dir", exc_info=True)
        return "failed"
    changed = False
    for name, text in _keeper_unit_texts().items():
        path = unit_dir / name
        try:
            if path.read_text(encoding="utf-8") == text:
                continue
        except OSError:
            pass
        try:
            path.write_text(text, encoding="utf-8")
            changed = True
        except OSError:
            log.debug("Fetch could not write %s", path, exc_info=True)
            return "failed"
    if changed and not _systemctl_user("daemon-reload"):
        return "failed"
    if not _systemctl_user("enable", "--now", f"{_KEEPER_UNIT_NAME}.timer"):
        return "failed"
    if changed:
        log.info("Fetch supervised keeper timer installed (%s.timer)", _KEEPER_UNIT_NAME)
        return "installed"
    return "unchanged"


def restart_relay_runtime_for_reconfigure() -> dict:
    """Hand the relay uplink over cleanly after (re)pairing.

    Stops the autostarted relay runtime child and any tunnel-owner lock holder
    that is itself an autostarted runtime, so the following
    ``ensure_relay_runtime()`` boots a fresh process that reads the credentials
    setup just wrote — instead of an old PID looping 403s against the relay
    with the credentials it booted with.

    A live agent/gateway process that owns the uplink is deliberately NOT
    killed (that would tear down the user's sessions); its tunnel self-heals by
    reloading credentials from disk on the next auth rejection.
    """
    stopped: list[int] = []
    left_running: list[int] = []

    pid = _active_runtime_pid()
    if pid is not None and _terminate_process(pid):
        stopped.append(pid)
        try:
            _pid_path().unlink()
        except OSError:
            pass

    try:
        locks = sorted(_runtime_dir().glob("fetch-tunnel-*.pid"))
    except OSError:
        locks = []
    for lock in locks:
        lock_pid, _role = _read_pid_record(lock)
        if lock_pid is None or lock_pid == os.getpid() or not _process_alive(lock_pid):
            # Dead/corrupt/self-owned lock: clear it so the fresh runtime can
            # claim ownership immediately.
            try:
                lock.unlink()
            except OSError:
                pass
            continue
        command = _process_command(lock_pid)
        if command and AUTOSTART_RUNTIME_ENV.lower() in command.lower():
            if _terminate_process(lock_pid):
                stopped.append(lock_pid)
                try:
                    lock.unlink()
                except OSError:
                    pass
            continue
        left_running.append(lock_pid)

    if stopped:
        log.info("Fetch stopped superseded relay processes for reconfigure: %s", stopped)
    if left_running:
        log.info(
            "Fetch left live tunnel owner(s) %s running during reconfigure; "
            "they adopt the new credentials from disk on their next reconnect",
            left_running,
        )
    return {"stopped": stopped, "left_running": left_running}


def _spawn_reaper(process) -> None:
    """Collect the child's exit status so it can never linger as a zombie.

    The Popen handle is discarded by callers; in a long-lived spawner such as
    ``hermes gateway setup`` an un-waited child zombifies when the runtime is
    later killed, and the corpse then wedges the tunnel-owner lock.
    """

    def _reap() -> None:
        try:
            process.wait()
        except Exception:
            pass

    threading.Thread(target=_reap, name="fetch-runtime-reaper", daemon=True).start()


def _runtime_record_age_s(pid: int) -> float | None:
    """Age of the structured runtime record when it still names ``pid``."""
    try:
        data = json.loads(_pid_path().read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        record_pid = int(data.get("pid"))
        created_at = float(data.get("created_at"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError, AttributeError):
        return None
    if record_pid != pid or created_at <= 0:
        return None
    return max(0.0, time.time() - created_at)


def _current_tunnel_owner_status() -> dict | None:
    """Inspect the owner lock for the currently persisted relay identity.

    ``None`` means the probe itself is unavailable, not that the owner is
    unhealthy. Recovery must fail open here so a diagnostics/import problem
    cannot churn a working runtime.
    """
    try:
        relay = _sibling("fetch_plugin_relay", "_relay.py")
        relay_client = relay.relay_client()
        credentials = relay_client._read_credentials()
        if credentials is None:
            return None
        tunnel = _sibling("fetch_plugin_tunnel", "_tunnel.py")
        lock_dir = Path(relay_client.credentials_path).parent.parent / "run"
        owner = tunnel.TunnelOwnerLock(
            agent_id=credentials.agent_id,
            lock_dir=lock_dir,
        )
        return owner.status()
    except Exception:
        log.debug("Fetch could not inspect tunnel ownership for runtime recovery", exc_info=True)
        return None


def _unhealthy_tunnel_owner(runtime_pid: int) -> dict | None:
    """Return a recoverable unhealthy owner status after startup grace."""
    status = _current_tunnel_owner_status()
    if status is None or status.get("state") not in {
        "stale",
        "invalid",
        "foreign",
        "unowned",
    }:
        return None
    if status.get("state") == "unowned" and runtime_pid == os.getpid():
        # The child calls ensure_relay_runtime() from _spawn_tunnel before it
        # starts the tunnel, so unowned is the expected pre-tunnel state.
        # Treating it as failed after grace replaces this process, returns
        # "started", and skips the tunnel — a slow discover_plugins path then
        # respawns successors forever.
        return None

    age_s = _runtime_record_age_s(runtime_pid)
    if age_s is None and runtime_pid == os.getpid():
        # The child can import the plugin before its parent writes the pid
        # record. Module uptime still prevents that startup race from replacing
        # a healthy child before its tunnel thread has acquired the owner lock.
        age_s = time.monotonic() - _MODULE_STARTED_MONOTONIC
    if age_s is not None and age_s < _TUNNEL_START_GRACE_S:
        return None
    return status


def ensure_relay_runtime(*, environment: dict[str, str] | None = None) -> str:
    """Start a long-lived headless relay runtime unless one is already running.

    Returns one of:
      - "started": spawned a background process.
      - "already-running": a previous runtime PID is still alive.
      - "self": this process is already the autostart child.
      - "disabled": autostart is explicitly disabled.
      - "failed": spawning failed; callers may fall back to inline tunnel start.
    """
    if truthy(os.environ.get(DISABLE_AUTOSTART_ENV)):
        return "disabled"

    is_runtime_child = truthy(os.environ.get(AUTOSTART_RUNTIME_ENV))
    runtime_pid = os.getpid() if is_runtime_child else _active_runtime_pid(reclaim_legacy=True)
    if runtime_pid is not None:
        unhealthy_owner = _unhealthy_tunnel_owner(runtime_pid)
        if unhealthy_owner is None:
            return "self" if is_runtime_child else "already-running"

        log.warning(
            "Fetch relay runtime pid=%s has tunnel owner state=%s (owner pid=%s); replacing it",
            runtime_pid,
            unhealthy_owner.get("state"),
            unhealthy_owner.get("owner_pid") or "unknown",
        )
        if not is_runtime_child:
            if not _terminate_process(runtime_pid):
                return "already-running"
            current_pid, _role = _read_pid_record(_pid_path())
            if current_pid == runtime_pid:
                try:
                    _pid_path().unlink()
                except OSError:
                    pass
        # A runtime child replaces itself by spawning its successor and writing
        # the successor pid below. The child's supersession watcher then exits
        # the old dashboard process cleanly while the new tunnel takes over.

    log_dir = _hermes_home() / "logs"
    runtime_dir = _runtime_dir()
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        runtime_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        log.warning("Fetch could not create runtime/log directories", exc_info=True)
        return "failed"

    env = dict(environment) if environment is not None else os.environ.copy()
    env[TUNNEL_ENABLED_ENV] = "1"
    env[AUTOSTART_RUNTIME_ENV] = "1"
    env["PYTHONPATH"] = _child_pythonpath()
    log_path = log_dir / _LOG_FILE
    try:
        with open(log_path, "ab") as log_file:
            process = subprocess.Popen(
                [_child_python_executable(), "-c", _child_script()],
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,
                close_fds=True,
            )
        _spawn_reaper(process)
        _write_pid_record(_pid_path(), process.pid)
        log.info("Fetch relay runtime started in background pid=%s", process.pid)
        return "started"
    except Exception:
        log.warning("Fetch failed to start relay runtime", exc_info=True)
        return "failed"
