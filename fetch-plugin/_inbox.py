"""Fetch inbox delivery helpers.

Fetch is the single visible Hermes platform. This module gives that platform its
send-only delivery behavior: persist a message into Hermes' session database,
then send a proactive Fetch push for the created thread.

``inbox`` survives only as an internal wire tag — the session ``source`` column
value and the ``inbox_<slug>`` session-id prefix the iOS app keys its inbox off.
The user never sees it: the platform, the delivery target, and every env var are
``fetch``.
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
HOME_CHANNEL_ENV = "HERMES_FETCH_HOME_CHANNEL"
ENABLED_ENV = "HERMES_FETCH_DELIVERY_ENABLED"
# Legacy env var names — read as fallback so an upgrade doesn't silently
# disable delivery until the user re-runs setup (which writes the new names).
_LEGACY_HOME_CHANNEL_ENV = "HERMES_INBOX_HOME_CHANNEL"
_LEGACY_ENABLED_ENV = "HERMES_INBOX_ENABLED"
_LEGACY_STORE_HOME_ENV = "HERMES_INBOX_STORE_HOME"
DEFAULT_CHANNEL = "default"
DEFAULT_TITLE = "Fetch"
CHANNEL_LABEL = "Fetch"
# When set, delivery sessions are persisted into THIS home's state.db instead of
# the running process's HERMES_HOME. The Fetch app pairs with ONE home over the
# relay; a delivery that runs under a worker profile (`hermes -p researcher`)
# would otherwise write to the researcher's profile db, invisible to Fetch.
STORE_HOME_ENV = "HERMES_FETCH_STORE_HOME"

_relay_module = None


@dataclass(frozen=True)
class InboxDelivery:
    session_id: str
    message_id: int


@dataclass(frozen=True)
class CronDeliveryInfo:
    name: str
    job_id: str


_CRON_RESPONSE_RE = re.compile(
    r"\ACronjob Response:\s*(?P<name>[^\n]+)\n\(job_id:\s*(?P<job_id>[^)]+)\)",
    re.IGNORECASE,
)

# Process-level cache mapping home-channel → last resolved cron channel.
# When Hermes chunks a long cron response, only the first chunk starts with
# "Cronjob Response... (job_id: ...)"; later chunks lack the header and would
# otherwise fall back to the home channel, splitting one run between
# inbox_cron-* and inbox_default. This cache survives long enough to carry
# the resolved channel across the remaining chunks in the same delivery.
_cron_channel_cache: dict[str, str] = {}


class FetchInboxAdapter(BasePlatformAdapter):
    """Gateway adapter that routes outbound Fetch sends to Hermes sessions."""

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform(PLATFORM_NAME))

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        self._mark_connected()
        return True

    async def disconnect(self) -> None:
        self._mark_disconnected()

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        channel = _channel_from_chat_id(chat_id)
        thread_id = _thread_id_from_metadata(metadata)
        title = _title_from_metadata(metadata) or _default_title_for_delivery(
            channel=channel,
            content=str(content or ""),
            thread_id=thread_id,
        )
        try:
            delivery = deliver_to_inbox(
                channel=channel,
                content=str(content or ""),
                title=title,
                thread_id=thread_id,
            )
        except Exception as exc:
            logger.exception("Fetch inbox delivery failed")
            return SendResult(success=False, error=str(exc))
        return SendResult(success=True, message_id=str(delivery.message_id))

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        channel = _channel_from_chat_id(chat_id)
        name = DEFAULT_TITLE if _is_home_channel(channel) else _label_for_channel(channel)
        return {"name": name, "type": "dm"}


def check_requirements() -> bool:
    return True


def adapter_factory(config: PlatformConfig) -> FetchInboxAdapter:
    return FetchInboxAdapter(config)


def validate_config(config) -> bool:
    return bool(_configured_home_channel(config))


def is_delivery_enabled() -> bool:
    return _truthy(os.environ.get(ENABLED_ENV) or os.environ.get(_LEGACY_ENABLED_ENV))


def env_enablement(*, force: bool = False) -> dict[str, Any] | None:
    if not force and not is_delivery_enabled():
        return None
    channel = _home_channel()
    return {
        "home_channel": {"chat_id": channel, "name": DEFAULT_TITLE},
        "channel": channel,
    }


def enable_delivery_for_future_starts() -> None:
    """Persist Fetch delivery defaults for future Hermes processes (pairing)."""
    set_delivery_enabled(True, channel=_home_channel())


def set_delivery_enabled(enabled: bool, *, channel: str | None = None) -> None:
    """Persist the Fetch delivery on/off flag (and optional home channel) for
    future Hermes processes, seeding the channel alias when enabling.

    Single source of truth for both Fetch pairing (always enables) and the
    dashboard ``/inbox/enable`` endpoint (explicit toggle). The env vars are
    internal: setup and the dashboard write them; users target ``fetch``.
    """
    flag = "1" if enabled else "0"
    os.environ[ENABLED_ENV] = flag
    if channel is not None:
        os.environ[HOME_CHANNEL_ENV] = channel.strip() or DEFAULT_CHANNEL
    try:
        from hermes_cli.config import save_env_value

        save_env_value(ENABLED_ENV, flag)
        if channel is not None:
            save_env_value(HOME_CHANNEL_ENV, os.environ[HOME_CHANNEL_ENV])
    except Exception:
        logger.debug("Could not persist Fetch delivery env", exc_info=True)
    if enabled:
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
    channel = _channel_from_chat_id(chat_id)
    # Pre-resolve the delivery channel so chunked cron responses stay in the
    # same thread even when later chunks lack the "Cronjob Response..." header.
    resolved_channel = _delivery_channel(
        channel=channel,
        content=str(message or ""),
        thread_id=thread_id,
    )
    if not thread_id and _is_home_channel(channel):
        if _cron_channel_from_content(str(message or "")):
            # First chunk of a cron response: cache the resolved channel so
            # subsequent chunks (which lack the header) route to the same thread.
            _cron_channel_cache[channel] = resolved_channel
        elif channel in _cron_channel_cache:
            # Subsequent chunk: reuse the cached cron channel.
            resolved_channel = _cron_channel_cache[channel]
    title = _default_title_for_delivery(
        channel=channel,
        content=str(message or ""),
        thread_id=thread_id,
    )
    delivery = deliver_to_inbox(
        channel=resolved_channel,
        content=str(message or ""),
        title=title,
        thread_id=thread_id,
    )
    return {"success": True, "message_id": str(delivery.message_id), "session_id": delivery.session_id}


def deliver_to_inbox(
    *,
    channel: str,
    content: str,
    title: str = DEFAULT_TITLE,
    thread_id: str | None = None,
) -> InboxDelivery:
    """Persist one Fetch inbox message and notify iOS devices.

    ``channel`` maps to a stable Hermes session, so repeated deliveries to the
    same channel land in the same app thread. Per-agent channels
    (``fetch:researcher``) produce per-agent sessions (``inbox_researcher``) so
    each agent gets its own Fetch DM instead of one pooled ``inbox_default``
    thread.
    """
    clean_channel = _delivery_channel(channel=channel, content=content, thread_id=thread_id)
    clean_title = _title_for_channel(clean_channel, content=content, proposed=title)
    session_id = _session_id_for_channel(clean_channel)
    body = content.strip()
    if not body:
        raise ValueError("Fetch cannot deliver an empty message")

    db = SessionDB(db_path=_store_home() / "state.db")
    try:
        db.create_session(session_id=session_id, source="inbox", user_id=clean_channel)
        db.reopen_session(session_id)
        _set_title_if_possible(db, session_id, clean_title)
        message_id = db.append_message(
            session_id=session_id,
            role="assistant",
            content=body,
            platform_message_id=f"{PLATFORM_NAME}:{message_fingerprint(clean_channel, body)}",
            observed=True,
        )
    finally:
        db.close()

    _notify_proactive(session_id=session_id, title=clean_title, body=body)
    return InboxDelivery(session_id=session_id, message_id=int(message_id or 0))


def message_fingerprint(channel: str, body: str) -> str:
    digest = hashlib.sha256(f"{channel}\0{body}".encode("utf-8")).hexdigest()
    return digest[:24]


def seed_channel_alias() -> None:
    """Advertise Fetch as a named, addressable send target.

    Manages only the ``fetch`` key in ``channel_aliases.json`` — every other
    platform's aliases are left untouched. Stale auto-generated ``fetch`` aliases
    (value still equal to ``CHANNEL_LABEL``) for a previous home channel are
    pruned when the home channel changes; user-renamed aliases are preserved.
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
        # One alias per profile dir → per-agent DM target. The slug IS the
        # profile name; the label is Title-Cased for display. setdefault keeps
        # this non-destructive: a user-renamed profile alias is preserved.
        profile_channels = _profile_channels()
        for slug, label in profile_channels:
            pruned.setdefault(slug, label)
        aliases[PLATFORM_NAME] = pruned

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
            # Stamp source="inbox" so the device routes the push into the
            # phone-owned inbox (it's in the app's inboxSources allowlist).
            # Without this the iOS push gate skips the push and the thread
            # only appears via the session-list refresh, not the push.
            source="inbox",
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
        raw = metadata.get("title")
        if raw:
            return str(raw)
    return None


