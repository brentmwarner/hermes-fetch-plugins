"""Profile-scoped Bot Chat identity for Fetch deliveries.

The exact title is the registry and Hermes core owns the Bot Mode protocol
(prompt section, ``message_agent`` tool, wire format). This module only finds
or mints the one "Bot Chat" row of a profile; it never rewrites the ``source``
or ``hidden`` flags of a row another client (Hermes Desktop, the TUI, the app)
created, and it never stores a session-id pin or picks the newest row.
"""
from __future__ import annotations

import re
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path
from typing import NamedTuple

BOT_CHAT_TITLE = "Bot Chat"
MINTED_SOURCE = "fetch"


class CanonicalSession(NamedTuple):
    """The profile's Bot Chat as resolved for one delivery."""

    session_id: str  # live delivery target: the compression tip when known
    root_id: str  # exact-title registry row
    source: str  # the registry row's actual source ("" when unset)
    minted: bool  # True when this call created the row


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


@contextmanager
def _transaction(path: Path):
    # SQLite locking is cross-process and works on Windows as well as Unix.
    with closing(sqlite3.connect(path, timeout=30)) as conn:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            yield conn


def resolve_bot_chat(db_path: Path, profile: str, db=None) -> CanonicalSession:
    """Adopt the exact-title row atomically after SessionDB initializes schema.

    SQLite's write transaction serializes against gateway/CLI/Desktop creators,
    so concurrent first deliveries mint exactly one row. An adopted row is left
    untouched; only a freshly minted row is ``source=fetch`` and hidden.
    Transcript writes still go through SessionDB.
    """
    with _transaction(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
        row = conn.execute(
            "SELECT id, source FROM sessions WHERE title = ?", (BOT_CHAT_TITLE,)
        ).fetchone()
        if row:
            root_id, source, minted = row[0], row[1] or "", False
        else:
            root_id, source, minted = f"fetch_bot_{uuid.uuid4().hex}", MINTED_SOURCE, True
            values = {"id": root_id, "source": MINTED_SOURCE, "title": BOT_CHAT_TITLE,
                      "user_id": profile, "started_at": time.time()}
            if "profile_name" in columns:
                values["profile_name"] = profile
            if "hidden" in columns:
                values["hidden"] = 1
            conn.execute(
                f"INSERT INTO sessions ({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
                tuple(values.values()),
            )
    # Official Hermes keeps the exact title on the registry root after a
    # compression fork. Deliver to its live compression tip, never a delegate
    # or a newer unrelated chat. Older runtimes without this API keep the root.
    get_tip = getattr(db, "get_compression_tip", None)
    tip = get_tip(root_id) if get_tip is not None else None
    return CanonicalSession(
        session_id=tip or root_id, root_id=root_id, source=source, minted=minted,
    )


def canonical_session(db_path: Path, profile: str, db=None) -> str:
    """Delivery target id for the profile's Bot Chat (see ``resolve_bot_chat``)."""
    return resolve_bot_chat(db_path, profile, db).session_id
