"""Headless Fetch relay runtime.

Relay pairing removes the need for the phone to reach a public dashboard, but
the agent side still needs a local Hermes API/WebSocket surface to serve through
the relay. This module starts that surface headlessly and keeps the tunnel
process alive after `hermes setup` exits.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

log = logging.getLogger("fetch_plugin.runtime")

DASHBOARD_HOST = "127.0.0.1"
DASHBOARD_PORT = 9119
TUNNEL_ENABLED_ENV = "HERMES_FETCH_TUNNEL_ENABLED"
AUTOSTART_RUNTIME_ENV = "HERMES_FETCH_TUNNEL_AUTOSTARTED_RUNTIME"
DISABLE_AUTOSTART_ENV = "HERMES_FETCH_TUNNEL_DISABLE_DASHBOARD_AUTOSTART"

_PID_FILE = "fetch-relay-runtime.pid"
_LOG_FILE = "fetch-relay-runtime.log"
_PID_ROLE = "fetch-relay-runtime"


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


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


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
import os
import socket
import sys
import time

DASHBOARD_HOST = {DASHBOARD_HOST!r}
DASHBOARD_PORT = {DASHBOARD_PORT!r}
TUNNEL_ENABLED_ENV = {TUNNEL_ENABLED_ENV!r}
AUTOSTART_RUNTIME_ENV = {AUTOSTART_RUNTIME_ENV!r}
PROJECT_ROOT = {project_root_text!r}


def dashboard_listening():
    try:
        with socket.create_connection((DASHBOARD_HOST, DASHBOARD_PORT), timeout=0.25):
            return True
    except OSError:
        return False


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

if dashboard_listening():
    while True:
        time.sleep(3600)
else:
    from hermes_cli.web_server import start_server
    start_server(host=DASHBOARD_HOST, port=DASHBOARD_PORT, open_browser=False, allow_public=False)
"""


def ensure_relay_runtime() -> str:
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
    if truthy(os.environ.get(AUTOSTART_RUNTIME_ENV)):
        return "self"
    if _active_runtime_pid(reclaim_legacy=True) is not None:
        return "already-running"

    log_dir = _hermes_home() / "logs"
    runtime_dir = _runtime_dir()
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        runtime_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        log.warning("Fetch could not create runtime/log directories", exc_info=True)
        return "failed"

    env = os.environ.copy()
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
        _write_pid_record(_pid_path(), process.pid)
        log.info("Fetch relay runtime started in background pid=%s", process.pid)
        return "started"
    except Exception:
        log.warning("Fetch failed to start relay runtime", exc_info=True)
        return "failed"
