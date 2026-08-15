import importlib.util
import socket
import sys
import threading
from pathlib import Path

import pytest

_path = Path(__file__).resolve().parent.parent / "computer_setup.py"
_spec = importlib.util.spec_from_file_location("fetch_plugin_computer_setup_test", _path)
setup = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = setup
_spec.loader.exec_module(setup)


def test_persist_environment_preserves_unrelated_lines_and_replaces_values(tmp_path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "# user setting\nOTHER=value\nHERMES_FETCH_COMPUTER_KIND=Old\n",
        encoding="utf-8",
    )

    setup.persist_environment(
        env_path,
        {
            "HERMES_FETCH_COMPUTER_KIND": "Virtual Linux desktop",
            "HERMES_FETCH_COMPUTER_TARGET": "tcp://127.0.0.1:5901",
        },
    )

    text = env_path.read_text(encoding="utf-8")
    assert "# user setting" in text
    assert "OTHER=value" in text
    assert 'HERMES_FETCH_COMPUTER_KIND="Virtual Linux desktop"' in text
    assert 'HERMES_FETCH_COMPUTER_TARGET="tcp://127.0.0.1:5901"' in text
    assert env_path.stat().st_mode & 0o777 == 0o600


def test_persist_environment_treats_chmod_as_best_effort(tmp_path, monkeypatch) -> None:
    env_path = tmp_path / ".env"

    def _unsupported(_path, _mode):
        raise OSError("chmod is not supported")

    monkeypatch.setattr(setup.os, "chmod", _unsupported)
    setup.persist_environment(env_path, {"HERMES_FETCH_COMPUTER_TARGET": "tcp://127.0.0.1:5901"})

    assert 'HERMES_FETCH_COMPUTER_TARGET="tcp://127.0.0.1:5901"' in env_path.read_text(
        encoding="utf-8"
    )


def test_configured_vnc_password_reads_owner_only_hermes_environment(tmp_path, monkeypatch) -> None:
    env_path = tmp_path / ".env"
    setup.persist_environment(env_path, {setup.VNC_PASSWORD_ENV: 'local "secret"'})
    monkeypatch.setattr(setup, "hermes_home", lambda: tmp_path)
    monkeypatch.delenv(setup.VNC_PASSWORD_ENV, raising=False)

    assert setup.configured_vnc_password() == 'local "secret"'
    assert env_path.stat().st_mode & 0o777 == 0o600


def test_remove_environment_keys_drops_computer_settings_and_keeps_other_lines(tmp_path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "# keep\nOTHER=value\nHERMES_FETCH_COMPUTER_TARGET=tcp://127.0.0.1:5901\n"
        "export HERMES_FETCH_COMPUTER_KIND=Old\nHERMES_FETCH_TUNNEL_ENABLED=1\n"
        "DISPLAY=:1\nXAUTHORITY=/tmp/fetch.Xauthority\nAGENT_BROWSER_HEADED=1\n"
        "WAYLAND_DISPLAY=\nDBUS_SESSION_BUS_ADDRESS=\n",
        encoding="utf-8",
    )

    setup.remove_environment_keys(env_path, setup.PERSISTED_COMPUTER_ENV_KEYS)

    text = env_path.read_text(encoding="utf-8")
    assert "# keep" in text
    assert "OTHER=value" in text
    assert "HERMES_FETCH_TUNNEL_ENABLED=1" in text
    assert "HERMES_FETCH_COMPUTER_TARGET" not in text
    assert "HERMES_FETCH_COMPUTER_KIND" not in text
    assert "DISPLAY=" not in text
    assert "XAUTHORITY=" not in text
    assert "AGENT_BROWSER_HEADED" not in text
    for key in setup.VIRTUAL_DESKTOP_ENV_KEYS:
        assert key not in text


