import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

_path = Path(__file__).resolve().parent.parent / "_computer_runtime.py"
_spec = importlib.util.spec_from_file_location("fetch_plugin_computer_runtime_test", _path)
computer_runtime = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = computer_runtime
_spec.loader.exec_module(computer_runtime)


class FakeProcess:
    pid = 4242


def _fake_runtime(tmp_path, *, command=None, terminate=True):
    terminated = []
    reaped = []
    owned_command = f"python -c {computer_runtime.AUTOSTART_ENV}"

    def _terminate(pid):
        terminated.append(pid)
        return terminate

    def _command(_pid):
        return owned_command if command is None else command

    runtime = SimpleNamespace(
        _runtime_dir=lambda: tmp_path / "run",
        _hermes_home=lambda: tmp_path,
        _child_pythonpath=lambda: "/tmp/hermes",
        _child_python_executable=lambda: "/tmp/hermes/bin/python",
        _process_alive=lambda pid: True,
        _process_command=_command,
        _terminate_process=_terminate,
        _spawn_reaper=lambda process: reaped.append(process),
        terminated=terminated,
        reaped=reaped,
    )
    return runtime


def _write_pid(tmp_path, pid=4242, *, role="fetch-computer-runtime", signature=None) -> Path:
    runtime_dir = tmp_path / "run"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    path = runtime_dir / "fetch-computer-runtime.pid"
    record = {"pid": pid, "role": role, "signature": signature or computer_runtime._signature()}
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def test_ensure_starts_dedicated_computer_process(tmp_path, monkeypatch) -> None:
    credentials = tmp_path / "push" / "fetch-relay.json"
    credentials.parent.mkdir()
    credentials.write_text(
        json.dumps(
            {
                "relay_url": "https://relay.example.com",
                "agent_id": "agent-1",
                "agent_secret": "secret",
            }
        ),
        encoding="utf-8",
    )
    calls = []
    monkeypatch.setenv(computer_runtime.TARGET_ENV, "tcp://127.0.0.1:5901")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.delenv(computer_runtime.AUTOSTART_ENV, raising=False)
    monkeypatch.delenv(computer_runtime.DISABLE_AUTOSTART_ENV, raising=False)
    monkeypatch.setattr(computer_runtime, "_load_runtime_module", lambda: _fake_runtime(tmp_path))
    monkeypatch.setattr(computer_runtime, "_credentials_path", lambda: credentials)
    monkeypatch.setattr(computer_runtime, "_active_pid", lambda **kwargs: None)
    monkeypatch.setattr(
        computer_runtime.subprocess,
        "Popen",
        lambda args, **kwargs: calls.append((args, kwargs)) or FakeProcess(),
    )

    assert computer_runtime.ensure_computer_runtime() == "started"

    record = json.loads(
        (tmp_path / "run" / "fetch-computer-runtime.pid").read_text(encoding="utf-8")
    )
    assert record["pid"] == 4242
    assert record["role"] == "fetch-computer-runtime"
    assert record["signature"] == computer_runtime._signature()
    assert len(calls) == 1
    assert calls[0][1]["env"][computer_runtime.AUTOSTART_ENV] == "1"


def test_ensure_computer_runtime_uses_explicit_child_environment(tmp_path, monkeypatch) -> None:
    credentials = tmp_path / "push" / "fetch-relay.json"
    credentials.parent.mkdir()
    credentials.write_text("{}", encoding="utf-8")
    calls = []
    monkeypatch.setenv(computer_runtime.TARGET_ENV, "tcp://127.0.0.1:5901")
    monkeypatch.delenv(computer_runtime.AUTOSTART_ENV, raising=False)
    monkeypatch.delenv(computer_runtime.DISABLE_AUTOSTART_ENV, raising=False)
    monkeypatch.setattr(computer_runtime, "_load_runtime_module", lambda: _fake_runtime(tmp_path))
    monkeypatch.setattr(computer_runtime, "_credentials_path", lambda: credentials)
    monkeypatch.setattr(computer_runtime, "_active_pid", lambda **kwargs: None)
    monkeypatch.setattr(
        computer_runtime.subprocess,
        "Popen",
        lambda args, **kwargs: calls.append((args, kwargs)) or FakeProcess(),
    )

    assert computer_runtime.ensure_computer_runtime(environment={"DISPLAY": ":1"}) == "started"
    assert calls[0][1]["env"]["DISPLAY"] == ":1"
    assert calls[0][1]["env"][computer_runtime.TARGET_ENV] == "tcp://127.0.0.1:5901"
    assert "WAYLAND_DISPLAY" not in calls[0][1]["env"]


