"""Hermes Inbox platform plugin.

The platform's ``send`` operation persists a message into Hermes' canonical
session database and emits an iOS proactive push tied to that session. This
lets cron jobs and webhook direct-delivery routes target the Fetch iOS app.
The visible platform moved to ``fetch``; this plugin keeps the old
``hermes_inbox`` target available only when explicitly opted in.
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

logger = logging.getLogger(__name__)

_relay_module = None


def _load_relay():
    """Return the co-installed Fetch plugin's relay client (loaded once, cached).

    Hermes loads each plugin as ``hermes_plugins.<slug>`` with the plugin dir on
    the package ``__path__`` but NOT on ``sys.path``, so a bare ``import`` of a
    sibling module won't resolve. Load the fetch plugin's ``_relay.py`` by file
    path instead — the same pattern ``fetch/__init__.py`` uses — so this works
    no matter how this module was imported (runtime half or dashboard half).
    """
    global _relay_module
    if _relay_module is not None:
        return _relay_module
    here = Path(__file__).resolve().parent
    candidates = [
        here.parent / "fetch" / "_relay.py",         # installed: sibling plugin dir
        here.parent / "fetch-plugin" / "_relay.py",  # dev tree: server/fetch-plugin
    ]
    relay_path = next((p for p in candidates if p.exists()), None)
    if relay_path is None:
        raise ModuleNotFoundError(
            "Fetch plugin _relay.py not found alongside hermes-inbox "
            f"(looked in {[str(c) for c in candidates]}); is the `fetch` plugin installed?"
        )
    spec = importlib.util.spec_from_file_location("fetch_inbox_relay", relay_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # register before exec so @dataclass annotations resolve
    spec.loader.exec_module(module)
    _relay_module = module
    return _relay_module

PLATFORM_NAME = "hermes_inbox"
HOME_CHANNEL_ENV = "HERMES_INBOX_HOME_CHANNEL"
ENABLED_ENV = "HERMES_INBOX_ENABLED"
LEGACY_PLATFORM_ENV = "HERMES_INBOX_REGISTER_LEGACY_PLATFORM"
DEFAULT_CHANNEL = "default"
DEFAULT_TITLE = "Fetch Inbox"
CHANNEL_LABEL = "Fetch"  # friendly name the agent sees for this channel in its target list
# When set, inbox sessions are persisted into THIS home's state.db instead of
# the running process's HERMES_HOME. The Fetch app pairs with ONE home over the
# relay; a delivery that runs under a worker profile (`hermes -p researcher`)
# would otherwise write to the researcher's profile db, invisible to Fetch.
# Point this at the relay-paired home so every per-agent channel
# (`inbox_researcher`, `inbox_coder`, …) lands in the one store the phone reads.
STORE_HOME_ENV = "HERMES_INBOX_STORE_HOME"


@dataclass(frozen=True)
class InboxDelivery:
    session_id: str
    message_id: int


class HermesInboxAdapter(BasePlatformAdapter):
    """Gateway adapter that routes outbound sends to Hermes session history."""

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform(PLATFORM_NAME))

    async def connect(self) -> bool:
        self._mark_connected()
        return True

    async def disconnect(self) -> None:
        self._mark_disconnected()

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        channel = _channel_from_chat_id(chat_id)
        title = _title_from_metadata(metadata) or DEFAULT_TITLE
        try:
            delivery = deliver_to_inbox(channel=channel, content=str(content or ""), title=title)
        except Exception as exc:
            logger.exception("Hermes Inbox delivery failed")
            return SendResult(success=False, error=str(exc))
        return SendResult(success=True, message_id=str(delivery.message_id))


def check_requirements() -> bool:
    return True


def validate_config(config) -> bool:
    return _is_enabled() and bool(_home_channel())


def _env_enablement() -> dict[str, Any] | None:
    if not _is_enabled():
        return None
    channel = _home_channel()
    if not channel:
        return None
    return {
        "home_channel": {"chat_id": channel, "name": DEFAULT_TITLE},
        "channel": channel,
    }


async def _standalone_send(
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
    channel = _channel_from_chat_id(chat_id)
    delivery = deliver_to_inbox(channel=channel, content=str(message or ""), title=title)
    return {"success": True, "message_id": str(delivery.message_id), "session_id": delivery.session_id}


def deliver_to_inbox(*, channel: str, content: str, title: str = DEFAULT_TITLE) -> InboxDelivery:
    """Persist one inbox message and notify iOS devices.

    ``channel`` maps to a stable Hermes session, so repeated lead alerts land
    in the same app thread unless callers choose a different channel. Per-agent
    channels (`hermes_inbox:researcher`) produce per-agent sessions
    (`inbox_researcher`) so each agent gets its own Fetch DM instead of one
    pooled ``inbox_default`` thread.
    """
    clean_channel = _normalize_channel(channel)
    session_id = _session_id_for_channel(clean_channel)
    body = content.strip()
    if not body:
        raise ValueError("Hermes Inbox cannot deliver an empty message")

    db = SessionDB(db_path=_store_home() / "state.db")
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


def _notify_proactive(*, session_id: str, title: str, body: str) -> None:
    """Fire an iOS proactive push for an inbox message via the Fetch relay.

    ``send_event_background`` is already fire-and-forget on a daemon thread and
    de-duped, so there is no loop/await dance to manage here.

    The relay schema caps ``title`` at 120 chars and ``body`` at 500 chars.
    Clamp here before submission so long cron/webhook content does not fail
    relay validation.
    """
    try:
        _load_relay().send_event_background(
            kind="proactive",
            session_id=session_id,
            title=(title or "")[:120],
            body=(body or "")[:500],
            # The inbox session is created with source="inbox"; stamp it on the
            # push so the device routes it into the phone-owned inbox without a
            # source lookup or a Hermes-specific denylist.
            source="inbox",
        )
    except Exception:
        logger.debug("Hermes Inbox proactive push failed", exc_info=True)


def _set_title_if_possible(db: SessionDB, session_id: str, title: str) -> None:
    clean_title = " ".join((title or DEFAULT_TITLE).split())[:80] or DEFAULT_TITLE
    try:
        db.set_session_title(session_id, clean_title)
    except ValueError:
        fallback = f"{clean_title} {session_id[-8:]}"[:100]
        try:
            db.set_session_title(session_id, fallback)
        except Exception:
            logger.debug("Hermes Inbox could not set session title", exc_info=True)


def _title_from_metadata(metadata: Any) -> str | None:
    if isinstance(metadata, dict):
        raw = metadata.get("title") or metadata.get("thread_id")
        if raw:
            return str(raw)
    return None


def _home_channel() -> str:
    return os.environ.get(HOME_CHANNEL_ENV, "").strip() or DEFAULT_CHANNEL


def _store_home() -> Path:
    """Resolve which Hermes home's state.db inbox sessions persist into.

    Defaults to the running process's HERMES_HOME (the legacy behavior). When
    ``HERMES_INBOX_STORE_HOME`` is set, deliveries persist into THAT home's db
    instead — so a delivery run under a worker profile still lands in the
    relay-paired home the Fetch app reads. Without this, `hermes -p researcher
    … --deliver hermes_inbox:researcher` writes to researcher's profile db and
    is invisible to the phone.
    """
    override = os.environ.get(STORE_HOME_ENV, "").strip()
    if override:
        return Path(os.path.expanduser(override))
    return get_hermes_home()


def _channel_from_chat_id(chat_id) -> str:
    """Normalize a gateway delivery target into the inbox channel slug.

    The gateway normally splits a `platform:chat_id` target and passes just the
    chat_id half to `send` (so `hermes_inbox:researcher` → chat_id="researcher").
    This also defends against the full `hermes_inbox:researcher` string arriving
    unsplit, by stripping a leading `hermes_inbox:` prefix. Bare `hermes_inbox`
    or an empty value falls back to the home channel.
    """
    raw = str(chat_id or "").strip()
    if not raw or raw == PLATFORM_NAME:
        return DEFAULT_CHANNEL
    prefix = f"{PLATFORM_NAME}:"
    if raw.startswith(prefix):
        raw = raw[len(prefix):].strip()
    return raw or DEFAULT_CHANNEL


def _is_enabled() -> bool:
    value = os.environ.get(ENABLED_ENV, "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _legacy_platform_enabled() -> bool:
    value = os.environ.get(LEGACY_PLATFORM_ENV, "").strip().lower()
    return value in {"1", "true", "yes", "on"}


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


def hermes_home_env_path() -> str:
    return str(get_hermes_home() / ".env")


def _seed_channel_alias() -> None:
    """Advertise the inbox to the agent as a named, addressable channel.

    Hermes' channel directory skips any platform with no discovered channels
    (``format_directory_for_display``), and a send-only platform never discovers
    one from inbound traffic — so without help the agent never *sees* Fetch in
    ``send_message(action="list")`` and won't proactively message it. Registering
    the home channel in ``channel_aliases.json`` makes Hermes inject it as a named
    target even before the first message arrives.

    Also seeds one alias per Hermes profile (e.g. ``researcher``, ``coder``) so
    each agent is addressable as its own DM target — ``hermes_inbox:researcher``
    → "Researcher". This is what lets a cron job deliver to a specific agent's
    DM (`--deliver hermes_inbox:researcher`) and the agent proactively pick a DM
    (`send_message(target="hermes_inbox:researcher")`).

    Idempotent and non-destructive: add a channel only when absent; never
    overwrite a name the user changed, nor other platforms' aliases.

    An auto-generated alias is one whose value is still ``CHANNEL_LABEL`` — a
    user rename gives it a different value. When the configured home channel
    changes, prune stale auto-generated ``Fetch`` aliases pointing at other
    channels so friendly-name lookup can't keep resolving ``Fetch`` to a stale
    channel; user-renamed aliases are left untouched. Per-agent aliases carry
    their own labels (Title-Cased profile name), so they're never pruned as
    stale ``Fetch`` aliases.
    """
    try:
        path = get_hermes_home() / "channel_aliases.json"
        aliases: dict[str, Any] = {}
        if path.exists():
            with open(path, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if not isinstance(loaded, dict):
                logger.debug("Hermes Inbox alias seed skipped: channel_aliases top-level is not a dict")
                return
            aliases = loaded
        entries = aliases.get(PLATFORM_NAME)
        if entries is None:
            entries = {}
        elif not isinstance(entries, dict):
            logger.debug("Hermes Inbox alias seed skipped: platform aliases entry is not a dict")
            return
        channel = _home_channel()
        # Drop stale auto-generated Fetch aliases for any other channel; keep
        # user-renamed ones (value != CHANNEL_LABEL) and the current channel.
        pruned = {
            key: value
            for key, value in entries.items()
            if key == channel or value != CHANNEL_LABEL
        }
        if channel not in pruned:
            pruned[channel] = CHANNEL_LABEL  # respect an existing user rename above
        # One alias per profile dir → per-agent DM target. The slug IS the
        # profile name; the label is Title-Cased for display. setdefault keeps
        # this non-destructive: a user-renamed profile alias is preserved.
        for slug, label in _profile_channels():
            pruned.setdefault(slug, label)
        if pruned == entries:
            return  # nothing changed
        aliases[PLATFORM_NAME] = pruned
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(aliases, fh, indent=2)
        os.replace(tmp, path)
    except Exception:
        logger.debug("Hermes Inbox channel alias seeding failed", exc_info=True)


def _profile_channels() -> list[tuple[str, str]]:
    """(slug, label) for each profile under the store home, for per-agent DM
    alias seeding. Slug = profile dir name; label = Title-Cased name. Empty list
    when no profiles dir exists or it isn't readable."""
    try:
        profiles_dir = _store_home() / "profiles"
        if not profiles_dir.is_dir():
            return []
        out: list[tuple[str, str]] = []
        for entry in sorted(profiles_dir.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            out.append((entry.name, _profile_label(entry.name)))
        return out
    except Exception:
        logger.debug("Hermes Inbox profile channel enumeration failed", exc_info=True)
        return []


def _profile_label(slug: str) -> str:
    """Human-readable label for a profile slug: Title Case the name."""
    return slug.replace("_", " ").replace("-", " ").title()


def register(ctx):
    if _is_enabled() and _legacy_platform_enabled():
        _seed_channel_alias()
    if not _legacy_platform_enabled():
        logger.info(
            "Fetch Inbox legacy platform hidden; use the unified `fetch` platform "
            "or set %s=1 to expose `hermes_inbox`.",
            LEGACY_PLATFORM_ENV,
        )
        return
    ctx.register_platform(
        name=PLATFORM_NAME,
        label=DEFAULT_TITLE,
        adapter_factory=lambda cfg: HermesInboxAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        required_env=[],
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var=HOME_CHANNEL_ENV,
        standalone_sender_fn=_standalone_send,
        max_message_length=8000,
        platform_hint="Fetch Inbox delivers messages into the Fetch iOS app.",
        emoji="📱",
    )
