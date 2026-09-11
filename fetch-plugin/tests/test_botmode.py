"""Persistent Bot Chat contract, tested against SQLite rather than call spies."""
import asyncio
import importlib.util
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml

from test_inbox import _load_inbox

_spec = importlib.util.spec_from_file_location("botmode_test", Path(__file__).parents[1] / "_botmode.py")
botmode = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(botmode)


class SQLiteSessionDB:
    """Minimal Hermes schema/API fixture; routing uses real transactions."""
    def __init__(self, *, db_path):
        self.conn = sqlite3.connect(db_path, timeout=30)
        self.conn.executescript('''
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, source TEXT NOT NULL, title TEXT UNIQUE,
                user_id TEXT, started_at REAL NOT NULL, hidden INTEGER DEFAULT 0,
                profile_name TEXT, ended_at REAL, end_reason TEXT);
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT);
        ''')

    def create_session(self, *, session_id, source, user_id):
        self.conn.execute("INSERT OR IGNORE INTO sessions(id, source, user_id, started_at) VALUES (?, ?, ?, 0)",
                          (session_id, source, user_id))
        self.conn.commit()

    def reopen_session(self, sid):
        self.conn.execute("UPDATE sessions SET ended_at=NULL, end_reason=NULL WHERE id=?", (sid,))
        self.conn.commit()

    def set_session_title(self, sid, title):
        self.conn.execute("UPDATE sessions SET title=? WHERE id=?", (title, sid))
        self.conn.commit()

    def append_message(self, *, session_id, role, content, **kwargs):
        cur = self.conn.execute("INSERT INTO messages(session_id, role, content) VALUES (?, ?, ?)",
                                (session_id, role, content))
        self.conn.commit()
        return cur.lastrowid

    def close(self):
        self.conn.close()


@pytest.fixture
def delivery(tmp_path, monkeypatch):
    for slug in ("researcher", "writer"):
        (tmp_path / "profiles" / slug).mkdir(parents=True)
    inbox = _load_inbox()
    monkeypatch.setattr(inbox, "_store_home", lambda: tmp_path)
    monkeypatch.setattr(inbox, "SessionDB", SQLiteSessionDB)
    pushes = []
    monkeypatch.setattr(inbox, "_load_relay", lambda: type("Relay", (), {
        "send_event_background": staticmethod(lambda **kw: pushes.append(kw))}))
    return inbox, tmp_path, pushes


def rows(home, table="sessions"):
    with sqlite3.connect(home / "state.db") as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]


def test_existing_bot_chat_wins_over_newer_fetch_and_old_inbox(delivery):
    inbox, root, pushes = delivery
    home = root / "profiles/researcher"
    db = SQLiteSessionDB(db_path=home / "state.db")
    for sid, source, title in [("canonical", "cli", "Bot Chat"), ("newer", "fetch", "Other"),
                               ("inbox_researcher", "inbox", "Researcher")]:
        db.create_session(session_id=sid, source=source, user_id="researcher")
        db.set_session_title(sid, title)
    db.close()
    result = asyncio.run(inbox.standalone_send(None, "fetch:researcher", "DM"))
    assert result["session_id"] == "canonical"
    assert rows(home, "messages")[0]["session_id"] == "canonical"
    by_id = {row["id"]: row for row in rows(home)}
    assert by_id["canonical"]["source"] == "fetch"
    assert by_id["canonical"]["hidden"] == 1
    assert by_id["inbox_researcher"]["source"] == "inbox"
    assert pushes[0]["source"] == "fetch"
    assert pushes[0]["data"] == {"agent_id": "researcher"}


