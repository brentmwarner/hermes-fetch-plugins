#!/usr/bin/env python3
"""Start and stop the portable Fetch computer container."""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
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


class ComputerError(RuntimeError):
    pass


def image_dir() -> Path:
    return Path(__file__).resolve().parent


def plugin_dir() -> Path:
    return image_dir().parent


def runtime_dir() -> Path:
    return Path.home() / ".local" / "share" / "fetch-computer"


def xauthority_path() -> Path:
    return runtime_dir() / "Xauthority"


def container_home_dir() -> Path:
    return runtime_dir() / "home"


def uses_host_network(platform: str | None = None) -> bool:
    return (platform or sys.platform).startswith("linux")


def selinux_enforcing() -> bool:
    path = Path("/sys/fs/selinux/enforce")
    try:
        return path.read_text(encoding="utf-8").strip() == "1"
    except OSError:
        return False


def engine_binaries() -> list[str]:
    return [ENGINE] if shutil.which(ENGINE) else []


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


def bootstrap_command() -> str:
    return f"{image_dir() / 'manage-computer.sh'} bootstrap"


def engine_missing_instructions() -> str:
    return (
        "Fetch computer setup requires Docker. "
        "Install Docker, start it, then bootstrap once.\n"
        "\n"
        "  Ubuntu/Debian:\n"
        "    sudo apt-get update && sudo apt-get install -y docker.io\n"
        "    sudo systemctl enable --now docker\n"
        "  Fedora:\n"
        "    sudo dnf install -y docker\n"
        "    sudo systemctl enable --now docker\n"
        "\n"
        f"  Then run:\n    {bootstrap_command()}"
    )


def engine_not_running_instructions() -> str:
    return (
        "Docker is installed, but the daemon is not running.\n"
        "\n"
        "  sudo systemctl start docker\n"
        "\n"
        f"  Then run:\n    {bootstrap_command()}"
    )


def container_missing_instructions() -> str:
    return (
        "The Fetch computer container is not running. Start it once; after that "
        "the engine restart policy (`unless-stopped`) brings it back automatically.\n"
        "\n"
        f"  {bootstrap_command()}"
    )


def detect_engine() -> str:
    if not engine_binaries():
        raise ComputerError(engine_missing_instructions())
    if not engine_daemon_ready(ENGINE):
        raise ComputerError(engine_not_running_instructions())
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
    xauthority: str,
    home_dir: str,
    host_network: bool,
    restart: str | None = "unless-stopped",
    selinux: bool = False,
    uid: int | None = None,
    gid: int | None = None,
    image: str = IMAGE_NAME,
    name: str = CONTAINER_NAME,
) -> list[str]:
    args = [engine, "run", "-d", "--name", name, "--init", "--shm-size", "2g"]
    if restart:
        args.extend(["--restart", restart])
    if selinux:
        args.extend(["--security-opt", "label=disable"])
    if uid is not None and gid is not None:
        args.extend(["--user", f"{uid}:{gid}"])
    if host_network:
        args.extend(["--network", "host", "-e", "FETCH_VNC_LOCALHOST=1"])
    else:
        args.extend(
            [
                "-p",
                f"{RFB_HOST}:{RFB_PORT}:{RFB_PORT}",
                "-e",
                "FETCH_VNC_LOCALHOST=0",
            ]
        )
    args.extend(
        [
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
            "-v",
            f"{xauthority}:/home/fetch/.Xauthority",
        ]
    )
    if host_network:
        args.extend(["-v", "/tmp/.X11-unix:/tmp/.X11-unix"])
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


