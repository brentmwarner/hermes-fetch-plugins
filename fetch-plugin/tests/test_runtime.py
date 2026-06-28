import importlib.util
import json
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
    monkeypatch.setattr(runtime, "_child_pythonpath", lambda: "/tmp/hermes-agent")
    monkeypatch.setattr(runtime, "_child_python_executable", lambda: "/tmp/hermes-venv/bin/python")
    monkeypatch.setattr(runtime.subprocess, "Popen", fake_popen)
    monkeypatch.delenv(runtime.DISABLE_AUTOSTART_ENV, raising=False)
    monkeypatch.delenv(runtime.AUTOSTART_RUNTIME_ENV, raising=False)

    assert runtime.ensure_relay_runtime() == "started"

    pid_record = json.loads(
        (tmp_path / "run" / "fetch-relay-runtime.pid").read_text(encoding="utf-8")
    )
    assert pid_record["pid"] == 4242
    assert pid_record["role"] == "fetch-relay-runtime"
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[:2] == ["/tmp/hermes-venv/bin/python", "-c"]
    assert "discover_plugins()" in args[2]
    assert "start_server(host=DASHBOARD_HOST" in args[2]
    assert "time.sleep(3600)" in args[2]
    assert kwargs["env"][runtime.TUNNEL_ENABLED_ENV] == "1"
    assert kwargs["env"][runtime.AUTOSTART_RUNTIME_ENV] == "1"
    assert kwargs["env"]["PYTHONPATH"] == "/tmp/hermes-agent"
    assert kwargs["stdin"] == runtime.subprocess.DEVNULL
    assert kwargs["stderr"] == runtime.subprocess.STDOUT


def test_child_python_executable_prefers_hermes_venv(tmp_path, monkeypatch) -> None:
    python_path = tmp_path / "venv" / "bin" / "python"
    python_path.parent.mkdir(parents=True)
    python_path.write_text("#!/usr/bin/env python\n", encoding="utf-8")
    monkeypatch.setattr(runtime, "_hermes_project_root", lambda: tmp_path)

    assert runtime._child_python_executable() == str(python_path)


def test_child_python_executable_falls_back_to_current_python(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runtime, "_hermes_project_root", lambda: tmp_path)

    assert runtime._child_python_executable() == sys.executable


def test_child_pythonpath_ignores_parent_interpreter_paths(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runtime, "_hermes_project_root", lambda: tmp_path)
    monkeypatch.setattr(runtime.sys, "path", ["/tmp/python-3.13-stdlib"])
    monkeypatch.setenv("PYTHONPATH", "/tmp/operator-path")

    assert runtime._child_pythonpath() == str(tmp_path)


def test_ensure_relay_runtime_uses_existing_pid(monkeypatch) -> None:
    monkeypatch.setattr(runtime, "_active_runtime_pid", lambda: 1234)
    monkeypatch.delenv(runtime.DISABLE_AUTOSTART_ENV, raising=False)
    monkeypatch.delenv(runtime.AUTOSTART_RUNTIME_ENV, raising=False)

    assert runtime.ensure_relay_runtime() == "already-running"


def test_active_runtime_pid_reclaims_live_foreign_pid(tmp_path, monkeypatch) -> None:
    runtime_dir = tmp_path / "run"
    runtime_dir.mkdir()
    (runtime_dir / "fetch-relay-runtime.pid").write_text("4242", encoding="utf-8")
    monkeypatch.setattr(runtime, "_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(runtime, "_process_alive", lambda pid: True)
    monkeypatch.setattr(
        runtime,
        "_process_command",
        lambda pid: "python /tmp/fetch_runtime_restart.py",
    )

    assert runtime._active_runtime_pid() is None
    assert not (runtime_dir / "fetch-relay-runtime.pid").exists()


def test_active_runtime_pid_rejects_non_positive_json_pid(tmp_path, monkeypatch) -> None:
    runtime_dir = tmp_path / "run"
    runtime_dir.mkdir()
    monkeypatch.setattr(runtime, "_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(runtime, "_process_alive", lambda pid: True)
    monkeypatch.setattr(runtime, "_process_command", lambda pid: None)

    for pid in (0, -42):
        path = runtime_dir / "fetch-relay-runtime.pid"
        path.write_text(
            json.dumps({"pid": pid, "role": "fetch-relay-runtime"}),
            encoding="utf-8",
        )

        assert runtime._active_runtime_pid() is None


def test_active_runtime_pid_rejects_non_positive_legacy_pid(tmp_path, monkeypatch) -> None:
    runtime_dir = tmp_path / "run"
    runtime_dir.mkdir()
    monkeypatch.setattr(runtime, "_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(runtime, "_process_alive", lambda pid: True)

    for pid in ("0", "-42"):
        (runtime_dir / "fetch-relay-runtime.pid").write_text(pid, encoding="utf-8")

        assert runtime._active_runtime_pid() is None


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