def test_profile_deliveries_share_title_but_not_database(delivery, monkeypatch):
    inbox, root, pushes = delivery
    monkeypatch.setenv("HERMES_HOME", str(root / "profiles/writer"))
    monkeypatch.setenv("HERMES_PROFILE", "writer")
    ids = []
    for slug in ("default", "researcher", "writer"):
        ids.append(inbox.deliver_to_inbox(channel=slug, content="hello").session_id)
        home = botmode.profile_home(root, slug)
        assert [(row["title"], row["source"]) for row in rows(home)] == [("Bot Chat", "fetch")]
    assert len(set(ids)) == 3
    assert [push["data"]["agent_id"] for push in pushes] == ["default", "researcher", "writer"]


def test_concurrent_first_deliveries_create_one_bot_chat(delivery):
    inbox, root, _ = delivery
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda n: inbox.deliver_to_inbox(channel="researcher", content=f"DM {n}"), range(16)))
    assert len({result.session_id for result in results}) == 1
    assert len(rows(root / "profiles/researcher")) == 1
    assert len(rows(root / "profiles/researcher", "messages")) == 16


@pytest.mark.parametrize("home_channel", ["default", "researcher"])
def test_named_cron_chunks_stay_in_bot_chat(delivery, monkeypatch, home_channel):
    inbox, root, _ = delivery
    monkeypatch.setenv("HERMES_FETCH_HOME_CHANNEL", home_channel)
    first = asyncio.run(inbox.standalone_send(None, "researcher", "Cronjob Response: Brief\n(job_id: abc)\n\nfirst"))
    second = asyncio.run(inbox.standalone_send(None, "researcher", "second"))
    assert first["session_id"] == second["session_id"]
    assert rows(root / "profiles/researcher")[0]["title"] == "Bot Chat"


def test_automation_threads_stay_in_inbox(delivery):
    inbox, root, pushes = delivery
    result = inbox.deliver_to_inbox(channel="default", content="report", cron_job_id="abc")
    assert result.session_id == "inbox_cron-abc"
    assert rows(root)[0]["source"] == "inbox"
    assert pushes[0]["source"] == "inbox"


def test_protocol_is_idempotent_and_preserves_identity(delivery):
    inbox, root, _ = delivery
    home = root / "profiles/researcher"
    (home / "SOUL.md").write_text("# Researcher\nUser-authored personality.\n")
    (home / "profile.yaml").write_text("model: custom\nui_meta:\n  other: untouched\n")
    first = botmode.ensure_protocol(root, name="researcher", all_profiles=False, db_factory=SQLiteSessionDB)
    contents = [(home / filename).read_text() for filename in ("SOUL.md", "profile.yaml")]
    second = botmode.ensure_protocol(root, name="researcher", all_profiles=False, db_factory=SQLiteSessionDB)
    assert first == second
    assert contents == [(home / filename).read_text() for filename in ("SOUL.md", "profile.yaml")]
    assert contents[0].startswith("# Researcher\nUser-authored personality.")
    assert contents[0].count(botmode._START) == 1
    meta = yaml.safe_load(contents[1])
    assert meta == {"model": "custom", "ui_meta": {"other": "untouched", "hermes-bots": {}}}
    assert inbox.deliver_to_inbox(channel="researcher", content="hi").session_id == first["bots"][0]["session_id"]
    assert not (home / "sessions/bot-chat.title").exists()


def test_protocol_all_and_validation(delivery):
    _, root, _ = delivery
    for name, all_profiles in [(None, False), ("researcher", True), ("../escape", False), ("missing", False)]:
        with pytest.raises(ValueError):
            botmode.ensure_protocol(root, name=name, all_profiles=all_profiles, db_factory=SQLiteSessionDB)
    assert not (root / "SOUL.md").exists()
    result = botmode.ensure_protocol(root, name=None, all_profiles=True, db_factory=SQLiteSessionDB)
    assert [bot["name"] for bot in result["bots"]] == ["default", "researcher", "writer"]


def test_protocol_rejects_symlink_profile(delivery):
    _, root, _ = delivery
    (root / "profiles/alias").symlink_to(root / "profiles/researcher", target_is_directory=True)
    with pytest.raises(ValueError):
        botmode.profile_home(root, "alias")