def test_ensure_does_not_start_before_pairing(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(computer_runtime.TARGET_ENV, "tcp://127.0.0.1:5901")
    monkeypatch.delenv(computer_runtime.AUTOSTART_ENV, raising=False)
    monkeypatch.delenv(computer_runtime.DISABLE_AUTOSTART_ENV, raising=False)
    monkeypatch.setattr(computer_runtime, "_credentials_path", lambda: tmp_path / "missing.json")

    assert computer_runtime.ensure_computer_runtime() == "unpaired"


def test_child_reads_vnc_password_locally_without_embedding_it(monkeypatch) -> None:
    monkeypatch.setenv(computer_runtime.VNC_PASSWORD_ENV, "never-embed-this")

    script = computer_runtime._child_script()

    assert computer_runtime.VNC_PASSWORD_ENV in script
    assert "vnc_password=os.environ.get" in script
    assert "never-embed-this" not in script


def test_active_pid_does_not_terminate_reused_foreign_process(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(computer_runtime.TARGET_ENV, "tcp://127.0.0.1:5901")
    monkeypatch.setenv("HERMES_FETCH_STORE_HOME", str(tmp_path))
    runtime = _fake_runtime(tmp_path, command="nginx: master process")
    monkeypatch.setattr(computer_runtime, "_load_runtime_module", lambda: runtime)
    pid_path = _write_pid(tmp_path)

    assert computer_runtime._active_pid(reclaim=True) is None
    assert runtime.terminated == []
    assert not pid_path.exists()


def test_restart_does_not_kill_reused_foreign_pid(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(computer_runtime.TARGET_ENV, "tcp://127.0.0.1:5901")
    monkeypatch.setenv("HERMES_FETCH_STORE_HOME", str(tmp_path))
    runtime = _fake_runtime(tmp_path, command="nginx: master process")
    monkeypatch.setattr(computer_runtime, "_load_runtime_module", lambda: runtime)
    pid_path = _write_pid(tmp_path)

    assert computer_runtime.restart_computer_runtime() is True
    assert runtime.terminated == []
    assert pid_path.exists()


def test_active_pid_keeps_owned_computer_runtime(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(computer_runtime.TARGET_ENV, "tcp://127.0.0.1:5901")
    monkeypatch.setenv("HERMES_FETCH_STORE_HOME", str(tmp_path))
    runtime = _fake_runtime(tmp_path)
    monkeypatch.setattr(computer_runtime, "_load_runtime_module", lambda: runtime)
    _write_pid(tmp_path)

    assert computer_runtime._active_pid(reclaim=True) == 4242
    assert runtime.terminated == []


def test_restart_terminates_owned_computer_runtime(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(computer_runtime.TARGET_ENV, "tcp://127.0.0.1:5901")
    monkeypatch.setenv("HERMES_FETCH_STORE_HOME", str(tmp_path))
    runtime = _fake_runtime(tmp_path)
    monkeypatch.setattr(computer_runtime, "_load_runtime_module", lambda: runtime)
    pid_path = _write_pid(tmp_path)

    assert computer_runtime.restart_computer_runtime() is True
    assert runtime.terminated == [4242]
    assert not pid_path.exists()


def test_ensure_respects_disable_env(monkeypatch) -> None:
    monkeypatch.delenv(computer_runtime.AUTOSTART_ENV, raising=False)
    monkeypatch.setenv(computer_runtime.DISABLE_AUTOSTART_ENV, "1")
    monkeypatch.setenv(computer_runtime.TARGET_ENV, "tcp://127.0.0.1:5901")

    assert computer_runtime.ensure_computer_runtime() == "disabled"


def test_ensure_spawns_reaper_for_child(tmp_path, monkeypatch) -> None:
    credentials = tmp_path / "push" / "fetch-relay.json"
    credentials.parent.mkdir()
    credentials.write_text("{}", encoding="utf-8")
    fake = _fake_runtime(tmp_path)
    process = FakeProcess()
    monkeypatch.setenv(computer_runtime.TARGET_ENV, "tcp://127.0.0.1:5901")
    monkeypatch.delenv(computer_runtime.AUTOSTART_ENV, raising=False)
    monkeypatch.delenv(computer_runtime.DISABLE_AUTOSTART_ENV, raising=False)
    monkeypatch.setattr(computer_runtime, "_load_runtime_module", lambda: fake)
    monkeypatch.setattr(computer_runtime, "_credentials_path", lambda: credentials)
    monkeypatch.setattr(computer_runtime, "_active_pid", lambda **kwargs: None)
    monkeypatch.setattr(
        computer_runtime.subprocess, "Popen", lambda args, **kwargs: process
    )

    assert computer_runtime.ensure_computer_runtime() == "started"
    assert fake.reaped == [process]


def test_ambient_ensure_defers_to_live_runtime_with_other_signature(tmp_path, monkeypatch) -> None:
    # A gateway hook whose environment merely differs must not steal the
    # bridge: two configs alternately killing each other's runtime drops the
    # computer channel on every swap.
    credentials = tmp_path / "push" / "fetch-relay.json"
    credentials.parent.mkdir()
    credentials.write_text("{}", encoding="utf-8")
    fake = _fake_runtime(tmp_path)
    monkeypatch.setenv(computer_runtime.TARGET_ENV, "tcp://127.0.0.1:5901")
    monkeypatch.setenv("HERMES_FETCH_STORE_HOME", str(tmp_path))
    monkeypatch.delenv(computer_runtime.AUTOSTART_ENV, raising=False)
    monkeypatch.delenv(computer_runtime.DISABLE_AUTOSTART_ENV, raising=False)
    monkeypatch.setattr(computer_runtime, "_load_runtime_module", lambda: fake)
    monkeypatch.setattr(computer_runtime, "_credentials_path", lambda: credentials)
    pid_path = _write_pid(tmp_path, signature="someone-elses-config")

    assert computer_runtime.ensure_computer_runtime() == "already-running"
    assert fake.terminated == []
    assert pid_path.exists()


def test_explicit_environment_ensure_replaces_mismatched_runtime(tmp_path, monkeypatch) -> None:
    # Setup flows pass an explicit environment: that is an intentional
    # reconfigure and may swap the running bridge.
    credentials = tmp_path / "push" / "fetch-relay.json"
    credentials.parent.mkdir()
    credentials.write_text("{}", encoding="utf-8")
    fake = _fake_runtime(tmp_path)
    process = FakeProcess()
    monkeypatch.setenv(computer_runtime.TARGET_ENV, "tcp://127.0.0.1:5901")
    monkeypatch.setenv("HERMES_FETCH_STORE_HOME", str(tmp_path))
    monkeypatch.delenv(computer_runtime.AUTOSTART_ENV, raising=False)
    monkeypatch.delenv(computer_runtime.DISABLE_AUTOSTART_ENV, raising=False)
    monkeypatch.setattr(computer_runtime, "_load_runtime_module", lambda: fake)
    monkeypatch.setattr(computer_runtime, "_credentials_path", lambda: credentials)
    monkeypatch.setattr(
        computer_runtime.subprocess, "Popen", lambda args, **kwargs: process
    )
    _write_pid(tmp_path, signature="someone-elses-config")

    assert computer_runtime.ensure_computer_runtime(environment={"DISPLAY": ":1"}) == "started"
    assert fake.terminated == [4242]
    assert fake.reaped == [process]


def test_keeper_ensure_skips_target_gone_stale_after_disable(tmp_path, monkeypatch) -> None:
    # disable_computer() in another process removed the persisted configuration
    # but cannot scrub this host's environment: the keeper must not resurrect
    # the bridge from the stale value.
    monkeypatch.setenv("HERMES_FETCH_STORE_HOME", str(tmp_path))
    monkeypatch.setenv(computer_runtime.TARGET_ENV, "tcp://127.0.0.1:5900")
    monkeypatch.delenv(computer_runtime.LEGACY_TARGET_ENV, raising=False)
    calls = []
    monkeypatch.setattr(
        computer_runtime, "ensure_computer_runtime", lambda: calls.append(1) or "started"
    )

    assert computer_runtime.keeper_ensure_computer_runtime() == "stale-config"
    assert calls == []

    # A different persisted target belongs to a newer configuration owned by
    # another process; a stale host must not fight it either.
    (tmp_path / ".env").write_text(
        f'{computer_runtime.TARGET_ENV}="tcp://127.0.0.1:5999"\n', encoding="utf-8"
    )
    assert computer_runtime.keeper_ensure_computer_runtime() == "stale-config"
    assert calls == []


def test_keeper_ensure_runs_when_environment_matches_persisted(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HERMES_FETCH_STORE_HOME", str(tmp_path))
    monkeypatch.setenv(computer_runtime.TARGET_ENV, "tcp://127.0.0.1:5900")
    monkeypatch.delenv(computer_runtime.LEGACY_TARGET_ENV, raising=False)
    (tmp_path / ".env").write_text(
        f'export {computer_runtime.TARGET_ENV}="tcp://127.0.0.1:5900"\n', encoding="utf-8"
    )
    calls = []
    monkeypatch.setattr(
        computer_runtime, "ensure_computer_runtime", lambda: calls.append(1) or "already-running"
    )

    assert computer_runtime.keeper_ensure_computer_runtime() == "already-running"
    assert calls == [1]


def test_keeper_ensure_passes_through_unconfigured_hosts(tmp_path, monkeypatch) -> None:
    # No target anywhere: equality holds and the plain ensure handles it
    # (returning "disabled"), so relay-only hosts keep their keeper coverage.
    monkeypatch.setenv("HERMES_FETCH_STORE_HOME", str(tmp_path))
    monkeypatch.delenv(computer_runtime.TARGET_ENV, raising=False)
    monkeypatch.delenv(computer_runtime.LEGACY_TARGET_ENV, raising=False)
    monkeypatch.setattr(computer_runtime, "ensure_computer_runtime", lambda: "disabled")

    assert computer_runtime.keeper_ensure_computer_runtime() == "disabled"
