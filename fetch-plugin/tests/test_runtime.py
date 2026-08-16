import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

# Load _runtime.py by path the same way the plugin does.
_p = Path(__file__).resolve().parent.parent / "_runtime.py"
_spec = importlib.util.spec_from_file_location("fetch_plugin_runtime_test", _p)
runtime = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = runtime
_spec.loader.exec_module(runtime)


class FakeProcess:
    pid = 4242

    def wait(self, timeout=None):
        return 0


def test_ensure_relay_runtime_starts_child_with_tunnel_env(tmp_path, monkeypatch) -> None:
    calls = []

    def fake_popen(args, **kwargs):
        calls.append((args, kwargs))
        return FakeProcess()

    monkeypatch.setattr(runtime, "_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(runtime, "_active_runtime_pid", lambda **kwargs: None)
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


def test_ensure_relay_runtime_uses_explicit_child_environment(tmp_path, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(runtime, "_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(runtime, "_active_runtime_pid", lambda **kwargs: None)
    monkeypatch.setattr(runtime, "_child_pythonpath", lambda: "/tmp/hermes-agent")
    monkeypatch.setattr(runtime, "_child_python_executable", lambda: "/tmp/hermes-venv/bin/python")
    monkeypatch.setattr(
        runtime.subprocess,
        "Popen",
        lambda args, **kwargs: calls.append((args, kwargs)) or FakeProcess(),
    )
    monkeypatch.delenv(runtime.DISABLE_AUTOSTART_ENV, raising=False)
    monkeypatch.delenv(runtime.AUTOSTART_RUNTIME_ENV, raising=False)

    assert runtime.ensure_relay_runtime(environment={"DISPLAY": ":1"}) == "started"
    assert calls[0][1]["env"]["DISPLAY"] == ":1"
    assert runtime.TUNNEL_ENABLED_ENV in calls[0][1]["env"]


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
    monkeypatch.setattr(runtime, "_active_runtime_pid", lambda **kwargs: 1234)
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

    assert runtime._active_runtime_pid(reclaim_legacy=True) is None
    assert not (runtime_dir / "fetch-relay-runtime.pid").exists()


def test_active_runtime_pid_retires_legacy_autostart_runtime(tmp_path, monkeypatch) -> None:
    runtime_dir = tmp_path / "run"
    runtime_dir.mkdir()
    (runtime_dir / "fetch-relay-runtime.pid").write_text("4242", encoding="utf-8")
    terminated = []
    monkeypatch.setattr(runtime, "_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(runtime, "_process_alive", lambda pid: True)
    monkeypatch.setattr(
        runtime,
        "_process_command",
        lambda pid: "python -c HERMES_FETCH_TUNNEL_AUTOSTARTED_RUNTIME",
    )
    monkeypatch.setattr(
        runtime,
        "_terminate_process",
        lambda pid: terminated.append(pid) or True,
    )

    assert runtime._active_runtime_pid(reclaim_legacy=True) is None
    assert terminated == [4242]
    assert not (runtime_dir / "fetch-relay-runtime.pid").exists()


def test_active_runtime_pid_inspects_legacy_autostart_runtime_without_reclaim(tmp_path, monkeypatch) -> None:
    runtime_dir = tmp_path / "run"
    runtime_dir.mkdir()
    pid_path = runtime_dir / "fetch-relay-runtime.pid"
    pid_path.write_text("4242", encoding="utf-8")
    monkeypatch.setattr(runtime, "_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(runtime, "_process_alive", lambda pid: True)
    monkeypatch.setattr(
        runtime,
        "_process_command",
        lambda pid: "python -c HERMES_FETCH_TUNNEL_AUTOSTARTED_RUNTIME",
    )
    monkeypatch.setattr(
        runtime,
        "_terminate_process",
        lambda pid: (_ for _ in ()).throw(AssertionError("read-only lookup should not terminate")),
    )

    assert runtime._active_runtime_pid() == 4242
    assert pid_path.exists()


def test_terminate_process_waits_after_sigkill(monkeypatch) -> None:
    signals = []
    alive = {"value": True}

    def fake_kill(pid, sig):
        signals.append(sig)
        if sig == runtime.signal.SIGKILL:
            alive["value"] = False

    monkeypatch.setattr(runtime.os, "kill", fake_kill)
    monkeypatch.setattr(runtime, "_process_alive", lambda pid: alive["value"])

    assert runtime._terminate_process(4242, timeout_s=0) is True
    assert signals == [runtime.signal.SIGTERM, runtime.signal.SIGKILL]


def test_active_runtime_pid_drops_legacy_pid_when_command_unavailable(tmp_path, monkeypatch) -> None:
    runtime_dir = tmp_path / "run"
    runtime_dir.mkdir()
    (runtime_dir / "fetch-relay-runtime.pid").write_text("4242", encoding="utf-8")
    monkeypatch.setattr(runtime, "_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(runtime, "_process_alive", lambda pid: True)
    monkeypatch.setattr(runtime, "_process_command", lambda pid: None)
    monkeypatch.setattr(
        runtime,
        "_terminate_process",
        lambda pid: (_ for _ in ()).throw(AssertionError("should not terminate unknown process")),
    )

    assert runtime._active_runtime_pid(reclaim_legacy=True) is None
    assert not (runtime_dir / "fetch-relay-runtime.pid").exists()


def test_active_runtime_pid_inspects_command_unavailable_legacy_pid_without_reclaim(tmp_path, monkeypatch) -> None:
    runtime_dir = tmp_path / "run"
    runtime_dir.mkdir()
    pid_path = runtime_dir / "fetch-relay-runtime.pid"
    pid_path.write_text("4242", encoding="utf-8")
    monkeypatch.setattr(runtime, "_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(runtime, "_process_alive", lambda pid: True)
    monkeypatch.setattr(runtime, "_process_command", lambda pid: None)
    monkeypatch.setattr(
        runtime,
        "_terminate_process",
        lambda pid: (_ for _ in ()).throw(AssertionError("read-only lookup should not terminate")),
    )

    assert runtime._active_runtime_pid() is None
    assert pid_path.exists()


def test_active_runtime_pid_keeps_structured_runtime_record(tmp_path, monkeypatch) -> None:
    runtime_dir = tmp_path / "run"
    runtime_dir.mkdir()
    (runtime_dir / "fetch-relay-runtime.pid").write_text(
        json.dumps({"pid": 4242, "role": "fetch-relay-runtime"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(runtime, "_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(runtime, "_process_alive", lambda pid: True)
    monkeypatch.setattr(
        runtime,
        "_process_command",
        lambda pid: "python -c HERMES_FETCH_TUNNEL_AUTOSTARTED_RUNTIME",
    )
    monkeypatch.setattr(
        runtime,
        "_terminate_process",
        lambda pid: (_ for _ in ()).throw(AssertionError("should not terminate")),
    )

    assert runtime._active_runtime_pid() == 4242


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
    monkeypatch.delenv(runtime.DISABLE_AUTOSTART_ENV, raising=False)
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


# --- reconfigure handoff: stop superseded relay processes ---


def _write_tunnel_lock(run_dir, name, pid, agent_id="agent-old"):
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / name
    path.write_text(
        json.dumps({"pid": pid, "role": "fetch-tunnel-owner", "agent_id": agent_id}),
        encoding="utf-8",
    )
    return path


def test_reconfigure_stops_runtime_child_and_its_lock(tmp_path, monkeypatch) -> None:
    """The autostarted runtime (and the tunnel lock it holds) is terminated so
    ensure_relay_runtime() boots a fresh process with the new credentials."""
    run_dir = tmp_path / "run"
    monkeypatch.setattr(runtime, "_hermes_home", lambda: tmp_path)
    lock = _write_tunnel_lock(run_dir, "fetch-tunnel-agent-old.pid", 5151)
    runtime._write_pid_record(runtime._pid_path(), 5151)

    alive = {5151: True}
    killed = []

    monkeypatch.setattr(runtime, "_process_alive", lambda pid: alive.get(pid, False))
    monkeypatch.setattr(
        runtime,
        "_process_command",
        lambda pid: "python -c import os ... HERMES_FETCH_TUNNEL_AUTOSTARTED_RUNTIME ... dashboard",
    )

    def fake_terminate(pid, **kwargs):
        killed.append(pid)
        alive[pid] = False
        return True

    monkeypatch.setattr(runtime, "_terminate_process", fake_terminate)

    result = runtime.restart_relay_runtime_for_reconfigure()

    assert killed == [5151]
    assert result["stopped"] == [5151]
    assert result["left_running"] == []
    assert not runtime._pid_path().exists()
    assert not lock.exists()


def test_reconfigure_leaves_live_gateway_owner_running(tmp_path, monkeypatch) -> None:
    """A gateway process that owns the uplink is NOT killed (it would take the
    user's sessions down); it self-heals by reloading credentials on the next
    auth rejection."""
    run_dir = tmp_path / "run"
    monkeypatch.setattr(runtime, "_hermes_home", lambda: tmp_path)
    lock = _write_tunnel_lock(run_dir, "fetch-tunnel-agent-old.pid", 6161)

    monkeypatch.setattr(runtime, "_process_alive", lambda pid: True)
    monkeypatch.setattr(
        runtime, "_process_command", lambda pid: "python -m hermes_cli.main gateway"
    )
    monkeypatch.setattr(
        runtime,
        "_terminate_process",
        lambda pid, **kwargs: (_ for _ in ()).throw(AssertionError("must not kill gateway")),
    )

    result = runtime.restart_relay_runtime_for_reconfigure()

    assert result["stopped"] == []
    assert result["left_running"] == [6161]
    assert lock.exists()


def test_reconfigure_clears_dead_owner_locks(tmp_path, monkeypatch) -> None:
    run_dir = tmp_path / "run"
    monkeypatch.setattr(runtime, "_hermes_home", lambda: tmp_path)
    lock = _write_tunnel_lock(run_dir, "fetch-tunnel-agent-old.pid", 7171)

    monkeypatch.setattr(runtime, "_process_alive", lambda pid: False)
    monkeypatch.setattr(
        runtime,
        "_terminate_process",
        lambda pid, **kwargs: (_ for _ in ()).throw(AssertionError("nothing to kill")),
    )

    result = runtime.restart_relay_runtime_for_reconfigure()

    assert result["stopped"] == []
    assert result["left_running"] == []
    assert not lock.exists()


def _wait_for_state_zombie(pid: int, timeout_s: float = 5.0) -> bool:
    """Poll ps (impl-independent) until ``pid`` shows as a zombie."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        out = subprocess.run(
            ["ps", "-o", "state=", "-p", str(pid)], capture_output=True, text=True
        ).stdout.strip()
        if out.upper().startswith("Z"):
            return True
        time.sleep(0.02)
    return False


def test_process_alive_treats_zombie_child_as_dead() -> None:
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    try:
        assert _wait_for_state_zombie(pid)
        assert runtime._process_alive(pid) is False
    finally:
        os.waitpid(pid, 0)


def test_terminate_process_succeeds_when_child_zombifies() -> None:
    # The caller holds the Popen without waiting, so the SIGTERM'd child
    # becomes a zombie of this very process - the gateway-setup geometry.
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert runtime._terminate_process(proc.pid, timeout_s=3.0) is True
    finally:
        try:
            proc.kill()
        except OSError:
            pass
        proc.wait()


def test_ensure_relay_runtime_reaps_exited_child(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runtime, "_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(runtime, "_active_runtime_pid", lambda **kwargs: None)
    monkeypatch.setattr(runtime, "_child_pythonpath", lambda: "")
    monkeypatch.setattr(runtime, "_child_python_executable", lambda: sys.executable)
    monkeypatch.setattr(runtime, "_child_script", lambda: "raise SystemExit(0)")
    monkeypatch.delenv(runtime.DISABLE_AUTOSTART_ENV, raising=False)
    monkeypatch.delenv(runtime.AUTOSTART_RUNTIME_ENV, raising=False)

    assert runtime.ensure_relay_runtime() == "started"
    pid = json.loads(
        (tmp_path / "run" / "fetch-relay-runtime.pid").read_text(encoding="utf-8")
    )["pid"]

    deadline = time.time() + 5.0
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return  # fully reaped: no zombie left behind
        except PermissionError:
            break  # pid reused by another user's process: reaped long ago
        time.sleep(0.02)
    else:
        pytest.fail(f"runtime child pid {pid} was never reaped")
