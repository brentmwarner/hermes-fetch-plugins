import importlib.util
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
_path = PLUGIN_DIR / "linux-computer" / "manage.py"
_spec = importlib.util.spec_from_file_location("fetch_plugin_linux_computer_test", _path)
manage = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = manage
_spec.loader.exec_module(manage)


def test_detect_engine_prefers_ready_docker(monkeypatch) -> None:
    monkeypatch.setattr(manage, "engine_binaries", lambda: ["docker", "podman"])
    monkeypatch.setattr(manage, "engine_daemon_ready", lambda engine: engine == "docker")

    assert manage.detect_engine() == "docker"


def test_detect_engine_uses_podman_when_docker_daemon_is_down(monkeypatch) -> None:
    monkeypatch.setattr(manage, "engine_binaries", lambda: ["docker", "podman"])
    monkeypatch.setattr(manage, "engine_daemon_ready", lambda engine: engine == "podman")

    assert manage.detect_engine() == "podman"


def test_detect_engine_fails_when_docker_and_podman_are_missing(monkeypatch) -> None:
    monkeypatch.setattr(manage, "engine_binaries", lambda: [])

    with pytest.raises(manage.ComputerError, match="requires Docker or Podman"):
        manage.detect_engine()


def test_detect_engine_fails_when_installed_engine_is_not_running(monkeypatch) -> None:
    monkeypatch.setattr(manage, "engine_binaries", lambda: ["docker"])
    monkeypatch.setattr(manage, "engine_daemon_ready", lambda _engine: False)

    with pytest.raises(manage.ComputerError, match="not running"):
        manage.detect_engine()


def test_linux_run_args_use_host_network_and_never_publish_vnc() -> None:
    args = manage.container_run_args(
        engine="docker",
        xauthority="/tmp/fetch.Xauthority",
        home_dir="/tmp/fetch-home",
        host_network=True,
        uid=1000,
        gid=1000,
    )

    assert args[:4] == ["docker", "run", "-d", "--name"]
    assert manage.CONTAINER_NAME in args
    assert "--network" in args and "host" in args
    assert "FETCH_VNC_LOCALHOST=1" in args
    assert "FETCH_GEOMETRY=1280x800" in args
    assert "-p" not in args
    assert "--publish" not in args
    assert "0.0.0.0" not in " ".join(args)
    assert "/tmp/.X11-unix:/tmp/.X11-unix" in args
    assert "--user" in args


def test_desktop_run_args_publish_only_loopback() -> None:
    args = manage.container_run_args(
        engine="docker",
        xauthority="/tmp/fetch.Xauthority",
        home_dir="/tmp/fetch-home",
        host_network=False,
        uid=501,
        gid=20,
    )

    assert "-p" in args
    assert f"{manage.RFB_HOST}:{manage.RFB_PORT}:{manage.RFB_PORT}" in args
    assert "FETCH_VNC_LOCALHOST=0" in args
    assert "--network" not in args
    assert "0.0.0.0" not in " ".join(args)
    assert "/tmp/.X11-unix:/tmp/.X11-unix" not in args


def test_reject_non_loopback_publish() -> None:
    with pytest.raises(manage.ComputerError, match="0.0.0.0"):
        manage.reject_non_loopback_publish(
            ["docker", "run", "-p", "0.0.0.0:5901:5901", "fetch-computer:local"]
        )
    with pytest.raises(manage.ComputerError, match="loopback-only"):
        manage.reject_non_loopback_publish(
            ["docker", "run", "-p", "5901:5901", "fetch-computer:local"]
        )


def test_stop_container_is_noop_without_an_engine(monkeypatch) -> None:
    monkeypatch.setattr(manage, "engine_binaries", lambda: [])

    assert manage.stop_container() == "no-engine"


