"""Fetch — Hermes Agent plugin (runtime half).

Makes Fetch a first-class Hermes channel with NO core patches and NO Apple
credentials on this host:

  * **Platform registration** — registers ``fetch`` as a gateway platform so it
    appears in ``hermes setup`` next to Telegram/Discord, with a relay pairing
    flow (``setup_fn``) that prints a setup link + QR for the iOS app. Same
    plugin extension point Discord uses (``ctx.register_platform``).
  * **Push hooks** — two agent hooks notify the app like a messaging app:
      - ``post_llm_call``        — a turn finished anywhere → "Fetch replied".
      - ``pre_approval_request`` — agent waiting on approval/question/secret →
                                   "needs attention".
  * **Reverse tunnel** — a persistent outbound WebSocket to the relay so the app
    can reach this NAT'd agent with no inbound port / no Tailscale. Fetch setup
    enables it and starts a headless relay runtime; manual hosts can still gate
    it with ``HERMES_FETCH_TUNNEL_ENABLED``.

Each push hook fires-and-forgets an HTTPS POST to the Fetch relay, which holds
the single APNs key and fans out to registered devices. Device-token
registration is handled by the dashboard half (``dashboard/plugin_api.py``).

The platform's app control path is the reverse tunnel above, selected by the
app's relay pairing, not a gateway inbound adapter. The same ``fetch`` platform
also owns send-only inbox delivery so Hermes setup, cron delivery, and
``send_message`` expose one user-facing Fetch entry instead of a separate
``Fetch Inbox`` platform.

This file is imported by the Hermes ``PluginManager`` as ``hermes_plugins.fetch``
and its ``register(ctx)`` is called once at agent startup.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import sys
import threading
from pathlib import Path

log = logging.getLogger("fetch_plugin")


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _is_kanban_worker() -> bool:
    """True when this process is a dispatcher-spawned kanban worker (FET-5).

    Workers run `hermes chat --accept-hooks` with HERMES_KANBAN_TASK set and
    report results on the task itself — their turn completions and approval
    prompts are automation, not someone messaging the user, so they must never
    raise a Fetch push (the recipient model: a thread appears only because
    something messaged you)."""
    return bool(os.environ.get("HERMES_KANBAN_TASK"))


# Channels that are conversations with the user *through the Fetch app*. A reply
# in one of these is a Fetch conversation the phone should show in its inbox.
# Empty string = an untagged gateway chat (the Fetch app's dashboard-WS sessions
# carry no channel tag). ``inbox`` = a hermes-inbox proactive delivery. Other
# channels (``telegram``, ``discord``, ``cli``, ``tui``, ``cron`` for a cron
# *run*, …) are not Fetch conversations, so their replies don't push to the
# phone — the user is already on that surface, or it's an automated run.
#
# This is the app's own channel identity, not a Hermes job-type denylist: it says
# "is this one of MY channels" rather than enumerating Hermes internals to
# exclude. It can't rot when Hermes adds a new background job type, because a
# new job type is simply not in this set.
FETCH_CHANNELS: frozenset[str] = frozenset({"", "fetch", "ios", "mobile", "inbox"})


def _session_source(session_id: str | None) -> str | None:
    """Look up a session's ``source`` channel from the agent's state.db.

    Returns None when the session can't be found (e.g. a profile-scoped db the
    launch home doesn't see, or a brand-new session whose row hasn't persisted
    yet). Callers treat None as "unknown — fall back to current behavior" so a
    lookup miss never accidentally suppresses a Fetch notification.
    """
    if not session_id:
        return None
    try:
        from hermes_state import SessionDB
        db = SessionDB()
        try:
            row = db.get_session(session_id)
        finally:
            db.close()
    except Exception:
        log.debug("Fetch plugin source lookup failed", exc_info=True)
        return None
    if not row:
        return None
    source = row.get("source")
    return source if isinstance(source, str) else None


def _platform_from_session_key(session_key: str) -> str | None:
    """Extract the platform segment from a gateway session key.

    ``agent:main:telegram:private:123456789`` -> ``telegram``. An empty platform
    segment (``agent:main::private:…``) is an untagged gateway chat = a Fetch
    conversation, and returns ``""``. Returns None for empty/unexpected shapes
    so the device treats it as unknown.
    """
    parts = (session_key or "").split(":")
    if len(parts) >= 3 and parts[0] == "agent":
        return parts[2]
    return None


def _load_sibling(module_name: str, filename: str):
    """Load a sibling module by file path (no plugin-namespace dependency).

    The dashboard half loads the same modules the same way from its own process;
    the two never share a Python import, only the relay and the on-disk
    credentials file.
    """
    path = Path(__file__).resolve().parent / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register in sys.modules BEFORE exec so a module's @dataclass annotations
    # resolve (they use `from __future__ import annotations`).
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_relay = _load_sibling("fetch_plugin_relay", "_relay.py")
_pairing = _load_sibling("fetch_plugin_pairing", "_pairing.py")
_runtime = _load_sibling("fetch_plugin_runtime", "_runtime.py")
_inbox = _load_sibling("fetch_plugin_inbox", "_inbox.py")


def _on_post_llm_call(*, session_id: str = "", assistant_response: str = "", **_kwargs) -> None:
    """A turn completed (final_response present, not interrupted)."""
    if _is_kanban_worker():
        return  # FET-5: dispatched workers report on the task, never push.
    # Only push for Fetch-channel conversations. A reply on Telegram/Discord/CLI
    # is not a Fetch conversation — the user is on that surface already — so it
    # must not ring the phone or create a Fetch inbox thread. If the source
    # can't be resolved, fall back to pushing (preserves prior behavior) rather
    # than risk suppressing a real Fetch notification on a lookup miss.
    source = _session_source(session_id or None)
    if source is not None and source not in FETCH_CHANNELS:
        return
    body = (assistant_response or "").strip() or "Finished working."
    _relay.send_event_background(
        kind="replies", session_id=session_id or None, title="Fetch replied",
        body=body, source=source,
    )


def _on_pre_approval_request(
    *, command: str = "", description: str = "", session_key: str = "", **_kwargs
) -> None:
    """The agent is blocking on an approval / question / secret."""
    if _is_kanban_worker():
        return  # FET-5: a worker's approval prompt is automation, not a message.
    detail = (description or command or "").strip() or "Open Fetch to continue."
    # Approvals always notify (the agent needs the user regardless of surface).
    # Carry the channel parsed from the session_key so the device can later
    # decide inbox membership; session_key shape is ``agent:<profile>:<platform>:<type>:<id>``.
    source = _platform_from_session_key(session_key)
    _relay.send_event_background(
        kind="attention", session_id=session_key or None,
        title="Fetch needs your attention", body=detail, source=source,
    )


def _fetch_is_connected(config) -> bool:
    return bool(_pairing.is_pairing_configured() or _inbox.validate_config(config))


def _fetch_env_enablement():
    return _inbox.env_enablement(force=_pairing.is_pairing_configured())


def _spawn_tunnel() -> None:
    """Start the agent-side reverse-tunnel client on a daemon thread, so the
    phone can reach this NAT'd agent with no inbound port. Gated behind
    HERMES_FETCH_TUNNEL_ENABLED (default off) and spawn-and-return so it never
    blocks plugin load. The tunnel module + its `websockets` dep are imported
    lazily here, so a host without them is unaffected unless the flag is set."""
    if not _truthy(os.environ.get("HERMES_FETCH_TUNNEL_ENABLED")):
        return
    if _runtime.ensure_relay_runtime() in {"started", "already-running"}:
        return

    def _run() -> None:
        try:
            tunnel = _load_sibling("fetch_plugin_tunnel", "_tunnel.py")

            async def _boot() -> None:
                creds = await _relay.relay_client()._credentials()
                client = tunnel.AgentTunnel(
                    relay_url=creds.relay_url,
                    agent_id=creds.agent_id,
                    agent_secret=creds.agent_secret,
                    dashboard_token=os.environ.get("HERMES_DASHBOARD_SESSION_TOKEN"),
                )
                await client.run_forever()

            asyncio.run(_boot())
        except Exception:
            log.warning("Fetch reverse-tunnel client failed to start", exc_info=True)

    threading.Thread(target=_run, daemon=True, name="fetch-tunnel").start()
    log.info("Fetch reverse-tunnel client starting (HERMES_FETCH_TUNNEL_ENABLED set)")


def register(ctx) -> None:
    # Push hooks: notify the app on turn completion / attention-needed.
    ctx.register_hook("post_llm_call", _on_post_llm_call)
    ctx.register_hook("pre_approval_request", _on_pre_approval_request)

    # Platform registration: surface Fetch in `hermes setup` with a pairing
    # flow. Guarded so an older Hermes without the platform API still loads the
    # push hooks above.
    register_platform = getattr(ctx, "register_platform", None)
    if callable(register_platform):
        try:
            register_platform(
                name="fetch",
                label="Fetch",
                adapter_factory=_inbox.adapter_factory,
                check_fn=_inbox.check_requirements,
                validate_config=_inbox.validate_config,
                is_connected=_fetch_is_connected,
                setup_fn=_pairing.interactive_setup,
                env_enablement_fn=_fetch_env_enablement,
                cron_deliver_env_var=_inbox.HOME_CHANNEL_ENV,
                standalone_sender_fn=_inbox.standalone_send,
                max_message_length=8000,
                platform_hint="Fetch delivers messages into the Fetch iOS app.",
                emoji="📱",
                install_hint="",
            )
            if _inbox.is_delivery_enabled():
                _inbox.seed_channel_alias()
            log.info("Fetch platform registered (pairing setup_fn + inbox delivery)")
        except Exception:
            log.warning("Fetch platform registration failed; push hooks still active", exc_info=True)
    else:
        log.info("Hermes build has no register_platform; Fetch running as push-only")

    # Reverse tunnel: hold an outbound channel to the relay (gated, default off).
    _spawn_tunnel()

    log.info("Fetch plugin registered (push hooks + pairing + reverse tunnel)")
