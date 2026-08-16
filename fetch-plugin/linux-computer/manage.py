#!/usr/bin/env python3
"""Start and stop the portable Fetch computer container."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

CONTAINER_NAME = "fetch-computer"
IMAGE_NAME = "fetch-computer:local"
ENGINE = "docker"
RFB_HOST = "127.0.0.1"
RFB_PORT = 5901
DISPLAY_NAME = ":1"
GEOMETRY = "1280x800"
TARGET = f"tcp://{RFB_HOST}:{RFB_PORT}"
KIND = "Virtual Linux desktop"
NAME = "Fetch computer"
TARGET_ENV = "HERMES_FETCH_COMPUTER_TARGET"
CONTAINER_XAUTHORITY = "/home/fetch/.Xauthority"
READY_MESSAGE = "Fetch computer is ready. Fetch Watch can use this desktop."


class ComputerError(RuntimeError):
    pass


def image_dir() -> Path:
    return Path(__file__).resolve().parent


def plugin_dir() -> Path:
    return image_dir().parent


def runtime_dir() -> Path:
    return Path.home() / ".local" / "share" / "fetch-computer"


def container_home_dir() -> Path:
    return runtime_dir() / "home"


def gui_run_wrapper() -> Path:
    return image_dir() / "fetch-computer-run.sh"


def gui_chrome_wrapper() -> Path:
    return image_dir() / "fetch-computer-chrome.sh"


def normalized_platform(platform: str | None = None) -> str:
    value = (platform or sys.platform or "").lower()
    if value.startswith("linux"):
        return "linux"
    if value.startswith("darwin"):
        return "darwin"
    if value.startswith("win"):
        return "win32"
    return value


def is_native_linux_host(platform: str | None = None) -> bool:
    """True on a real Linux kernel. Docker Desktop for Mac/Windows is a Linux VM."""

    return normalized_platform(platform) == "linux"


def is_macos_host(platform: str | None = None) -> bool:
    return normalized_platform(platform) == "darwin"


def is_windows_host(platform: str | None = None) -> bool:
    return normalized_platform(platform) == "win32"


def selinux_enforcing() -> bool:
    path = Path("/sys/fs/selinux/enforce")
    try:
        return path.read_text(encoding="utf-8").strip() == "1"
    except OSError:
        return False


def engine_binaries() -> list[str]:
    return [ENGINE] if shutil.which(ENGINE) else []


def _command_text(args: list[str], *, timeout: float = 8.0) -> str:
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return f"{result.stdout or ''}\n{result.stderr or ''}"


def docker_looks_like_podman(engine: str = ENGINE) -> bool:
    """True when `docker` is Podman or a podman-docker shim."""

    path = shutil.which(engine)
    if path:
        real = os.path.realpath(path)
        if "podman" in real.lower():
            return True
        try:
            head = Path(path).read_bytes()[:4096]
        except OSError:
            head = b""
        if b"podman" in head.lower():
            return True
    blob = (
        _command_text([engine, "--version"]) + "\n" + _command_text([engine, "info"])
    ).lower()
    return "podman" in blob


def engine_daemon_ready(engine: str, *, timeout: float = 8.0) -> bool:
    try:
        result = subprocess.run(
            [engine, "info"],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def bootstrap_command(*, platform: str | None = None) -> str:
    if is_windows_host(platform):
        return f'python "{image_dir() / "manage.py"}" bootstrap'
    return f"{image_dir() / 'manage-computer.sh'} bootstrap"


def stop_command(*, platform: str | None = None) -> str:
    if is_windows_host(platform):
        return f'python "{image_dir() / "manage.py"}" stop'
    return f"{image_dir() / 'manage-computer.sh'} stop"


def stop_and_retry_commands(*, platform: str | None = None) -> str:
    return f"  {stop_command(platform=platform)}\n  {bootstrap_command(platform=platform)}"


def _linux_engine_install_steps() -> str:
    return (
        "  Ubuntu/Debian:\n"
        "    sudo apt-get update && sudo apt-get install -y docker.io\n"
        "    sudo systemctl enable --now docker\n"
        "  Fedora:\n"
        "    sudo dnf install -y moby-engine\n"
        "    sudo systemctl enable --now docker\n"
    )


def _macos_engine_install_steps() -> str:
    return (
        "  macOS:\n"
        "    Install Docker Desktop for Mac (Apple Silicon or Intel, matching this Mac).\n"
        "    https://docs.docker.com/desktop/setup/install/mac-install/\n"
        "    Open Docker Desktop and wait until `docker info` works.\n"
    )


def _windows_engine_install_steps() -> str:
    return (
        "  Windows:\n"
        "    Install Docker Desktop for Windows.\n"
        "    https://docs.docker.com/desktop/setup/install/windows-install/\n"
        "    Start Docker Desktop and wait until `docker info` works.\n"
        "    Then run `python manage.py bootstrap` (or `manage-computer.sh bootstrap`).\n"
    )


def engine_install_steps(*, platform: str | None = None) -> str:
    host = normalized_platform(platform)
    if host == "darwin":
        return _macos_engine_install_steps()
    if host == "win32":
        return _windows_engine_install_steps()
    return _linux_engine_install_steps()


def engine_missing_instructions(*, platform: str | None = None) -> str:
    return (
        "Fetch computer setup requires Docker. "
        "Install Docker, start it, then bootstrap once.\n"
        "\n"
        f"{engine_install_steps(platform=platform)}"
        "\n"
        f"  Then run:\n    {bootstrap_command(platform=platform)}"
    )


def engine_shim_instructions(*, platform: str | None = None) -> str:
    return (
        "Fetch computer setup requires real Docker, not Podman or podman-docker. "
        "Install Docker, start it, then bootstrap once.\n"
        "\n"
        f"{engine_install_steps(platform=platform)}"
        "\n"
        f"  Then run:\n    {bootstrap_command(platform=platform)}"
    )


def engine_not_running_instructions(*, platform: str | None = None) -> str:
    if is_macos_host(platform):
        start_step = (
            "  Open Docker Desktop and wait until `docker info` works.\n"
        )
        lead = "Docker is installed, but Docker Desktop is not running.\n"
    elif is_windows_host(platform):
        start_step = (
            "  Start Docker Desktop and wait until `docker info` works.\n"
        )
        lead = "Docker is installed, but Docker Desktop is not running.\n"
    else:
        start_step = "  sudo systemctl start docker\n"
        lead = "Docker is installed, but the daemon is not running.\n"
    return (
        f"{lead}"
        "\n"
        f"{start_step}"
        "\n"
        f"  Then run:\n    {bootstrap_command(platform=platform)}"
    )


def container_missing_instructions(*, platform: str | None = None) -> str:
    return (
        "The Fetch computer container is not running. Start it once; after that "
        "the engine restart policy (`unless-stopped`) brings it back automatically.\n"
        "\n"
        f"  {bootstrap_command(platform=platform)}"
    )


def display_or_port_busy_instructions(*, platform: str | None = None) -> str:
    return (
        f"Display {DISPLAY_NAME} or port {RFB_PORT} is already in use. "
        "Stop the other computer, then run:\n"
        f"{stop_and_retry_commands(platform=platform)}"
    )


def extra_container_instructions(names: list[str], *, platform: str | None = None) -> str:
    listed = ", ".join(names)
    return (
        f"Another Fetch computer container is already running ({listed}). "
        "Use one container named fetch-computer. Stop it, then run:\n"
        f"{stop_and_retry_commands(platform=platform)}"
    )


def desktop_unhealthy_instructions(*, platform: str | None = None) -> str:
    return (
        "The Fetch computer is running, but a desktop client inside the container "
        "could not open the VNC X server. Stop it, then run:\n"
        f"{stop_and_retry_commands(platform=platform)}"
    )


def rfb_down_instructions(*, platform: str | None = None) -> str:
    return (
        f"The Fetch computer is running, but VNC is not answering on "
        f"{RFB_HOST}:{RFB_PORT}. Stop it, then run:\n"
        f"{stop_and_retry_commands(platform=platform)}"
    )


def detect_engine(*, platform: str | None = None) -> str:
    if not engine_binaries():
        raise ComputerError(engine_missing_instructions(platform=platform))
    if docker_looks_like_podman(ENGINE):
        raise ComputerError(engine_shim_instructions(platform=platform))
    if not engine_daemon_ready(ENGINE):
        raise ComputerError(engine_not_running_instructions(platform=platform))
    return ENGINE


def reject_non_loopback_publish(args: list[str]) -> list[str]:
    """Fail closed if a caller tries to publish VNC on a non-loopback address."""

    for index, arg in enumerate(args):
        if arg not in {"-p", "--publish"}:
            continue
        if index + 1 >= len(args):
            raise ComputerError("VNC publish is missing a host:port value.")
        value = args[index + 1]
        if "0.0.0.0" in value:
            raise ComputerError("Refusing to publish VNC on 0.0.0.0.")
        if not (value.startswith(f"{RFB_HOST}:") or value.startswith("localhost:")):
            raise ComputerError(f"VNC publish must be loopback-only, got {value}.")
    joined = " ".join(args)
    if "0.0.0.0" in joined:
        raise ComputerError("Refusing to publish VNC on 0.0.0.0.")
    return args


def container_run_args(
    *,
    engine: str,
    home_dir: str,
    restart: str | None = "unless-stopped",
    selinux: bool = False,
    uid: int | None = None,
    gid: int | None = None,
    image: str = IMAGE_NAME,
    name: str = CONTAINER_NAME,
) -> list[str]:
    if name != CONTAINER_NAME:
        raise ComputerError(
            f"Refusing to start a second computer named {name}. "
            f"The only computer container is {CONTAINER_NAME}."
        )
    args = [engine, "run", "-d", "--name", name, "--init", "--shm-size", "2g"]
    if restart:
        args.extend(["--restart", restart])
    if selinux:
        args.extend(["--security-opt", "label=disable"])
    if uid is not None and gid is not None:
        args.extend(["--user", f"{uid}:{gid}"])
    args.extend(
        [
            "-p",
            f"{RFB_HOST}:{RFB_PORT}:{RFB_PORT}",
            "-e",
            "FETCH_VNC_LOCALHOST=0",
            "-e",
            f"DISPLAY={DISPLAY_NAME}",
            "-e",
            f"FETCH_RFB_PORT={str(RFB_PORT)}",
            "-e",
            f"FETCH_GEOMETRY={GEOMETRY}",
            "-e",
            "HOME=/home/fetch",
            "-v",
            f"{home_dir}:/home/fetch",
        ]
    )
    args.append(image)
    return reject_non_loopback_publish(args)


def container_exists(engine: str, name: str = CONTAINER_NAME) -> bool:
    result = subprocess.run(
        [engine, "inspect", name],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def container_running(engine: str, name: str = CONTAINER_NAME) -> bool:
    result = subprocess.run(
        [engine, "inspect", "-f", "{{.State.Running}}", name],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip().lower() == "true"


def extra_computer_containers(engine: str) -> list[str]:
    names: set[str] = set()
    listed = _command_text(
        [engine, "ps", "-a", "--filter", f"ancestor={IMAGE_NAME}", "--format", "{{.Names}}"]
    )
    names.update(line.strip() for line in listed.splitlines() if line.strip())
    all_names = _command_text([engine, "ps", "-a", "--format", "{{.Names}}"])
    for line in all_names.splitlines():
        name = line.strip()
        if name.startswith("fetch-computer") and name != CONTAINER_NAME:
            names.add(name)
    names.discard(CONTAINER_NAME)
    return sorted(names)


def hermes_env_path() -> Path:
    store_home = os.environ.get("HERMES_FETCH_STORE_HOME", "").strip()
    if store_home:
        return Path(os.path.expanduser(store_home)) / ".env"
    hermes_home = os.environ.get("HERMES_HOME", "").strip()
    if hermes_home:
        return Path(os.path.expanduser(hermes_home)) / ".env"
    return Path.home() / ".hermes" / ".env"


def _env_file_value(path: Path, key: str) -> str:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    found = ""
    for line in lines:
        raw = line.strip()
        if raw.startswith("export "):
            raw = raw[7:].lstrip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        name, _separator, value = raw.partition("=")
        if name.strip() != key:
            continue
        try:
            decoded = json.loads(value)
            found = decoded if isinstance(decoded, str) else ""
        except (TypeError, ValueError):
            found = value.strip().strip("'\"")
    return found


def configured_computer_target() -> str:
    return (os.environ.get(TARGET_ENV, "").strip() or _env_file_value(hermes_env_path(), TARGET_ENV))


def parse_tcp_target(target: str) -> tuple[str, int] | None:
    if not target.startswith("tcp://"):
        return None
    rest = target[len("tcp://") :]
    host, separator, port_text = rest.rpartition(":")
    if not separator or not host:
        return None
    try:
        return host, int(port_text)
    except ValueError:
        return None


def existing_loopback_desktop_ready() -> bool:
    """True when an opt-in host desktop already answers.

    Mac Screen Sharing, Windows UltraVNC, linux-desktop, and linux-vps persist a
    loopback target. Leave those setups alone so bootstrap does not clobber them.
    New hosts with no target default to the fetch-computer container.
    """

    target = configured_computer_target()
    if not target:
        return False
    parsed = parse_tcp_target(target)
    if parsed is None:
        return True
    host, port = parsed
    return rfb_port_open(host, port)


def container_exec_args(
    engine: str,
    *command: str,
    name: str = CONTAINER_NAME,
) -> list[str]:
    return [
        engine,
        "exec",
        "-e",
        f"DISPLAY={DISPLAY_NAME}",
        "-e",
        f"XAUTHORITY={CONTAINER_XAUTHORITY}",
        name,
        *command,
    ]


def desktop_client_can_open(engine: str, name: str = CONTAINER_NAME) -> bool:
    """True when a client inside the container can talk to the VNC X server."""

    try:
        result = subprocess.run(
            container_exec_args(
                engine,
                "sh",
                "-c",
                "xdpyinfo >/dev/null && xset q >/dev/null && xsetroot -cursor_name left_ptr",
                name=name,
            ),
            capture_output=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def doctor_desktop(*, engine: str | None = None, platform: str | None = None) -> dict[str, str]:
    """Prove the managed container desktop works, or return the next fix."""

    if engine is None:
        binaries = engine_binaries()
        if not binaries:
            return {
                "state": "engine-missing",
                "message": engine_missing_instructions(platform=platform),
            }
        if docker_looks_like_podman(ENGINE):
            return {
                "state": "engine-shim",
                "message": engine_shim_instructions(platform=platform),
            }
        if not engine_daemon_ready(ENGINE):
            return {
                "state": "engine-not-running",
                "message": engine_not_running_instructions(platform=platform),
            }
        engine = ENGINE
    extras = extra_computer_containers(engine)
    if extras:
        return {
            "state": "extra-container",
            "engine": engine,
            "message": extra_container_instructions(extras, platform=platform),
        }
    if not container_running(engine):
        return {
            "state": "container-absent",
            "engine": engine,
            "message": container_missing_instructions(platform=platform),
        }
    if not rfb_port_open():
        return {
            "state": "rfb-down",
            "engine": engine,
            "message": rfb_down_instructions(platform=platform),
        }
    if not desktop_client_can_open(engine):
        return {
            "state": "desktop-unhealthy",
            "engine": engine,
            "message": desktop_unhealthy_instructions(platform=platform),
        }
    return {"state": "ready", "engine": engine, "message": READY_MESSAGE}


def computer_readiness(*, platform: str | None = None) -> dict[str, str]:
    binaries = engine_binaries()
    if (
        binaries
        and not docker_looks_like_podman(ENGINE)
        and engine_daemon_ready(ENGINE)
    ):
        extras = extra_computer_containers(ENGINE)
        if extras:
            return {
                "state": "extra-container",
                "engine": ENGINE,
                "message": extra_container_instructions(extras, platform=platform),
            }
        if container_running(ENGINE):
            return doctor_desktop(engine=ENGINE, platform=platform)
    if existing_loopback_desktop_ready():
        return {
            "state": "ready",
            "message": "Fetch computer is already configured on this host.",
        }
    return doctor_desktop(platform=platform)


def guide_computer(*, offer_bootstrap: bool = True, printer=print) -> str:
    """Print copy-pasteable next steps and optionally start the container."""

    report = computer_readiness()
    if report["state"] == "configured":
        return report["state"]
    printer(report["message"])
    if report["state"] != "container-absent" or not offer_bootstrap:
        return report["state"]
    if not sys.stdin.isatty():
        return report["state"]
    try:
        from hermes_cli.cli_output import prompt_yes_no
    except Exception:
        return report["state"]
    if not prompt_yes_no("Start the Fetch computer container now?", True):
        return report["state"]
    try:
        install()
    except ComputerError as exc:
        printer(f"Fetch computer setup failed: {exc}")
        printer(f"Retry with:\n  {bootstrap_command()}")
        return "failed"
    printer(READY_MESSAGE)
    return "started"


def rfb_port_open(host: str = RFB_HOST, port: int = RFB_PORT) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.4):
            return True
    except OSError:
        return False


def host_user_ids() -> tuple[int | None, int | None]:
    getuid = getattr(os, "getuid", None)
    getgid = getattr(os, "getgid", None)
    if not callable(getuid) or not callable(getgid):
        return None, None
    try:
        return int(getuid()), int(getgid())
    except (OSError, TypeError, ValueError):
        return None, None


def linux_volume_ownership(*, platform: str | None = None) -> tuple[int | None, int | None, bool]:
    """Host uid/gid and SELinux only apply on real Linux.

    Docker Desktop for Mac/Windows runs a Linux VM. Passing the host uid there
    would steal /home/fetch from the image's ``fetch`` user.
    """

    if not is_native_linux_host(platform):
        return None, None, False
    uid, gid = host_user_ids()
    return uid, gid, selinux_enforcing()


def container_needs_recreate(engine: str, name: str = CONTAINER_NAME) -> bool:
    """True when a running container still uses the legacy host-X11 integration."""

    if not container_exists(engine, name):
        return False
    network = _command_text(
        [engine, "inspect", "-f", "{{.HostConfig.NetworkMode}}", name]
    ).strip()
    if network == "host":
        return True
    mounts = _command_text(
        [
            engine,
            "inspect",
            "-f",
            "{{range .Mounts}}{{.Source}}:{{.Destination}} {{end}}",
            name,
        ]
    )
    if "/tmp/.X11-unix" in mounts or "Xauthority" in mounts:
        return True
    image_id = _command_text([engine, "inspect", "-f", "{{.Image}}", name]).strip()
    current_image_id = _command_text(
        [engine, "inspect", "-f", "{{.Id}}", IMAGE_NAME]
    ).strip()
    if image_id and current_image_id and image_id != current_image_id:
        return True
    return False


def stop_container(*, name: str = CONTAINER_NAME) -> str:
    binaries = engine_binaries()
    if not binaries:
        return "no-engine"
    saw_container = False
    for engine in binaries:
        names = [name]
        if name == CONTAINER_NAME:
            names.extend(extra_computer_containers(engine))
        for container_name in dict.fromkeys(names):
            if not container_exists(engine, container_name):
                continue
            saw_container = True
            result = subprocess.run(
                [engine, "rm", "-f", container_name],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "").strip()
                raise ComputerError(
                    f"Could not stop the Fetch computer container: {detail or engine}"
                )
    return "stopped" if saw_container else "absent"


def build_image(engine: str) -> None:
    result = subprocess.run(
        [engine, "build", "-t", IMAGE_NAME, str(image_dir())],
        check=False,
    )
    if result.returncode != 0:
        raise ComputerError("Could not build the Fetch computer image.")


def wait_for_container_desktop(engine: str, *, wait_seconds: float) -> None:
    deadline = time.monotonic() + max(0.0, wait_seconds)
    last = "container did not become ready"
    while True:
        if not container_running(engine):
            last = "container is not running"
        elif not rfb_port_open():
            last = f"VNC is not answering on {RFB_HOST}:{RFB_PORT}"
        elif not desktop_client_can_open(engine):
            last = "desktop client could not open the VNC X server"
        else:
            return
        if time.monotonic() >= deadline:
            break
        time.sleep(0.25)
    raise ComputerError(
        f"The Fetch computer desktop did not become ready: {last}. "
        f"Stop it, then run:\n{stop_and_retry_commands()}"
    )


def run_container(engine: str) -> None:
    runtime_dir().mkdir(parents=True, exist_ok=True)
    container_home_dir().mkdir(parents=True, exist_ok=True)
    extras = extra_computer_containers(engine)
    if extras:
        raise ComputerError(extra_container_instructions(extras))
    if (
        container_running(engine)
        and rfb_port_open()
        and desktop_client_can_open(engine)
        and not container_needs_recreate(engine)
    ):
        return
    if container_exists(engine):
        stop_container()
    if rfb_port_open():
        raise ComputerError(display_or_port_busy_instructions())
    uid, gid, selinux = linux_volume_ownership()
    args = container_run_args(
        engine=engine,
        home_dir=str(container_home_dir()),
        restart="unless-stopped",
        selinux=selinux,
        uid=uid,
        gid=gid,
    )
    result = subprocess.run(args, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        if "already in use" in detail.lower() or "address already in use" in detail.lower():
            raise ComputerError(display_or_port_busy_instructions())
        raise ComputerError(f"Could not start the Fetch computer container: {detail}")


def _computer_setup_command(*, check_only: bool, wait_seconds: float) -> list[str]:
    command = [
        sys.executable,
        str(plugin_dir() / "computer_setup.py"),
        "--target",
        TARGET,
        "--kind",
        KIND,
        "--name",
        NAME,
        "--headed-browser",
        "--browser",
        str(gui_chrome_wrapper()),
        "--wait-seconds",
        str(wait_seconds),
    ]
    if check_only:
        command.append("--check-only")
    return command


def _run_computer_setup(*, check_only: bool, wait_seconds: float) -> None:
    result = subprocess.run(
        _computer_setup_command(check_only=check_only, wait_seconds=wait_seconds),
        check=False,
    )
    if result.returncode != 0:
        raise ComputerError("Fetch computer setup did not finish successfully.")


def install(*, wait_seconds: float = 90.0) -> None:
    engine = detect_engine()
    build_image(engine)
    run_container(engine)
    wait_for_container_desktop(engine, wait_seconds=wait_seconds)
    _run_computer_setup(check_only=False, wait_seconds=wait_seconds)


def uninstall() -> None:
    stop_container()
    result = subprocess.run(
        [sys.executable, str(plugin_dir() / "computer_setup.py"), "--disable"],
        check=False,
    )
    if result.returncode != 0:
        raise ComputerError("Could not disable Fetch computer access.")
    print("Fetch computer container was removed and computer access is disabled.")


def status(*, wait_seconds: float = 5.0) -> None:
    report = computer_readiness()
    if report["state"] != "ready":
        raise ComputerError(report["message"])
    if report.get("message") == READY_MESSAGE:
        _run_computer_setup(check_only=True, wait_seconds=wait_seconds)
    print(report["message"])


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        nargs="?",
        default="install",
        choices=("bootstrap", "install", "uninstall", "status", "doctor", "stop", "guide"),
        help="bootstrap and install are the same: build/run the container, then wire Fetch.",
    )
    parser.add_argument("--wait-seconds", type=float, default=90.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.action in {"bootstrap", "install"}:
            install(wait_seconds=args.wait_seconds)
            print(READY_MESSAGE)
        elif args.action == "uninstall":
            uninstall()
        elif args.action in {"status", "doctor"}:
            status(wait_seconds=min(args.wait_seconds, 15.0) if args.action == "doctor" else 5.0)
        elif args.action == "stop":
            result = stop_container()
            print(f"Fetch computer container: {result}")
        elif args.action == "guide":
            state = guide_computer(offer_bootstrap=False)
            return 0 if state == "ready" else 1
    except ComputerError as exc:
        print(f"Fetch computer setup failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