def test_gateway_cron_metadata_delivers_to_profile_chat(delivery, monkeypatch):
    inbox, root, _ = delivery
    monkeypatch.setattr(inbox, "SendResult", lambda **kw: kw)
    adapter = object.__new__(inbox.FetchInboxAdapter)
    assert asyncio.run(adapter.send("researcher", "report", metadata={"job_id": "abc"}))["success"]
    assert rows(root / "profiles/researcher")[0]["title"] == "Bot Chat"
    assert not (root / "state.db").exists()


def test_store_home_override_routes_worker_to_paired_profile(delivery, monkeypatch):
    inbox, root, _ = delivery
    monkeypatch.setenv("HERMES_FETCH_STORE_HOME", str(root))
    monkeypatch.setenv("HERMES_PROFILE", "writer")
    monkeypatch.setenv("HERMES_HOME", str(root / "profiles/writer"))
    monkeypatch.setattr(inbox, "_store_home", lambda: inbox._load_owner().delivery_home())
    inbox.deliver_to_inbox(channel="researcher", content="hi")
    assert rows(root / "profiles/researcher")[0]["source"] == "fetch"
    assert not (root / "profiles/writer/state.db").exists()


def test_pairing_persists_store_home(delivery, monkeypatch):
    inbox, root, _ = delivery
    saved = {}
    monkeypatch.setenv("HERMES_FETCH_STORE_HOME", "")
    monkeypatch.setenv("HERMES_FETCH_DELIVERY_ENABLED", "")
    monkeypatch.setenv("HERMES_FETCH_HOME_CHANNEL", "default")
    monkeypatch.setattr(inbox, "seed_channel_alias", lambda: None)
    import hermes_cli.config as config
    monkeypatch.setattr(config, "save_env_value", lambda key, value: saved.update({key: value}), raising=False)
    inbox.enable_delivery_for_future_starts()
    assert saved["HERMES_FETCH_STORE_HOME"] == str(root)
    assert saved["HERMES_FETCH_DELIVERY_ENABLED"] == "1"


def test_ensure_protocol_api_validates_and_creates_chat(delivery, monkeypatch):
    from test_plugin_api import api, _client, _FakeClient
    inbox, root, _ = delivery
    monkeypatch.setattr(api, "_load_inbox", lambda: inbox)
    client = _client(_FakeClient())
    assert client.get("/bots/ensure-protocol").status_code == 405
    for payload in ({}, {"name": "../outside"}, {"name": "missing"}, {"name": "writer", "all": True}):
        assert client.post("/bots/ensure-protocol", json=payload).status_code == 400
    response = client.post("/bots/ensure-protocol", json={"name": "researcher"})
    assert response.status_code == 200
    assert response.json()["bots"][0]["title"] == "Bot Chat"
    assert client.post("/bots/ensure-protocol", json={"all": True}).status_code == 200


def test_compression_tip_receives_delivery_without_moving_title(delivery, monkeypatch):
    inbox, root, pushes = delivery
    home = root / "profiles/researcher"
    db = SQLiteSessionDB(db_path=home / "state.db")
    for sid in ("root", "tip", "delegate"):
        db.create_session(session_id=sid, source="cli", user_id="researcher")
    db.set_session_title("root", "Bot Chat")
    db.close()
    monkeypatch.setattr(SQLiteSessionDB, "get_compression_tip", lambda self, sid: "tip" if sid == "root" else sid, raising=False)
    result = inbox.deliver_to_inbox(channel="researcher", content="after compaction")
    assert result.session_id == "tip"
    assert rows(home, "messages")[0]["session_id"] == "tip"
    by_id = {row["id"]: row for row in rows(home)}
    assert by_id["root"]["title"] == "Bot Chat"
    assert by_id["tip"]["source"] == "fetch"
    assert by_id["delegate"]["source"] == "cli"
    assert pushes[0]["session_id"] == "tip"
    assert pushes[0]["title"] == "Bot Chat"
