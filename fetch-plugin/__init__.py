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
  * **Generative-UI platform hint** — the ``platform_hint`` registered with
    the platform teaches the agent (via the cached system prompt, Fetch
    sessions only) to emit ``card`` fences the iOS app renders natively as
    ``GenerativeCardView``. No per-turn hook, no core patches — uses the same
    per-surface guidance mechanism Telegram/WhatsApp/Slack use. The
    ``fetch-cards`` skill holds richer patterns (dashboard, carousel, brief).
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
import shutil
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
# carry no channel tag). ``inbox`` = a Fetch proactive/cron delivery (the
# internal source tag the iOS inbox keys off). Other
# channels (``telegram``, ``discord``, ``cli``, ``tui``, ``cron`` for a cron
# *run*, …) are not Fetch conversations, so their replies don't push to the
# phone — the user is already on that surface, or it's an automated run.
#
# This is the app's own channel identity, not a Hermes job-type denylist: it says
# "is this one of MY channels" rather than enumerating Hermes internals to
# exclude. It can't rot when Hermes adds a new background job type, because a
# new job type is simply not in this set.
#
# FETCH_APP_SOURCES is the durable per-session ``source`` tag the Fetch app
# stamps on its own chats; FETCH_CHANNELS is that same set plus the empty-string
# untagged dashboard-WS chat. Deriving one from the other keeps the two from
# drifting when a new Fetch source is added.
FETCH_APP_SOURCES: frozenset[str] = frozenset({"fetch", "fetch-ios", "ios", "mobile", "inbox"})
FETCH_CHANNELS: frozenset[str] = FETCH_APP_SOURCES | {""}

_FETCH_IOS_TURN_CONTEXT = """[Fetch iOS client context - do not quote or mention this block.
Client: Fetch iOS app.
Output surface: native mobile chat, not a terminal, shell, TUI, browser, or file artifact.
Fetch supports standard Markdown plus fenced `card` JSON blocks rendered as native UI.
For charts, graphs, reports, dashboards, token usage, rankings, trends, metrics, tables, or other structured visual summaries, emit a fenced ```card JSON payload using Fetch native generative UI.
Do not use ASCII/text bar charts, Unicode block charts, SVG links, inline SVG, HTML artifacts, Mermaid diagrams, image links, or external chart files unless the user explicitly asks for those formats.
For token/usage reports, prefer a card with `stats` plus `chart` or `blocks` containing a native chart.
]"""


# A session's ``source`` is immutable once persisted, but both the pre_llm_call
# and post_llm_call hooks resolve it on every turn. Memoize resolved sources so
# a long conversation doesn't reopen state.db on each turn. Only successfully
# resolved (string) sources are cached; a None lookup miss is left uncached so a
# not-yet-persisted session is re-queried until its row appears.
_SESSION_SOURCE_CACHE: dict[str, str] = {}


def _session_source(session_id: str | None) -> str | None:
    """Look up a session's ``source`` channel from the agent's state.db.

    Returns None when the session can't be found (e.g. a profile-scoped db the
    launch home doesn't see, or a brand-new session whose row hasn't persisted
    yet). Callers treat None as "unknown — fall back to current behavior" so a
    lookup miss never accidentally suppresses a Fetch notification.
    """
    if not session_id:
        return None
    cached = _SESSION_SOURCE_CACHE.get(session_id)
    if cached is not None:
        return cached
    db = None
    try:
        from hermes_state import SessionDB

        db = SessionDB()
        row = db.get_session(session_id)
    except Exception:
        log.debug("Fetch plugin source lookup failed", exc_info=True)
        return None
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                log.debug("Fetch plugin could not close SessionDB", exc_info=True)
    if not row:
        return None
    source = row.get("source")
    if not isinstance(source, str):
        return None
    _SESSION_SOURCE_CACHE[session_id] = source
    return source


