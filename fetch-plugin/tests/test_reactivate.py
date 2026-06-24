"""The /tasks/{id}/reactivate route forces a worker respawn for a task the user
explicitly replied to, bypassing the dispatcher's recent-success / open-PR guard
(FET-15) — without patching Hermes core.

The fetch-plugin suite runs without the agent installed (conftest stubs
``hermes_cli``), so these inject a fake ``hermes_cli.kanban_db`` and assert the
endpoint's guard-bypass wiring, retry, and HTTP behavior against it — no real
dispatcher or worker subprocess.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_p = Path(__file__).resolve().parent.parent / "dashboard" / "plugin_api.py"
_spec = importlib.util.spec_from_file_location("fetch_plugin_api_reactivate_test", _p)
api = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = api
_spec.loader.exec_module(api)


def _install_fake_kanban(monkeypatch, *, task, dispatch):
    """Install a fake ``hermes_cli.kanban_db`` the endpoint will import.

    ``task`` is the object ``get_task`` returns (or None). ``dispatch`` is the
    ``dispatch_once`` implementation — it runs with the guard already swapped,
    so it can read ``fake.check_respawn_guard`` to prove the bypass.
    """
    fake = types.ModuleType("hermes_cli.kanban_db")

    def check_respawn_guard(conn, tid):
        return "recent_success"  # the real auto-guard would block a respawn

    fake.check_respawn_guard = check_respawn_guard
    fake.connect = lambda board=None: types.SimpleNamespace(close=lambda: None)
    fake.get_task = lambda conn, tid: task
    fake.dispatch_once = dispatch

    hermes_cli = sys.modules.get("hermes_cli") or types.ModuleType("hermes_cli")
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setattr(hermes_cli, "kanban_db", fake, raising=False)
    monkeypatch.setitem(sys.modules, "hermes_cli.kanban_db", fake)
    return fake


def _client():
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


def _task(status="ready", assignee="coder", tid="t_1"):
    return types.SimpleNamespace(id=tid, status=status, assignee=assignee)


def test_reactivate_bypasses_guard_and_reports_spawn(monkeypatch):
    tid = "t_1"
    seen = {}

    def dispatch(conn, *, max_spawn=None, board=None, **kw):
        # _force_dispatch must have neutralised the guard for THIS task.
        seen["guard"] = sys.modules["hermes_cli.kanban_db"].check_respawn_guard(conn, tid)
        seen["other"] = sys.modules["hermes_cli.kanban_db"].check_respawn_guard(conn, "t_other")
        return types.SimpleNamespace(spawned=[(tid, "coder", "/tmp/ws")], skipped_locked=False)

    fake = _install_fake_kanban(monkeypatch, task=_task(tid=tid), dispatch=dispatch)

    res = _client().post(f"/tasks/{tid}/reactivate")
    assert res.status_code == 200, res.text
    assert res.json() == {"ok": True, "spawned": True}
    assert seen["guard"] is None              # bypassed for our task…
    assert seen["other"] == "recent_success"  # …but not for anyone else
    # Guard restored after the tick.
    assert fake.check_respawn_guard(object(), tid) == "recent_success"


def test_reactivate_retries_while_tick_lock_held(monkeypatch):
    tid = "t_1"
    calls = {"n": 0}

    def dispatch(conn, *, max_spawn=None, board=None, **kw):
        calls["n"] += 1
        if calls["n"] < 3:
            return types.SimpleNamespace(spawned=[], skipped_locked=True)
        return types.SimpleNamespace(spawned=[(tid, "coder", "/tmp/ws")], skipped_locked=False)

    _install_fake_kanban(monkeypatch, task=_task(tid=tid), dispatch=dispatch)
    res = _client().post(f"/tasks/{tid}/reactivate")
    assert res.status_code == 200
    assert res.json()["spawned"] is True
    assert calls["n"] == 3


def test_reactivate_counts_concurrent_claim_as_spawn(monkeypatch):
    """If our tick didn't spawn it but it's now running (another tick claimed
    it), that's success, not failure."""
    tid = "t_1"
    task = _task(tid=tid)

    def dispatch(conn, *, max_spawn=None, board=None, **kw):
        task.status = "running"  # simulate a concurrent claim
        return types.SimpleNamespace(spawned=[], skipped_locked=False)

    _install_fake_kanban(monkeypatch, task=task, dispatch=dispatch)
    res = _client().post(f"/tasks/{tid}/reactivate")
    assert res.json()["spawned"] is True


def test_reactivate_404_for_unknown_task(monkeypatch):
    _install_fake_kanban(monkeypatch, task=None, dispatch=lambda *a, **k: None)
    assert _client().post("/tasks/t_nope/reactivate").status_code == 404


def test_reactivate_running_is_noop(monkeypatch):
    _install_fake_kanban(monkeypatch, task=_task(status="running"), dispatch=lambda *a, **k: None)
    res = _client().post("/tasks/t_1/reactivate")
    assert res.status_code == 200
    assert res.json() == {"ok": True, "spawned": False, "reason": "already_running"}


def test_reactivate_409_when_not_ready(monkeypatch):
    _install_fake_kanban(monkeypatch, task=_task(status="done"), dispatch=lambda *a, **k: None)
    assert _client().post("/tasks/t_1/reactivate").status_code == 409


def test_reactivate_409_when_unassigned(monkeypatch):
    _install_fake_kanban(monkeypatch, task=_task(assignee=""), dispatch=lambda *a, **k: None)
    assert _client().post("/tasks/t_1/reactivate").status_code == 409
