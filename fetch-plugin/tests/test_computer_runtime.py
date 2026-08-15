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
        terminated=terminated,
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
    monkeypatch.delenv(computer_runtime.AUTOSTART_ENV, raising=False)
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


def test_ensure_does_not_start_before_pairing(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(computer_runtime.TARGET_ENV, "tcp://127.0.0.1:5901")
    monkeypatch.delenv(computer_runtime.AUTOSTART_ENV, raising=False)
    monkeypatch.setattr(computer_runtime, "_credentials_path", lambda: tmp_path / "missing.json")

    assert computer_runtime.ensure_computer_runtime() == "unpaired"


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