def _is_fetch_app_session(session_id: str | None) -> bool:
    """True when the persisted session is owned by the Fetch app surface.

    The iOS app currently speaks the shared dashboard/TUI websocket protocol,
    and that gateway may instantiate the underlying agent with a generic TUI
    platform value. The durable signal we own is the session row's source tag,
    which Fetch stamps as `fetch` on app-created chats and `inbox` on app-owned
    proactive threads.
    """
    source = (_session_source(session_id) or "").strip().lower()
    return source in FETCH_APP_SOURCES


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


# Sentinels a directory the plugin installed itself (vs. a user/custom skill it
# must never touch). Written into every plugin-managed copy on install.
_MANAGED_MARKER = ".fetch-plugin-managed"


def _install_managed_skill(bundled: Path, target: Path) -> None:
    """Install or refresh a plugin-managed copy of a bundled skill.

    Copies the plugin-owned ``bundled`` skill tree into ``target``. Each copy
    the plugin installs is marked with a sentinel file and refreshed from the
    bundled source on every call, so a newer bundled skill propagates on the
    next plugin load. A pre-existing ``target`` without the sentinel is treated
    as a user/custom skill and is left untouched.
    """
    if not (bundled / "SKILL.md").is_file():
        return
    if target.exists() and not (target / _MANAGED_MARKER).is_file():
        return  # user-customized skill — never overwrite it
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(bundled, target)
    (target / _MANAGED_MARKER).write_text("", encoding="utf-8")
    log.info("Installed bundled Fetch skill: %s", target)


def _ensure_fetch_cards_skill() -> None:
    """Expose the bundled fetch-cards skill to Hermes agents.

    Plugin platform hints and Fetch cron jobs refer to `fetch-cards`, but Hermes
    only discovers skills under HERMES_HOME/skills. Installing the plugin should
    therefore make the bundled skill visible without requiring a separate manual
    skill install. Plugin-managed installs are refreshed on every load so the
    bundled skill stays aligned with the app schema; a skill that a user has
    customized (no plugin sentinel) is never touched.
    """
    bundled = Path(__file__).resolve().parent / "skills" / "fetch-cards"
    if not (bundled / "SKILL.md").is_file():
        return
    try:
        from hermes_cli.config import get_hermes_home

        _install_managed_skill(bundled, get_hermes_home() / "skills" / "fetch-cards")
    except Exception:
        log.debug("Could not install bundled fetch-cards skill", exc_info=True)


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


def _on_pre_llm_call(*, session_id: str = "", **_kwargs):
    """Inject Fetch's native mobile output contract into every Fetch turn.

    Hermes' dashboard/TUI websocket path can build the agent with a generic TUI
    platform even when the real client is Fetch iOS. The supported
    `pre_llm_call` hook lets the plugin add API-call-time context to the current
    user message without persisting it or showing it in the app transcript.
    """
    if _is_kanban_worker():
        return None
    if not _is_fetch_app_session(session_id or None):
        return None
    return {"context": _FETCH_IOS_TURN_CONTEXT}


# Truthy spellings the model may pass for a boolean tool arg (kanban_create's
# `triage` arrives as a JSON bool, but be tolerant of stringified forms).
_TRUTHY_ARG = frozenset({True, 1, "1", "true", "True", "yes", "on"})