def test_stop_container_removes_a_running_container(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(list(command))
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(manage, "engine_binaries", lambda: ["docker"])
    monkeypatch.setattr(manage, "container_exists", lambda engine, name=manage.CONTAINER_NAME: True)
    monkeypatch.setattr(manage.subprocess, "run", fake_run)

    assert manage.stop_container() == "stopped"
    assert calls == [["docker", "rm", "-f", manage.CONTAINER_NAME]]


def test_stop_container_reports_absent_when_no_container(monkeypatch) -> None:
    monkeypatch.setattr(manage, "engine_binaries", lambda: ["podman"])
    monkeypatch.setattr(manage, "container_exists", lambda engine, name=manage.CONTAINER_NAME: False)

    assert manage.stop_container() == "absent"


def test_uninstall_stops_container_then_disables_bridge(monkeypatch) -> None:
    calls: list[object] = []

    monkeypatch.setattr(
        manage, "stop_container", lambda: calls.append("stop-container") or "stopped"
    )

    def fake_run(command, **_kwargs):
        calls.append(list(command))
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(manage.subprocess, "run", fake_run)

    manage.uninstall()

    assert calls[0] == "stop-container"
    assert calls[1][-1] == "--disable"
    assert "computer_setup.py" in calls[1][-2]


def test_computer_setup_command_uses_fetch_loopback_contract() -> None:
    command = manage._computer_setup_command(check_only=False, wait_seconds=90)

    assert "--target" in command
    assert command[command.index("--target") + 1] == "tcp://127.0.0.1:5901"
    assert command[command.index("--kind") + 1] == "Virtual Linux desktop"
    assert command[command.index("--name") + 1] == "Fetch computer"
    assert "--headed-browser" in command


def test_manager_does_not_install_host_xfce() -> None:
    text = _path.read_text(encoding="utf-8")
    assert "xfce" not in text.lower()
    assert "requires Docker or Podman" in text
    assert "apt-get install -y docker.io" in text
    assert "dnf install -y podman" in text
    assert "subprocess.run([\"apt" not in text
    assert "subprocess.run([\"dnf" not in text


def test_image_keeps_branded_wallpaper_and_loopback_vnc() -> None:
    computer_dir = PLUGIN_DIR / "linux-computer"
    dockerfile = (computer_dir / "Dockerfile").read_text(encoding="utf-8")
    entrypoint = (computer_dir / "entrypoint.sh").read_text(encoding="utf-8")
    compose = (computer_dir / "docker-compose.yml").read_text(encoding="utf-8")
    wallpaper = computer_dir / "branding" / "wallpaper.png"

    assert wallpaper.is_file()
    assert wallpaper.stat().st_size > 10_000
    assert "branding/wallpaper.png" in dockerfile
    assert "FROM ubuntu:24.04" in dockerfile
    assert "FETCH_GEOMETRY=1280x800" in dockerfile
    assert "tigervnc-standalone-server" in dockerfile
    assert "xfce4" in dockerfile
    assert "EXPOSE" not in dockerfile
    assert "1280x800" in entrypoint
    assert "1920x1080" not in entrypoint
    assert "-localhost" in entrypoint
    assert "hsetroot -cover" in entrypoint
    assert "startxfce4" in entrypoint
    assert "network_mode: host" in compose
    compose_code = "\n".join(
        line for line in compose.splitlines() if not line.lstrip().startswith("#")
    )
    assert "ports:" not in compose_code
    assert "0.0.0.0" not in compose_code


def test_readme_makes_the_container_the_default_linux_computer() -> None:
    readme = (PLUGIN_DIR / "README.md").read_text(encoding="utf-8")

    assert "linux-computer" in readme
    assert "Docker or Podman" in readme
    assert "Fedora Wayland" in readme
    assert "scrape the physical login session" in readme
    assert "Xorg" in readme
    assert "linux-desktop" in readme
    assert "Xcode and Simulator cannot run in Ubuntu" in readme
    assert "optional extra" in readme
    assert "branding/wallpaper.png" in readme
    assert "1280×800" in readme or "1280x800" in readme
    assert "hermes plugins update" in readme
    assert "bootstrap" in readme
    assert "Fetch Watch just works" in readme


def test_bootstrap_command_points_at_the_installed_manager() -> None:
    command = manage.bootstrap_command()

    assert command.endswith("linux-computer/manage-computer.sh bootstrap")
    assert str(PLUGIN_DIR / "linux-computer" / "manage-computer.sh") in command


def test_computer_readiness_is_not_linux_on_macos() -> None:
    report = manage.computer_readiness(platform="darwin")

    assert report["state"] == "not-linux"
    assert report["message"] == ""


def test_computer_readiness_reports_engine_missing(monkeypatch) -> None:
    monkeypatch.setattr(manage, "uses_host_network", lambda platform=None: True)
    monkeypatch.setattr(manage, "engine_binaries", lambda: [])

    report = manage.computer_readiness()

    assert report["state"] == "engine-missing"
    assert "sudo apt-get install -y docker.io" in report["message"]
    assert "sudo dnf install -y podman" in report["message"]
    assert "manage-computer.sh bootstrap" in report["message"]


def test_computer_readiness_reports_engine_not_running(monkeypatch) -> None:
    monkeypatch.setattr(manage, "uses_host_network", lambda platform=None: True)
    monkeypatch.setattr(manage, "engine_binaries", lambda: ["docker"])
    monkeypatch.setattr(manage, "engine_daemon_ready", lambda _engine: False)

    report = manage.computer_readiness()

    assert report["state"] == "engine-not-running"
    assert "sudo systemctl start docker" in report["message"]
    assert "manage-computer.sh bootstrap" in report["message"]


def test_computer_readiness_reports_container_absent(monkeypatch) -> None:
    monkeypatch.setattr(manage, "uses_host_network", lambda platform=None: True)
    monkeypatch.setattr(manage, "engine_binaries", lambda: ["podman"])
    monkeypatch.setattr(manage, "engine_daemon_ready", lambda engine: engine == "podman")
    monkeypatch.setattr(manage, "container_running", lambda engine, name=manage.CONTAINER_NAME: False)

    report = manage.computer_readiness()

    assert report["state"] == "container-absent"
    assert report["engine"] == "podman"
    assert "unless-stopped" in report["message"]
    assert "manage-computer.sh bootstrap" in report["message"]


def test_computer_readiness_reports_ready(monkeypatch) -> None:
    monkeypatch.setattr(manage, "uses_host_network", lambda platform=None: True)
    monkeypatch.setattr(manage, "engine_binaries", lambda: ["docker"])
    monkeypatch.setattr(manage, "engine_daemon_ready", lambda engine: engine == "docker")
    monkeypatch.setattr(manage, "container_running", lambda engine, name=manage.CONTAINER_NAME: True)

    report = manage.computer_readiness()

    assert report["state"] == "ready"
    assert "running" in report["message"]


def test_guide_prints_copy_pasteable_bootstrap_and_does_not_auto_start(monkeypatch) -> None:
    printed: list[str] = []
    monkeypatch.setattr(
        manage,
        "computer_readiness",
        lambda: {
            "state": "container-absent",
            "engine": "docker",
            "message": manage.container_missing_instructions(),
        },
    )
    monkeypatch.setattr(manage.sys.stdin, "isatty", lambda: False)

    def fail_install() -> None:
        raise AssertionError("guide must not bootstrap without a confirmed TTY")

    monkeypatch.setattr(manage, "install", fail_install)

    state = manage.guide_linux_computer(offer_bootstrap=True, printer=printed.append)

    assert state == "container-absent"
    assert any("manage-computer.sh bootstrap" in line for line in printed)


def test_guide_is_silent_when_not_linux(monkeypatch) -> None:
    printed: list[str] = []
    monkeypatch.setattr(
        manage, "computer_readiness", lambda: {"state": "not-linux", "message": ""}
    )

    assert manage.guide_linux_computer(printer=printed.append) == "not-linux"
    assert printed == []


def test_compose_sets_grok_bot_geometry() -> None:
    compose = (PLUGIN_DIR / "linux-computer" / "docker-compose.yml").read_text(
        encoding="utf-8"
    )

    assert "FETCH_GEOMETRY: \"1280x800\"" in compose
    assert "1920x1080" not in compose