def test_disable_computer_stops_bridge_and_clears_persisted_target(tmp_path, monkeypatch) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        'HERMES_FETCH_COMPUTER_TARGET="tcp://127.0.0.1:5901"\n'
        'HERMES_FETCH_COMPUTER_VNC_PASSWORD="dedicated-password"\n'
        'HERMES_FETCH_TUNNEL_ENABLED="1"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_FETCH_COMPUTER_TARGET", "tcp://127.0.0.1:5901")
    monkeypatch.setenv("HERMES_FETCH_COMPUTER_KIND", "Virtual Linux desktop")
    monkeypatch.setenv(setup.VNC_PASSWORD_ENV, "dedicated-password")
    monkeypatch.setenv(setup.WAYLAND_DISPLAY_ENV, "wayland-0")
    monkeypatch.setenv(setup.DBUS_SESSION_BUS_ADDRESS_ENV, "unix:path=/run/user/1000/bus")
    monkeypatch.setattr(setup, "hermes_home", lambda: tmp_path)
    calls = []

    class FakeRuntime:
        def restart_computer_runtime(self):
            calls.append("stop")
            return True

    class FakeRelayRuntime:
        def restart_relay_runtime_for_reconfigure(self):
            calls.append("restart-relay")
            return {"stopped": [42], "left_running": []}

        def ensure_relay_runtime(self, **kwargs):
            calls.append("start-relay")
            return "started"

    monkeypatch.setattr(setup, "_computer_runtime_module", lambda: FakeRuntime())
    monkeypatch.setattr(setup, "_relay_runtime_module", lambda: FakeRelayRuntime())

    setup.disable_computer()

    assert calls == ["stop", "restart-relay", "start-relay"]
    saved = env_path.read_text(encoding="utf-8")
    assert "HERMES_FETCH_COMPUTER_TARGET" not in saved
    assert setup.VNC_PASSWORD_ENV not in saved
    assert 'HERMES_FETCH_TUNNEL_ENABLED="1"' in saved
    assert "HERMES_FETCH_COMPUTER_TARGET" not in setup.os.environ
    assert "HERMES_FETCH_COMPUTER_KIND" not in setup.os.environ
    assert setup.VNC_PASSWORD_ENV not in setup.os.environ
    assert setup.os.environ[setup.WAYLAND_DISPLAY_ENV] == "wayland-0"
    assert setup.os.environ[setup.DBUS_SESSION_BUS_ADDRESS_ENV] == "unix:path=/run/user/1000/bus"


def test_disable_computer_stops_container_before_clearing_env(tmp_path, monkeypatch) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text('HERMES_FETCH_COMPUTER_TARGET="tcp://127.0.0.1:5901"\n', encoding="utf-8")
    monkeypatch.setattr(setup, "hermes_home", lambda: tmp_path)
    calls = []

    class FakeLinuxComputer:
        class ComputerError(RuntimeError):
            pass

        def stop_container(self):
            calls.append("stop-container")
            return "stopped"

    class FakeRuntime:
        def restart_computer_runtime(self):
            calls.append("stop-bridge")
            return True

    class FakeRelayRuntime:
        def restart_relay_runtime_for_reconfigure(self):
            return {"stopped": [1], "left_running": []}

        def ensure_relay_runtime(self, **kwargs):
            return "started"

    monkeypatch.setattr(setup, "_linux_computer_module", lambda: FakeLinuxComputer())
    monkeypatch.setattr(setup, "_computer_runtime_module", lambda: FakeRuntime())
    monkeypatch.setattr(setup, "_relay_runtime_module", lambda: FakeRelayRuntime())

    setup.disable_computer()

    assert calls == ["stop-container", "stop-bridge"]
    assert "HERMES_FETCH_COMPUTER_TARGET" not in env_path.read_text(encoding="utf-8")