def _thread_id_from_metadata(metadata: Any) -> str | None:
    if isinstance(metadata, dict):
        raw = metadata.get("thread_id")
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
    return (os.environ.get(HOME_CHANNEL_ENV, "").strip()
            or os.environ.get(_LEGACY_HOME_CHANNEL_ENV, "").strip()
            or DEFAULT_CHANNEL)


def _is_home_channel(channel: str) -> bool:
    return _normalize_channel(channel) == _normalize_channel(_home_channel())


def _store_home() -> Path:
    """Resolve which Hermes home's state.db delivery sessions persist into.

    Defaults to the running process's HERMES_HOME. When
    ``HERMES_FETCH_STORE_HOME`` is set, deliveries persist into THAT home's db
    instead — so a delivery run under a worker profile still lands in the
    relay-paired home the Fetch app reads.
    """
    override = os.environ.get(STORE_HOME_ENV, "").strip()
    if override:
        return Path(os.path.expanduser(override))
    return get_hermes_home()


def _channel_from_chat_id(chat_id) -> str:
    """Normalize a gateway delivery target into the channel slug.

    The gateway normally splits a `platform:chat_id` target and passes just the
    chat_id half to `send` (so `fetch:researcher` -> chat_id="researcher"). This
    also defends against the full `fetch:researcher` string arriving unsplit, by
    stripping a leading `fetch:` prefix. Bare `fetch` or an empty value falls
    back to the configured home channel (`HERMES_FETCH_HOME_CHANNEL`), matching
    what `env_enablement()` advertises.
    """
    raw = str(chat_id or "").strip()
    if not raw or raw == PLATFORM_NAME:
        return _home_channel()
    prefix = f"{PLATFORM_NAME}:"
    if raw.startswith(prefix):
        raw = raw[len(prefix):].strip()
    return raw or _home_channel()


