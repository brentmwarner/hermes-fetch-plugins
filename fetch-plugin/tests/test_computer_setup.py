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


def test_remove_environment_keys_drops_computer_settings_and_keeps_other_lines(tmp_path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "# keep\nOTHER=value\nHERMES_FETCH_COMPUTER_TARGET=tcp://127.0.0.1:5901\n"
        "export HERMES_FETCH_COMPUTER_KIND=Old\nHERMES_FETCH_TUNNEL_ENABLED=1\n",
        encoding="utf-8",
    )

    setup.remove_environment_keys(env_path, setup.COMPUTER_ENV_KEYS)

    text = env_path.read_text(encoding="utf-8")
    assert "# keep" in text
    assert "OTHER=value" in text
    assert "HERMES_FETCH_TUNNEL_ENABLED=1" in text
    assert "HERMES_FETCH_COMPUTER_TARGET" not in text
    assert "HERMES_FETCH_COMPUTER_KIND" not in text


def test_disable_computer_stops_bridge_and_clears_persisted_target(tmp_path, monkeypatch) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        'HERMES_FETCH_COMPUTER_TARGET="tcp://127.0.0.1:5901"\nHERMES_FETCH_TUNNEL_ENABLED="1"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_FETCH_COMPUTER_TARGET", "tcp://127.0.0.1:5901")
    monkeypatch.setenv("HERMES_FETCH_COMPUTER_KIND", "Virtual Linux desktop")
    monkeypatch.setattr(setup, "hermes_home", lambda: tmp_path)
    calls = []

    class FakeRuntime:
        def restart_computer_runtime(self):
            calls.append("stop")
            return True

    monkeypatch.setattr(setup, "_computer_runtime_module", lambda: FakeRuntime())

    setup.disable_computer()

    assert calls == ["stop"]
    saved = env_path.read_text(encoding="utf-8")
    assert "HERMES_FETCH_COMPUTER_TARGET" not in saved
    assert 'HERMES_FETCH_TUNNEL_ENABLED="1"' in saved
    assert "HERMES_FETCH_COMPUTER_TARGET" not in setup.os.environ
    assert "HERMES_FETCH_COMPUTER_KIND" not in setup.os.environ


def test_disable_computer_fails_when_bridge_cannot_be_stopped(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(setup, "hermes_home", lambda: tmp_path)

    class FakeRuntime:
        def restart_computer_runtime(self):
            return False

    monkeypatch.setattr(setup, "_computer_runtime_module", lambda: FakeRuntime())

    with pytest.raises(setup.SetupError, match="Could not stop"):
        setup.disable_computer()


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


def test_probe_desktop_requires_an_actual_rfb_server() -> None:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def serve() -> None:
        connection, _address = server.accept()
        with connection:
            connection.sendall(b"RFB 003.008\n")
        server.close()

    thread = threading.Thread(target=serve)
    thread.start()
    setup.probe_desktop(f"tcp://127.0.0.1:{port}", wait_seconds=1)
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

    class FakeRuntime:
        def restart_computer_runtime(self):
            calls.append("restart")
            return True

        def ensure_computer_runtime(self):
            calls.append("start")
            return "started"

    monkeypatch.setattr(setup, "probe_desktop", lambda target, wait_seconds: calls.append("probe"))
    monkeypatch.setattr(
        setup,
        "_credentials",
        lambda: {"relay_url": "https://relay", "agent_id": "agent", "agent_secret": "secret"},
    )
    monkeypatch.setattr(setup, "hermes_home", lambda: tmp_path)
    monkeypatch.setattr(setup, "_load_sibling", lambda module_name, filename: FakeRuntime())
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
        wait_seconds=5,
        check_only=False,
    )

    assert result == {"ok": True}
    assert calls == ["probe", "restart", "start", "ready"]
    saved = (tmp_path / ".env").read_text(encoding="utf-8")
    assert 'DISPLAY=":1"' in saved
    assert 'HERMES_FETCH_COMPUTER_NAME="Hermes VPS"' in saved
