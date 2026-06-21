import importlib.util
import os
import sys
from pathlib import Path

# Load _runtime.py by path the same way the plugin does.
_p = Path(__file__).resolve().parent.parent / "_runtime.py"
_spec = importlib.util.spec_from_file_location("fetch_plugin_runtime_test", _p)
runtime = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = runtime
_spec.loader.exec_module(runtime)


class FakeProcess:
    pid = 4242


def test_ensure_relay_runtime_starts_child_with_tunnel_env(tmp_path, monkeypatch) -> None:
    calls = []

    def fake_popen(args, **kwargs):
        calls.append((args, kwargs))
        return FakeProcess()

    monkeypatch.setattr(runtime, "_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(runtime, "_active_runtime_pid", lambda: None)
    monkeypatch.setattr(runtime.subprocess, "Popen", fake_popen)
    monkeypatch.delenv(runtime.DISABLE_AUTOSTART_ENV, raising=False)
    monkeypatch.delenv(runtime.AUTOSTART_RUNTIME_ENV, raising=False)

    assert runtime.ensure_relay_runtime() == "started"

    assert (tmp_path / "run" / "fetch-relay-runtime.pid").read_text(encoding="utf-8") == "4242"
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[:2] == [sys.executable, "-c"]
    assert "discover_plugins()" in args[2]
    assert "start_server(host=DASHBOARD_HOST" in args[2]
    assert "time.sleep(3600)" in args[2]
    assert kwargs["env"][runtime.TUNNEL_ENABLED_ENV] == "1"
    assert kwargs["env"][runtime.AUTOSTART_RUNTIME_ENV] == "1"
    assert kwargs["stdin"] == runtime.subprocess.DEVNULL
    assert kwargs["stderr"] == runtime.subprocess.STDOUT


def test_ensure_relay_runtime_uses_existing_pid(monkeypatch) -> None:
    monkeypatch.setattr(runtime, "_active_runtime_pid", lambda: 1234)
    monkeypatch.delenv(runtime.DISABLE_AUTOSTART_ENV, raising=False)
    monkeypatch.delenv(runtime.AUTOSTART_RUNTIME_ENV, raising=False)

    assert runtime.ensure_relay_runtime() == "already-running"


def test_ensure_relay_runtime_respects_autostart_sentinel(monkeypatch) -> None:
    monkeypatch.setenv(runtime.AUTOSTART_RUNTIME_ENV, "1")

    assert runtime.ensure_relay_runtime() == "self"


def test_ensure_relay_runtime_respects_disable_env(monkeypatch) -> None:
    monkeypatch.setenv(runtime.DISABLE_AUTOSTART_ENV, "true")
    monkeypatch.delenv(runtime.AUTOSTART_RUNTIME_ENV, raising=False)

    assert runtime.ensure_relay_runtime() == "disabled"


def test_enable_tunnel_for_future_starts_sets_current_env(monkeypatch) -> None:
    monkeypatch.delenv(runtime.TUNNEL_ENABLED_ENV, raising=False)

    runtime.enable_tunnel_for_future_starts()

    assert os.environ[runtime.TUNNEL_ENABLED_ENV] == "1"