def _strip_platform_prefix(channel: str) -> str:
    """Strip a leading `fetch:` platform prefix from direct calls.

    Direct callers passing `fetch:researcher` must land in `inbox_researcher`
    instead of creating a platform-prefixed duplicate thread.
    """
    raw = str(channel or "").strip()
    prefix = f"{PLATFORM_NAME}:"
    if raw.startswith(prefix):
        return raw[len(prefix):].strip() or _home_channel()
    return raw


def _delivery_channel(*, channel: str, content: str, thread_id: str | None = None) -> str:
    """Resolve the stable Fetch thread key for one outbound delivery.

    Explicit channels are preserved (`fetch:researcher` stays `researcher`).
    Bare home-channel cron deliveries get split by cron job id, otherwise every
    scheduled job collapses into the shared `inbox_default` home thread.
    """
    clean_channel = _normalize_channel(_strip_platform_prefix(channel))
    if thread_id:
        return _thread_channel(clean_channel, thread_id)
    if _is_home_channel(clean_channel):
        cron_channel = _cron_channel_from_content(content)
        if cron_channel:
            return cron_channel
    return clean_channel


def _thread_channel(channel: str, thread_id: str) -> str:
    clean_thread = _slug_for_channel(thread_id)
    if not clean_thread:
        return channel
    if _is_home_channel(channel):
        return f"thread-{clean_thread}"
    return f"{channel}-{clean_thread}"


