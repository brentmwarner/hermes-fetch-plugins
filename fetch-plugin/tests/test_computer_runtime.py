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


def _fake_runtime(tmp_path):
    return SimpleNamespace(
        _runtime_dir=lambda: tmp_path / "run",
        _hermes_home=lambda: tmp_path,
        _child_pythonpath=lambda: "/tmp/hermes",
        _child_python_executable=lambda: "/tmp/hermes/bin/python",
        _process_alive=lambda pid: True,
        _terminate_process=lambda pid: True,
    )


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