def test_disable_computer_fails_when_container_cannot_be_stopped(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(setup, "hermes_home", lambda: tmp_path)

    class FakeLinuxComputer:
        class ComputerError(RuntimeError):
            pass

        def stop_container(self):
            raise FakeLinuxComputer.ComputerError("container still running")

    monkeypatch.setattr(setup, "_linux_computer_module", lambda: FakeLinuxComputer())

    with pytest.raises(setup.SetupError, match="container still running"):
        setup.disable_computer()


def test_disable_computer_fails_when_bridge_cannot_be_stopped(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(setup, "hermes_home", lambda: tmp_path)

    class FakeRuntime:
        def restart_computer_runtime(self):
            return False

    monkeypatch.setattr(setup, "_computer_runtime_module", lambda: FakeRuntime())

    with pytest.raises(setup.SetupError, match="Could not stop"):
        setup.disable_computer()


def test_native_mac_and_windows_helpers_keep_loopback_screen_sharing() -> None:
    plugin_dir = Path(__file__).resolve().parent.parent
    macos = (plugin_dir / "macos" / "check-computer.sh").read_text(encoding="utf-8")
    windows = (plugin_dir / "windows" / "check-computer.ps1").read_text(encoding="utf-8")

    assert "tcp://127.0.0.1:5900" in macos
    assert '--kind "Mac desktop"' in macos
    assert "tcp://127.0.0.1:5900" in windows
    assert "Windows desktop" in windows


def test_linux_service_scripts_use_xdg_config_home_and_disable_on_uninstall() -> None:
    plugin_dir = Path(__file__).resolve().parent.parent
    scripts = (
        plugin_dir / "linux-vps" / "manage-user-services.sh",
        plugin_dir / "linux-desktop" / "manage-user-service.sh",
    )
    for script in scripts:
        text = script.read_text(encoding="utf-8")
        assert "systemd-path" not in text
        assert "XDG_CONFIG_HOME" in text
        assert "computer_setup.py" in text and "--disable" in text


def test_virtual_linux_setup_supports_fedora_and_a_fixed_headed_desktop() -> None:
    plugin_dir = Path(__file__).resolve().parent.parent
    manager = (plugin_dir / "linux-vps" / "manage-user-services.sh").read_text(
        encoding="utf-8"
    )
    service = (plugin_dir / "linux-vps" / "fetch-computer-vnc.service").read_text(
        encoding="utf-8"
    )
    launcher = (plugin_dir / "linux-vps" / "run-virtual-desktop.sh").read_text(
        encoding="utf-8"
    )

    assert "bootstrap" in manager
    assert "require_command" not in manager
    assert "apt-get install" in manager
    assert "dnf install" in manager
    assert "tigervnc-server-minimal" in manager
    assert "xorg-x11-xauth" in manager
    assert "--headed-browser" in manager
    assert "run-virtual-desktop.sh" in service
    assert "Restart=always" in service
    assert "1920x1080" in launcher
    assert "-localhost" in launcher
    assert "-SecurityTypes None" in launcher
    assert "MIT-MAGIC-COOKIE-1" in launcher
    assert "unset DBUS_SESSION_BUS_ADDRESS DESKTOP_SESSION GDK_BACKEND QT_QPA_PLATFORM" in launcher
    assert "dbus-run-session -- startxfce4" in launcher
    assert "UnsetEnvironment=DBUS_SESSION_BUS_ADDRESS SESSION_MANAGER WAYLAND_DISPLAY XDG_SESSION_TYPE" in service


def test_probe_desktop_requires_an_actual_rfb_server() -> None:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def serve() -> None:
        connection, _address = server.accept()
        with connection:
            connection.sendall(b"RFB 003.008\n")
            assert connection.recv(12) == b"RFB 003.008\n"
            connection.sendall(b"\x01\x01")
            assert connection.recv(1) == b"\x01"
            connection.sendall(b"\0\0\0\0")
        server.close()

    thread = threading.Thread(target=serve)
    thread.start()
    setup.probe_desktop(f"tcp://127.0.0.1:{port}", password="", wait_seconds=1)
    thread.join(timeout=2)

    assert not thread.is_alive()


def test_probe_desktop_authenticates_on_the_host_before_reporting_ready() -> None:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    challenge = bytes(range(16))

    def serve() -> None:
        connection, _address = server.accept()
        with connection:
            connection.sendall(b"RFB 003.008\n")
            assert connection.recv(12) == b"RFB 003.008\n"
            connection.sendall(b"\x02\x01\x02")
            assert connection.recv(1) == b"\x02"
            connection.sendall(challenge)
            assert connection.recv(16) == bytes.fromhex(
                "ee22539f33a5983ec12f9c2edbc995dd"
            )
            connection.sendall(b"\0\0\0\0")
        server.close()

    thread = threading.Thread(target=serve)
    thread.start()
    setup.probe_desktop(
        f"tcp://127.0.0.1:{port}",
        password="secret",
        wait_seconds=1,
    )
    thread.join(timeout=2)

    assert not thread.is_alive()


def test_probe_desktop_retries_a_short_rfb_read_during_server_startup() -> None:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(2)
    port = server.getsockname()[1]

    def serve() -> None:
        first, _address = server.accept()
        with first:
            first.sendall(b"RFB")
        second, _address = server.accept()
        with second:
            second.sendall(b"RFB 003.008\n")
            assert second.recv(12) == b"RFB 003.008\n"
            second.sendall(b"\x01\x01")
            assert second.recv(1) == b"\x01"
            second.sendall(b"\0\0\0\0")
        server.close()

    thread = threading.Thread(target=serve)
    thread.start()
    setup.probe_desktop(
        f"tcp://127.0.0.1:{port}",
        password="",
        wait_seconds=2,
    )
    thread.join(timeout=2)

    assert not thread.is_alive()


def test_wait_for_relay_waits_until_computer_uplink_is_online(monkeypatch) -> None:
    responses = [
        (503, {"reason": "agent_offline"}),
        (200, {"ok": True, "status": {"computer": {"name": "Hermes VPS"}}}),
    ]
    monkeypatch.setattr(setup, "computer_status", lambda credentials: responses.pop(0))
    monkeypatch.setattr(setup.time, "sleep", lambda _seconds: None)

    result = setup.wait_for_relay({"relay_url": "x"}, wait_seconds=5)

    assert result["ok"] is True


def test_wait_for_relay_rejects_incompatible_relay(monkeypatch) -> None:
    monkeypatch.setattr(setup, "computer_status", lambda credentials: (404, {}))

    with pytest.raises(setup.SetupError, match="does not support computer readiness"):
        setup.wait_for_relay({"relay_url": "x"}, wait_seconds=0)


def test_configure_starts_dedicated_bridge_before_reporting_ready(tmp_path, monkeypatch) -> None:
    calls = []
    runtime_environments = []

    class FakeRuntime:
        def restart_computer_runtime(self):
            calls.append("restart")
            return True

        def ensure_computer_runtime(self, **kwargs):
            calls.append("start")
            runtime_environments.append(kwargs["environment"])
            return "started"

    class FakeRelayRuntime:
        def restart_relay_runtime_for_reconfigure(self):
            calls.append("restart-relay")
            return {"stopped": [42], "left_running": []}

        def ensure_relay_runtime(self, **kwargs):
            calls.append("start-relay")
            runtime_environments.append(kwargs["environment"])
            return "started"

    monkeypatch.setattr(
        setup,
        "probe_desktop",
        lambda target, password, wait_seconds: calls.append(("probe", password)),
    )
    monkeypatch.setattr(
        setup,
        "_credentials",
        lambda: {"relay_url": "https://relay", "agent_id": "agent", "agent_secret": "secret"},
    )
    monkeypatch.setattr(setup, "hermes_home", lambda: tmp_path)
    monkeypatch.setenv(setup.WAYLAND_DISPLAY_ENV, "wayland-0")
    monkeypatch.setattr(setup, "_computer_runtime_module", lambda: FakeRuntime())
    monkeypatch.setattr(setup, "_relay_runtime_module", lambda: FakeRelayRuntime())
    monkeypatch.setattr(
        setup,
        "wait_for_relay",
        lambda credentials, wait_seconds: calls.append("ready") or {"ok": True},
    )

    result = setup.configure(
        target="tcp://127.0.0.1:5901",
        kind="Virtual Linux desktop",
        name="Hermes VPS",
        display=":1",
        xauthority="/tmp/fetch.Xauthority",
        headed_browser=True,
        vnc_password="dedicated-password",
        wait_seconds=5,
        check_only=False,
    )

    assert result == {"ok": True}
    assert calls == [
        ("probe", "dedicated-password"),
        "restart",
        "start",
        "restart-relay",
        "start-relay",
        "ready",
    ]
    saved = (tmp_path / ".env").read_text(encoding="utf-8")
    assert 'DISPLAY=":1"' in saved
    assert 'XAUTHORITY="/tmp/fetch.Xauthority"' in saved
    assert 'AGENT_BROWSER_HEADED="1"' in saved
    assert 'WAYLAND_DISPLAY=""' in saved
    assert 'SESSION_MANAGER=""' in saved
    assert 'XDG_SESSION_TYPE="x11"' in saved
    assert 'XDG_CURRENT_DESKTOP=""' in saved
    assert 'XDG_SESSION_DESKTOP=""' in saved
    assert 'DESKTOP_SESSION=""' in saved
    assert 'DBUS_SESSION_BUS_ADDRESS=""' in saved
    assert 'GDK_BACKEND="x11"' in saved
    assert 'QT_QPA_PLATFORM="xcb"' in saved
    assert 'HERMES_FETCH_COMPUTER_NAME="Hermes VPS"' in saved
    assert 'HERMES_FETCH_COMPUTER_VNC_PASSWORD="dedicated-password"' in saved
    assert (tmp_path / ".env").stat().st_mode & 0o777 == 0o600
    assert setup.os.environ[setup.WAYLAND_DISPLAY_ENV] == "wayland-0"
    assert len(runtime_environments) == 2
    for environment in runtime_environments:
        assert environment[setup.WAYLAND_DISPLAY_ENV] == ""
        assert environment[setup.XDG_SESSION_TYPE_ENV] == "x11"


def test_non_virtual_setup_restores_physical_session_for_all_later_runtimes(
    tmp_path, monkeypatch
) -> None:
    startup_session = {
        setup.XDG_SESSION_TYPE_ENV: "x11",
        setup.GDK_BACKEND_ENV: "x11",
        setup.QT_QPA_PLATFORM_ENV: "xcb",
        setup.DBUS_SESSION_BUS_ADDRESS_ENV: "unix:path=/run/user/1000/bus",
    }
    setup.persist_environment(
        tmp_path / ".env",
        {setup.KIND_ENV: "Virtual Linux desktop", **setup.VIRTUAL_DESKTOP_ENV_VALUES},
    )
    for key, value in setup.VIRTUAL_DESKTOP_ENV_VALUES.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(setup, "_PROCESS_START_ENVIRONMENT", startup_session)
    monkeypatch.setattr(setup, "_VIRTUAL_DESKTOP_RECONCILIATION_REQUIRED", False)
    runtime_environments = []

    class FakeComputerRuntime:
        def restart_computer_runtime(self):
            return True

        def ensure_computer_runtime(self, **kwargs):
            runtime_environments.append(kwargs["environment"])
            return "started"

    class FakeRelayRuntime:
        def restart_relay_runtime_for_reconfigure(self):
            return {"stopped": [], "left_running": []}

        def ensure_relay_runtime(self, **kwargs):
            runtime_environments.append(kwargs["environment"])
            return "started"

    monkeypatch.setattr(setup, "probe_desktop", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        setup,
        "_credentials",
        lambda: {"relay_url": "https://relay", "agent_id": "agent", "agent_secret": "secret"},
    )
    monkeypatch.setattr(setup, "hermes_home", lambda: tmp_path)
    monkeypatch.setattr(setup, "_computer_runtime_module", lambda: FakeComputerRuntime())
    monkeypatch.setattr(setup, "_relay_runtime_module", lambda: FakeRelayRuntime())
    monkeypatch.setattr(setup, "wait_for_relay", lambda *args, **kwargs: {"ok": True})

    setup.configure(
        target="tcp://127.0.0.1:5900",
        kind="Linux desktop",
        name="",
        display=":0",
        xauthority="/tmp/Xauthority",
        headed_browser=True,
        vnc_password="",
        wait_seconds=5,
        check_only=False,
    )

    setup.configure(
        target="tcp://127.0.0.1:5900",
        kind="Linux desktop",
        name="",
        display=":0",
        xauthority="/tmp/Xauthority",
        headed_browser=True,
        vnc_password="",
        wait_seconds=5,
        check_only=False,
    )

    saved = (tmp_path / ".env").read_text(encoding="utf-8")
    for key in setup.VIRTUAL_DESKTOP_ENV_KEYS:
        assert key not in saved
        assert setup.os.environ[key] == setup.VIRTUAL_DESKTOP_ENV_VALUES[key]
    assert len(runtime_environments) == 4
    for environment in runtime_environments:
        for key, value in startup_session.items():
            assert environment[key] == value
        for key in set(setup.VIRTUAL_DESKTOP_ENV_KEYS) - set(startup_session):
            assert key not in environment


def test_runtime_environment_keeps_physical_x11_values_without_virtual_configuration(
    monkeypatch,
) -> None:
    monkeypatch.setenv(setup.XDG_SESSION_TYPE_ENV, "x11")
    monkeypatch.setenv(setup.GDK_BACKEND_ENV, "x11")
    monkeypatch.setenv(setup.QT_QPA_PLATFORM_ENV, "xcb")
    monkeypatch.setenv(setup.DBUS_SESSION_BUS_ADDRESS_ENV, "unix:path=/run/user/1000/bus")

    environment = setup._runtime_environment("Linux desktop")

    assert environment[setup.XDG_SESSION_TYPE_ENV] == "x11"
    assert environment[setup.GDK_BACKEND_ENV] == "x11"
    assert environment[setup.QT_QPA_PLATFORM_ENV] == "xcb"
    assert environment[setup.DBUS_SESSION_BUS_ADDRESS_ENV] == "unix:path=/run/user/1000/bus"


def test_configure_requires_a_managed_runtime_to_adopt_the_visible_display(
    tmp_path, monkeypatch
) -> None:
    class FakeComputerRuntime:
        def restart_computer_runtime(self):
            return True

        def ensure_computer_runtime(self, **kwargs):
            return "started"

    class FakeRelayRuntime:
        def restart_relay_runtime_for_reconfigure(self):
            return {"stopped": [], "left_running": [42]}

    monkeypatch.setattr(setup, "probe_desktop", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        setup,
        "_credentials",
        lambda: {"relay_url": "https://relay", "agent_id": "agent", "agent_secret": "secret"},
    )
    monkeypatch.setattr(setup, "hermes_home", lambda: tmp_path)
    monkeypatch.setattr(setup, "_computer_runtime_module", lambda: FakeComputerRuntime())
    monkeypatch.setattr(setup, "_relay_runtime_module", lambda: FakeRelayRuntime())

    with pytest.raises(setup.SetupError, match="manually managed Hermes gateway"):
        setup.configure(
            target="tcp://127.0.0.1:5901",
            kind="Virtual Linux desktop",
            name="",
            display=":1",
            xauthority="/tmp/fetch.Xauthority",
            headed_browser=True,
            vnc_password="",
            wait_seconds=5,
            check_only=False,
        )


def test_configure_accepts_a_restarted_manual_gateway(tmp_path, monkeypatch) -> None:
    calls = []
    owner_pids = [[42], [84]]

    class FakeComputerRuntime:
        def restart_computer_runtime(self):
            return True

        def ensure_computer_runtime(self, **kwargs):
            return "started"

    class FakeRelayRuntime:
        def restart_relay_runtime_for_reconfigure(self):
            return {"stopped": [], "left_running": owner_pids.pop(0)}

        def ensure_relay_runtime(self):
            raise AssertionError("A live manual gateway already owns the relay")

    monkeypatch.setattr(setup, "probe_desktop", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        setup,
        "_credentials",
        lambda: {"relay_url": "https://relay", "agent_id": "agent", "agent_secret": "secret"},
    )
    monkeypatch.setattr(setup, "hermes_home", lambda: tmp_path)
    monkeypatch.setattr(setup, "_computer_runtime_module", lambda: FakeComputerRuntime())
    monkeypatch.setattr(setup, "_relay_runtime_module", lambda: FakeRelayRuntime())
    monkeypatch.setattr(
        setup,
        "wait_for_relay",
        lambda credentials, wait_seconds: calls.append("ready") or {"ok": True},
    )

    kwargs = {
        "target": "tcp://127.0.0.1:5901",
        "kind": "Virtual Linux desktop",
        "name": "",
        "display": ":1",
        "xauthority": "/tmp/fetch.Xauthority",
        "headed_browser": True,
        "vnc_password": "",
        "wait_seconds": 5,
        "check_only": False,
    }
    with pytest.raises(setup.SetupError, match="Restart that gateway"):
        setup.configure(**kwargs)

    assert setup.configure(**kwargs) == {"ok": True}
    assert calls == ["ready"]
    assert not (tmp_path / "run" / "fetch-computer-gateway-restart.json").exists()


def test_disable_computer_requires_manual_gateway_restart(tmp_path, monkeypatch) -> None:
    owner_pids = [[42], [84]]

    class FakeComputerRuntime:
        def restart_computer_runtime(self):
            return True

    class FakeRelayRuntime:
        def restart_relay_runtime_for_reconfigure(self):
            return {"stopped": [], "left_running": owner_pids.pop(0)}

        def ensure_relay_runtime(self):
            raise AssertionError("A live manual gateway already owns the relay")

    monkeypatch.setattr(setup, "hermes_home", lambda: tmp_path)
    monkeypatch.setattr(setup, "_computer_runtime_module", lambda: FakeComputerRuntime())
    monkeypatch.setattr(setup, "_relay_runtime_module", lambda: FakeRelayRuntime())

    with pytest.raises(setup.SetupError, match="Restart that gateway"):
        setup.disable_computer()

    setup.disable_computer()
    assert not (tmp_path / "run" / "fetch-computer-gateway-restart.json").exists()
