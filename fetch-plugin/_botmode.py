"""Profile-scoped Bot Chat identity and the Fetch companion protocol.

The title is the registry. Never store a session-id pin or flatten several
profiles into the paired owner's database (Hermes titles are unique per DB).
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
import tempfile
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

import yaml

BOT_CHAT_TITLE = "Bot Chat"
_START = "<!-- fetch-bot-protocol:start -->"
_END = "<!-- fetch-bot-protocol:end -->"
_PROTOCOL = f"""{_START}
## Messaging other agents

Your private conversation with the user is the ongoing **Bot Chat** for this
Hermes profile. Keep that title. Compact it instead of creating a new chat.
Use the backend's `message_agent` tool when available to ask a teammate to work;
the backend owns teammate attribution, wake-up, and replies.
On older Hermes, use `hermes -p <teammate> chat --in ~ -c "Bot Chat"
--create-if-missing -Q --query-file <file>` via the terminal tool with
background=true and notify_on_complete=true. Write the message to the file
first, prefixed with `Message from 🤖 (@<your-profile>):`. Discover profile
names with `hermes profile list`. Do not interpolate message text into a shell
command. Send once, finish your turn, and wait for completion.
To deliver a visible update to a teammate's Fetch conversation, use
`send_message(target="fetch:<teammate>", message="...")`. This appends to that
profile's Bot Chat; it does not wake the teammate. Reply normally to the user
in your current chat. Groups are separate conversations, never a bot's home.
{_END}"""


def profile_home(store_home: Path, name: str) -> Path:
    """Resolve from the paired tree, independent of the sending worker's home."""
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", name):
        raise ValueError("name must be a Hermes profile slug")
    store = store_home.expanduser().resolve()
    root = store.parent.parent if store.parent.name == "profiles" else store
    home = root if name == "default" else root / "profiles" / name
    if not home.is_dir() or home.resolve() != home:
        raise ValueError(f"Unknown or non-local Hermes profile: {name}")
    return home


def _invalidate_session_source_cache(*session_ids: str) -> None:
    """Best-effort cache drop when Bot Chat provenance is restamped in SQLite."""
    ids = [session_id for session_id in session_ids if session_id]
    if not ids:
        return
    for module_name in ("hermes_plugins.fetch", "fetch_plugin"):
        module = sys.modules.get(module_name)
        if module is None:
            continue
        invalidate = getattr(module, "invalidate_session_source_cache", None)
        if callable(invalidate):
            invalidate(*ids)
            return
        cache = getattr(module, "_SESSION_SOURCE_CACHE", None)
        if isinstance(cache, dict):
            for session_id in ids:
                cache.pop(session_id, None)
            return


@contextmanager
def _transaction(path: Path):
    # SQLite locking is cross-process and works on Windows as well as Unix.
    with closing(sqlite3.connect(path, timeout=30)) as conn:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            yield conn


def canonical_session(db_path: Path, profile: str, db=None) -> str:
    """Claim/reuse the exact title atomically after SessionDB initializes schema.

    SQLite's write transaction also serializes against gateway/CLI creators.
    Only the canonical row is attested; old inbox and untitled rows stay intact.
    Hermes has no public source setter, so this small schema-compatible write
    owns the Fetch provenance stamp. Transcript writes still use SessionDB.
    """
    with _transaction(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
        row = conn.execute("SELECT id FROM sessions WHERE title = ?", (BOT_CHAT_TITLE,)).fetchone()
        if row:
            session_id = row[0]
        else:
            session_id = f"fetch_bot_{uuid.uuid4().hex}"
            values = {"id": session_id, "source": "fetch", "title": BOT_CHAT_TITLE,
                      "user_id": profile, "started_at": time.time()}
            if "profile_name" in columns:
                values["profile_name"] = profile
            conn.execute(
                f"INSERT INTO sessions ({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
                tuple(values.values()),
            )
        conn.execute("UPDATE sessions SET source = 'fetch' WHERE id = ?", (session_id,))
        if "hidden" in columns:
            conn.execute("UPDATE sessions SET hidden = 1 WHERE id = ?", (session_id,))
    # Official Hermes keeps the exact title on the registry root after a
    # compression fork. Deliver to its live compression tip, never a delegate
    # or a newer unrelated chat. Older runtimes without this API keep the root.
    stamped = {session_id}
    get_tip = getattr(db, "get_compression_tip", None)
    tip = get_tip(session_id) if get_tip is not None else None
    delivery_id = session_id
    if tip and tip != session_id:
        with _transaction(db_path) as conn:
            conn.execute("UPDATE sessions SET source = 'fetch' WHERE id = ?", (tip,))
            if "hidden" in columns:
                conn.execute("UPDATE sessions SET hidden = 1 WHERE id = ?", (tip,))
        delivery_id = tip
        stamped.add(tip)
    _invalidate_session_source_cache(*stamped)
    return delivery_id


def _read_protocol_assets(home: Path) -> tuple[str, dict]:
    soul_path, metadata_path = home / "SOUL.md", home / "profile.yaml"
    if soul_path.is_symlink() or metadata_path.is_symlink():
        raise ValueError("Bot protocol assets must be local files")
    soul = soul_path.read_text() if soul_path.exists() else ""
    metadata = yaml.safe_load(metadata_path.read_text()) if metadata_path.exists() else {}
    metadata = metadata or {}
    if not isinstance(metadata, dict) or not isinstance(metadata.get("ui_meta", {}), dict):
        raise ValueError("profile.yaml must contain a mapping with ui_meta mapping")
    if (_START in soul) != (_END in soul):
        raise ValueError("Incomplete Fetch protocol markers in SOUL.md")
    return soul, metadata


def _atomic_write(path: Path, text: str) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(text)
        if path.exists():
            os.chmod(temporary, path.stat().st_mode & 0o777)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def ensure_protocol(store_home: Path, *, name: str | None, all_profiles: bool, db_factory) -> dict:
    if (name is not None) == all_profiles:
        raise ValueError("Supply either name or all=true")
    root = store_home.resolve()
    if root.parent.name == "profiles":
        root = root.parent.parent
    names = [name] if name is not None else ["default"]
    if all_profiles and (root / "profiles").is_dir():
        names += sorted(entry.name for entry in (root / "profiles").iterdir()
                        if entry.is_dir() and not entry.is_symlink() and not entry.name.startswith("."))
    homes = [(slug, profile_home(store_home, slug)) for slug in names]
    prepared = [(slug, home, *_read_protocol_assets(home)) for slug, home in homes]
    results = []
    for slug, home, soul, metadata in prepared:
        with _transaction(home / ".fetch-bot-protocol.lock"):
            soul_path, metadata_path = home / "SOUL.md", home / "profile.yaml"
            updated = re.sub(re.escape(_START) + r".*?" + re.escape(_END), lambda _: _PROTOCOL,
                             soul, flags=re.DOTALL) if _START in soul else soul.rstrip() + "\n\n" + _PROTOCOL + "\n"
            if updated != soul:
                _atomic_write(soul_path, updated)
            ui_meta = metadata.setdefault("ui_meta", {})
            if "hermes-bots" not in ui_meta:
                ui_meta["hermes-bots"] = {}
                _atomic_write(metadata_path, yaml.safe_dump(metadata, sort_keys=False))
            db_path = home / "state.db"
            db = db_factory(db_path=db_path)
            try:
                session_id = canonical_session(db_path, slug, db)
            finally:
                db.close()
            results.append({"name": slug, "session_id": session_id, "title": BOT_CHAT_TITLE, "source": "fetch"})
    return {"ok": True, "bots": results}
