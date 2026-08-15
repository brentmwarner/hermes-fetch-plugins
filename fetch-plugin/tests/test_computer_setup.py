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