def _cron_channel_from_content(content: str) -> str | None:
    info = _cron_delivery_info(content)
    if info is None:
        return None
    slug = _slug_for_channel(info.job_id or info.name)
    return f"cron-{slug}" if slug else None


def _cron_delivery_info(content: str) -> CronDeliveryInfo | None:
    match = _CRON_RESPONSE_RE.match(content.strip())
    if match is None:
        return None
    name = " ".join(match.group("name").split())
    job_id = " ".join(match.group("job_id").split())
    if not name and not job_id:
        return None
    return CronDeliveryInfo(name=name or job_id, job_id=job_id)


def _default_title_for_delivery(*, channel: str, content: str, thread_id: str | None = None) -> str:
    if thread_id:
        return _label_for_channel(thread_id)
    info = _cron_delivery_info(content)
    if _is_home_channel(channel) and info is not None:
        return info.name
    return _label_for_channel(channel)


def _title_for_channel(channel: str, *, content: str, proposed: str) -> str:
    info = _cron_delivery_info(content)
    if channel.startswith("cron-") and info is not None:
        home_titles = {DEFAULT_TITLE, _label_for_channel(DEFAULT_CHANNEL), _label_for_channel(_home_channel())}
        if not proposed or proposed in home_titles:
            return info.name
    return proposed or _label_for_channel(channel)


def _label_for_channel(channel: str) -> str:
    """Human-readable thread title for a channel slug: Title Case the name.

    ``researcher`` -> ``Researcher``, ``code_reviewer`` -> ``Code Reviewer``.
    The home channel (``default``) keeps the app label ``Fetch`` so the pooled
    inbox thread is recognizable rather than titled ``Default``.
    """
    clean = _normalize_channel(channel)
    if clean == DEFAULT_CHANNEL:
        return DEFAULT_TITLE
    return clean.replace("_", " ").replace("-", " ").title()


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _normalize_channel(channel: str) -> str:
    clean = (channel or DEFAULT_CHANNEL).strip()
    return clean or DEFAULT_CHANNEL


def _slug_for_channel(channel: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(channel or "")).strip("-_.").lower()
    if len(slug) > 48:
        digest = hashlib.sha1(str(channel).encode("utf-8")).hexdigest()[:12]
        slug = f"{slug[:35]}-{digest}"
    return slug


def _session_id_for_channel(channel: str) -> str:
    slug = _slug_for_channel(channel)
    if not slug:
        slug = DEFAULT_CHANNEL
    return f"inbox_{slug}"


def _profile_channels() -> list[tuple[str, str]]:
    """(slug, label) for each profile under the store home, for per-agent DM
    alias seeding. Slug = profile dir name; label = Title-Cased name. Empty
    list when no profiles dir exists or it isn't readable."""
    try:
        profiles_dir = _store_home() / "profiles"
        if not profiles_dir.is_dir():
            return []
        out: list[tuple[str, str]] = []
        for entry in sorted(profiles_dir.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            out.append((entry.name, entry.name.replace("_", " ").replace("-", " ").title()))
        return out
    except Exception:
        logger.debug("Fetch profile channel enumeration failed", exc_info=True)
        return []