def computer_readiness(*, platform: str | None = None) -> dict[str, str]:
    if not uses_host_network(platform):
        return {"state": "not-linux", "message": ""}
    if not engine_binaries():
        return {"state": "engine-missing", "message": engine_missing_instructions()}
    if not engine_daemon_ready(ENGINE):
        return {"state": "engine-not-running", "message": engine_not_running_instructions()}
    if container_running(ENGINE):
        return {
            "state": "ready",
            "engine": ENGINE,
            "message": "Fetch computer is running. Fetch Watch can use this desktop.",
        }
    return {
        "state": "container-absent",
        "engine": ENGINE,
        "message": container_missing_instructions(),
    }


def guide_linux_computer(*, offer_bootstrap: bool = True, printer=print) -> str:
    """Print copy-pasteable next steps and optionally start the container."""

    report = computer_readiness()
    if report["state"] == "not-linux":
        return "not-linux"
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
    printer("Fetch computer is ready. Fetch Watch can use this desktop.")
    return "started"


def rfb_port_open(host: str = RFB_HOST, port: int = RFB_PORT) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.4):
            return True
    except OSError:
        return False


def stop_container(*, name: str = CONTAINER_NAME) -> str:
    binaries = engine_binaries()
    if not binaries:
        return "no-engine"
    saw_container = False
    for engine in binaries:
        if not container_exists(engine, name):
            continue
        saw_container = True
        result = subprocess.run(
            [engine, "rm", "-f", name],
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


def ensure_xauthority(path: Path, display: str = DISPLAY_NAME) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.touch()
    try:
        path.chmod(0o600)
    except OSError:
        pass
    xauth = shutil.which("xauth")
    if xauth is None:
        return
    subprocess.run(
        [xauth, "-f", str(path), "remove", display],
        check=False,
        capture_output=True,
    )
    cookie = os.urandom(16).hex()
    added = subprocess.run(
        [xauth, "-f", str(path), "add", display, "MIT-MAGIC-COOKIE-1", cookie],
        check=False,
        capture_output=True,
        text=True,
    )
    if added.returncode != 0:
        raise ComputerError("Could not write the Fetch computer Xauthority cookie.")


def build_image(engine: str) -> None:
    result = subprocess.run(
        [engine, "build", "-t", IMAGE_NAME, str(image_dir())],
        check=False,
    )
    if result.returncode != 0:
        raise ComputerError("Could not build the Fetch computer image.")


def run_container(engine: str) -> None:
    runtime_dir().mkdir(parents=True, exist_ok=True)
    container_home_dir().mkdir(parents=True, exist_ok=True)
    ensure_xauthority(xauthority_path())
    if container_exists(engine):
        stop_container()
    if rfb_port_open():
        raise ComputerError(
            f"Fetch computer port {RFB_PORT} is already in use on {RFB_HOST}. "
            "Stop the other VNC or linux-vps desktop, then rerun this setup."
        )
    args = container_run_args(
        engine=engine,
        xauthority=str(xauthority_path()),
        home_dir=str(container_home_dir()),
        host_network=uses_host_network(),
        restart="unless-stopped",
        selinux=selinux_enforcing(),
        uid=os.getuid(),
        gid=os.getgid(),
    )
    result = subprocess.run(args, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
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
        "--wait-seconds",
        str(wait_seconds),
    ]
    if uses_host_network():
        command.extend(
            ["--display", DISPLAY_NAME, "--xauthority", str(xauthority_path())]
        )
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
    engine = detect_engine()
    if not container_exists(engine):
        raise ComputerError("The Fetch computer container is not running.")
    _run_computer_setup(check_only=True, wait_seconds=wait_seconds)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        nargs="?",
        default="install",
        choices=("bootstrap", "install", "uninstall", "status", "stop", "guide"),
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
        elif args.action == "uninstall":
            uninstall()
        elif args.action == "status":
            status()
        elif args.action == "stop":
            result = stop_container()
            print(f"Fetch computer container: {result}")
        elif args.action == "guide":
            state = guide_linux_computer(offer_bootstrap=False)
            return 0 if state in {"ready", "not-linux"} else 1
    except ComputerError as exc:
        print(f"Fetch computer setup failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