def _on_pre_tool_call(*, tool_name: str = "", args: dict | None = None, **_kwargs):
    """Enforce that agent-created kanban tasks carry a real spec (FET-16).

    A title-only card strands the worker: the assignee sees only the task's
    title and body, so a card with an empty body gives it nothing to act on
    ("creates titles but not useful details"). Hermes honors a
    ``{"action": "block", "message": ...}`` return from a ``pre_tool_call``
    hook (see ``hermes_cli.plugins.get_pre_tool_call_block_message``): the
    block message is handed back to the model, which then re-calls
    ``kanban_create`` with a body.

    This lives in the Fetch plugin — a supported extension point — instead of a
    patch to the ``kanban_create`` schema / KANBAN_GUIDANCE, so a ``hermes
    update`` can't silently drop it. It applies to every ``kanban_create`` on
    this host (the body requirement is universally good); ``triage`` stubs are
    exempt because a specifier profile fleshes those out later.
    """
    if tool_name != "kanban_create":
        return None
    args = args if isinstance(args, dict) else {}
    body = args.get("body")
    body = body.strip() if isinstance(body, str) else ""
    if body:
        return None
    if args.get("triage") in _TRUTHY_ARG:
        return None
    return {
        "action": "block",
        "message": (
            "kanban_create needs a `body`. Write the full spec — goal, "
            "acceptance criteria, and any relevant links/context — then "
            "re-call kanban_create with it. The worker that picks up this "
            "card sees only the title and body, so a title-only card leaves "
            "it nothing to act on. (Pass triage=true only if you intend a "
            "deliberately empty stub for a specifier to flesh out.)"
        ),
    }


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
    _ensure_fetch_cards_skill()

    # Push hooks: notify the app on turn completion / attention-needed.
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("post_llm_call", _on_post_llm_call)
    ctx.register_hook("pre_approval_request", _on_pre_approval_request)
    # Quality gate: require a real `body` on agent-created kanban tasks (FET-16).
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)

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
                platform_hint=(
                    "You are chatting in the Fetch iOS app. Fetch renders "
                    "standard Markdown (bold, italic, headings, lists, code "
                    "blocks, tables) and additionally supports generative-UI "
                    "cards: emit a fenced code block whose language is `card` "
                    "containing a JSON object, and the app renders a native "
                    "tappable card instead of a code block. Use a card when "
                    "the reply is structured data — a daily brief, metrics "
                    "summary, tappable links, a small dashboard — and use "
                    "plain prose when the reply is narrative or explanation. "
                    "Cards are typographic and minimal: the title commands "
                    "the card, stats and item rows carry data through type "
                    "hierarchy alone. Omit `symbol` and `emoji` from every "
                    "field — no icon chips. Status (pass/fail, ready/blocked) "
                    "lives in the trailing `value` text on item rows, not "
                    "in icons. Carousel sub-cards are tappable as a whole — "
                    "do not use `cta`. "
                    "Card JSON schema (all fields optional):\n"
                    "  title, subtitle                  header text\n"
                    "  image                            hero image URL\n"
                    "  url                              URL opened when the whole card is tapped\n"
                    "  footer                           small caption text\n"
                    "  stats: [{label, value}]         big-number columns (value may be string, number, or bool)\n"
                    "  items: [{title, subtitle, value, url}]   tappable list rows\n"
                    "  cards: [{title, subtitle, image, badge, url}]   horizontal carousel sub-cards\n"
                    "  chart: {type, values, labels, legend, highlight, caption, value, series}   native chart body\n"
                    "  blocks: [{type, ...}]           ordered composable body blocks; when present, blocks replace stats/chart/items\n"
                    "Native chart types: bars, line, diverging, meter, groupedBars, horizontalBars, heatmap. "
                    "Fetch chart/report contract: when the user asks for a chart, graph, report, dashboard, usage, "
                    "token, ranking, trend, or visual breakdown in Fetch, emit a `card` fence with a native `chart` "
                    "or `blocks` chart. Do not answer with ASCII/text bars, SVG links, HTML artifacts, Mermaid, "
                    "image links, or external chart files unless the user explicitly asks for those formats. "
                    "For ranked token/usage reports, use `chart:{\"type\":\"horizontalBars\",\"values\":[...],\"labels\":[...]}` "
                    "or a `blocks` stack with `stats`, `chart`, and `text` blocks. If another skill suggests a "
                    "Markdown/terminal/SVG chart, the Fetch native-card contract wins. "
                    "Example — daily brief:\n"
                    "```card\n"
                    "{\"title\":\"Today\","
                    "\"stats\":[{\"label\":\"Meetings\",\"value\":3},{\"label\":\"Tasks\",\"value\":12}],"
                    "\"items\":[{\"title\":\"Standup\",\"subtitle\":\"9:30 Zoom\",\"url\":\"https://zoom.us/j/123\"}],"
                    "\"footer\":\"Updated just now\"}\n"
                    "```\n"
                    "For richer patterns (multi-stat dashboard, link carousel, weekly summary) load the `fetch-cards` skill. A malformed card fence falls back to a code block — it never breaks the chat, so prefer attempting a card over not emitting one when the content fits."
                ),
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
