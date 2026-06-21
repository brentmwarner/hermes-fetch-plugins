"""Fetch inbox delivery helpers.

Fetch is the visible Hermes platform. This module gives that platform the
send-only inbox behavior that used to live behind a separate ``hermes_inbox``
setup entry: persist a message into Hermes' session database, then send a
proactive Fetch push for the created thread.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from hermes_cli.config import get_hermes_home
from hermes_state import SessionDB

logger = logging.getLogger("fetch_plugin.inbox")

PLATFORM_NAME = "fetch"
LEGACY_PLATFORM_NAME = "hermes_inbox"
HOME_CHANNEL_ENV = "HERMES_INBOX_HOME_CHANNEL"
ENABLED_ENV = "HERMES_INBOX_ENABLED"
DEFAULT_CHANNEL = "default"
DEFAULT_TITLE = "Fetch"
CHANNEL_LABEL = "Fetch"

_relay_module = None


@dataclass(frozen=True)
class InboxDelivery:
    session_id: str
    message_id: int


class FetchInboxAdapter(BasePlatformAdapter):
    """Gateway adapter that routes outbound Fetch sends to Hermes sessions."""

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform(PLATFORM_NAME))

    async def connect(self) -> bool:
        self._mark_connected()
        return True

    async def disconnect(self) -> None:
        self._mark_disconnected()

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        channel = str(chat_id or DEFAULT_CHANNEL)
        title = _title_from_metadata(metadata) or DEFAULT_TITLE
        try:
            delivery = deliver_to_inbox(channel=channel, content=str(content or ""), title=title)
        except Exception as exc:
            logger.exception("Fetch inbox delivery failed")
            return SendResult(success=False, error=str(exc))
        return SendResult(success=True, message_id=str(delivery.message_id))


def check_requirements() -> bool:
    return True


def adapter_factory(config: PlatformConfig) -> FetchInboxAdapter:
    return FetchInboxAdapter(config)


def validate_config(config) -> bool:
    return bool(_configured_home_channel(config))


def is_delivery_enabled() -> bool:
    return _truthy(os.environ.get(ENABLED_ENV))


def env_enablement(*, force: bool = False) -> dict[str, Any] | None:
    if not force and not is_delivery_enabled():
        return None
    channel = _home_channel()
    return {
        "home_channel": {"chat_id": channel, "name": DEFAULT_TITLE},
        "channel": channel,
    }


def enable_delivery_for_future_starts() -> None:
    """Persist Fetch inbox delivery defaults for future Hermes processes."""
    os.environ[ENABLED_ENV] = "1"
    channel = _home_channel()
    os.environ[HOME_CHANNEL_ENV] = channel
    try:
        from hermes_cli.config import save_env_value

        save_env_value(ENABLED_ENV, "1")
        save_env_value(HOME_CHANNEL_ENV, channel)
    except Exception:
        logger.debug("Could not persist Fetch inbox delivery env", exc_info=True)
    seed_channel_alias()


async def standalone_send(
    pconfig,
    chat_id,
    message,
    *,
    thread_id=None,
    media_files=None,
    force_document=False,
) -> dict:
    title = DEFAULT_TITLE
    if thread_id:
        title = f"{DEFAULT_TITLE}: {thread_id}"
    delivery = deliver_to_inbox(channel=str(chat_id or DEFAULT_CHANNEL), content=str(message or ""), title=title)
    return {"success": True, "message_id": str(delivery.message_id), "session_id": delivery.session_id}


def deliver_to_inbox(*, channel: str, content: str, title: str = DEFAULT_TITLE) -> InboxDelivery:
    """Persist one Fetch inbox message and notify iOS devices."""
    clean_channel = _normalize_channel(channel)
    session_id = _session_id_for_channel(clean_channel)
    body = content.strip()
    if not body:
        raise ValueError("Fetch cannot deliver an empty message")

    db = SessionDB()
    try:
        db.create_session(session_id=session_id, source="inbox", user_id=clean_channel)
        db.reopen_session(session_id)
        _set_title_if_possible(db, session_id, title)
        message_id = db.append_message(
            session_id=session_id,
            role="assistant",
            content=body,
            platform_message_id=f"{PLATFORM_NAME}:{message_fingerprint(clean_channel, body)}",
            observed=True,
        )
    finally:
        db.close()

    _notify_proactive(session_id=session_id, title=title, body=body)
    return InboxDelivery(session_id=session_id, message_id=int(message_id or 0))


def message_fingerprint(channel: str, body: str) -> str:
    digest = hashlib.sha256(f"{channel}\0{body}".encode("utf-8")).hexdigest()
    return digest[:24]


def seed_channel_alias() -> None:
    """Advertise Fetch as a named, addressable send target.

    The visible platform is now ``fetch``. Any old auto-generated
    ``hermes_inbox`` alias named exactly ``Fetch`` is pruned so the agent does
    not see both targets after an upgrade; user-renamed legacy aliases are left
    untouched.
    """
    try:
        path = get_hermes_home() / "channel_aliases.json"
        aliases: dict[str, Any] = {}
        if path.exists():
            with open(path, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if not isinstance(loaded, dict):
                logger.debug("Fetch alias seed skipped: channel_aliases top-level is not a dict")
                return
            aliases = loaded

        entries = aliases.get(PLATFORM_NAME)
        if entries is None:
            entries = {}
        elif not isinstance(entries, dict):
            logger.debug("Fetch alias seed skipped: fetch aliases entry is not a dict")
            return

        channel = _home_channel()
        pruned = {
            key: value
            for key, value in entries.items()
            if key == channel or value != CHANNEL_LABEL
        }
        already_current = channel in pruned
        if not already_current:
            pruned[channel] = CHANNEL_LABEL
        aliases[PLATFORM_NAME] = pruned

        legacy = aliases.get(LEGACY_PLATFORM_NAME)
        if isinstance(legacy, dict) and not _legacy_platform_enabled():
            legacy_pruned = {
                key: value
                for key, value in legacy.items()
                if value != CHANNEL_LABEL
            }
            if legacy_pruned:
                aliases[LEGACY_PLATFORM_NAME] = legacy_pruned
            else:
                aliases.pop(LEGACY_PLATFORM_NAME, None)

        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(aliases, fh, indent=2)
        os.replace(tmp, path)
    except Exception:
        logger.debug("Fetch channel alias seeding failed", exc_info=True)


def _load_relay():
    global _relay_module
    if _relay_module is not None:
        return _relay_module
    path = Path(__file__).resolve().parent / "_relay.py"
    spec = importlib.util.spec_from_file_location("fetch_inbox_relay", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _relay_module = module
    return _relay_module


def _notify_proactive(*, session_id: str, title: str, body: str) -> None:
    try:
        _load_relay().send_event_background(
            kind="proactive",
            session_id=session_id,
            title=(title or "")[:120],
            body=(body or "")[:500],
        )
    except Exception:
        logger.debug("Fetch proactive push failed", exc_info=True)


def _set_title_if_possible(db: SessionDB, session_id: str, title: str) -> None:
    clean_title = " ".join((title or DEFAULT_TITLE).split())[:80] or DEFAULT_TITLE
    try:
        db.set_session_title(session_id, clean_title)
    except ValueError:
        fallback = f"{clean_title} {session_id[-8:]}"[:100]
        try:
            db.set_session_title(session_id, fallback)
        except Exception:
            logger.debug("Fetch could not set session title", exc_info=True)


def _title_from_metadata(metadata: Any) -> str | None:
    if isinstance(metadata, dict):
        raw = metadata.get("title") or metadata.get("thread_id")
        if raw:
            return str(raw)
    return None


def _configured_home_channel(config) -> str:
    home = getattr(config, "home_channel", None)
    chat_id = str(getattr(home, "chat_id", "") or "").strip()
    if chat_id:
        return chat_id
    extra = getattr(config, "extra", None)
    if isinstance(extra, dict):
        channel = str(extra.get("channel") or "").strip()
        if channel:
            return channel
    return _home_channel() if is_delivery_enabled() else ""


def _home_channel() -> str:
    return os.environ.get(HOME_CHANNEL_ENV, "").strip() or DEFAULT_CHANNEL


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _legacy_platform_enabled() -> bool:
    return _truthy(os.environ.get("HERMES_INBOX_REGISTER_LEGACY_PLATFORM"))


def _normalize_channel(channel: str) -> str:
    clean = (channel or DEFAULT_CHANNEL).strip()
    return clean or DEFAULT_CHANNEL


def _session_id_for_channel(channel: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", channel).strip("-_.").lower()
    if not slug:
        slug = DEFAULT_CHANNEL
    if len(slug) > 48:
        digest = hashlib.sha1(channel.encode("utf-8")).hexdigest()[:12]
        slug = f"{slug[:35]}-{digest}"
    return f"inbox_{slug}"
