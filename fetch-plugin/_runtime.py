"""Headless Fetch relay runtime.

Relay pairing removes the need for the phone to reach a public dashboard, but
the agent side still needs a local Hermes API/WebSocket surface to serve through
the relay. This module starts that surface headlessly and keeps the tunnel
process alive after `hermes setup` exits.
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("fetch_plugin.runtime")

DASHBOARD_HOST = "127.0.0.1"
DASHBOARD_PORT = 9119
TUNNEL_ENABLED_ENV = "HERMES_FETCH_TUNNEL_ENABLED"
AUTOSTART_RUNTIME_ENV = "HERMES_FETCH_TUNNEL_AUTOSTARTED_RUNTIME"
DISABLE_AUTOSTART_ENV = "HERMES_FETCH_TUNNEL_DISABLE_DASHBOARD_AUTOSTART"

_PID_FILE = "fetch-relay-runtime.pid"
_LOG_FILE = "fetch-relay-runtime.log"


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


def _active_runtime_pid() -> int | None:
    path = _pid_path()
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    if pid == os.getpid():
        return None
    if _process_alive(pid):
        return pid
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
    return f"""
import os
import socket
import time

DASHBOARD_HOST = {DASHBOARD_HOST!r}
DASHBOARD_PORT = {DASHBOARD_PORT!r}
TUNNEL_ENABLED_ENV = {TUNNEL_ENABLED_ENV!r}
AUTOSTART_RUNTIME_ENV = {AUTOSTART_RUNTIME_ENV!r}


def dashboard_listening():
    try:
        with socket.create_connection((DASHBOARD_HOST, DASHBOARD_PORT), timeout=0.25):
            return True
    except OSError:
        return False


os.environ[AUTOSTART_RUNTIME_ENV] = "1"
os.environ[TUNNEL_ENABLED_ENV] = "1"

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
    if _active_runtime_pid() is not None:
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
    log_path = log_dir / _LOG_FILE
    try:
        with open(log_path, "ab") as log_file:
            process = subprocess.Popen(
                [sys.executable, "-c", _child_script()],
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,
                close_fds=True,
            )
        _pid_path().write_text(str(process.pid), encoding="utf-8")
        log.info("Fetch relay runtime started in background pid=%s", process.pid)
        return "started"
    except Exception:
        log.warning("Fetch failed to start relay runtime", exc_info=True)
        return "failed"
