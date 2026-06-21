"""Fetch — Hermes Agent plugin (runtime half).

Makes Fetch a first-class Hermes channel with NO core patches and NO Apple
credentials on this host:

  * **Platform registration** — registers ``fetch`` as a gateway platform so it
    appears in ``hermes setup`` next to Telegram/Discord, with a pairing flow
    (``setup_fn``) that prints a setup link + QR for the iOS app (relay pairing
    when the tunnel is reachable, direct LAN/Tailscale otherwise). Same plugin
    extension point Discord uses (``ctx.register_platform``).
  * **Push hooks** — two agent hooks notify the app like a messaging app:
      - ``post_llm_call``        — a turn finished anywhere → "Fetch replied".
      - ``pre_approval_request`` — agent waiting on approval/question/secret →
                                   "needs attention".
  * **Reverse tunnel** (optional) — a persistent outbound WebSocket to the relay
    so the app can reach this NAT'd agent with no inbound port / no Tailscale.
    Gated behind ``HERMES_FETCH_TUNNEL_ENABLED`` (default off).

Each push hook fires-and-forgets an HTTPS POST to the Fetch relay, which holds
the single APNs key and fans out to registered devices. Device-token
registration is handled by the dashboard half (``dashboard/plugin_api.py``).

The platform's transport *adapter* is intentionally inert here — the live path
is the reverse tunnel above, selected by the app's relay pairing, not a gateway
connect adapter. Pairing writes no ``platforms.fetch`` config block, so the
gateway never adds Fetch to its connect set; the registered ``adapter_factory``/
``is_connected`` are belt-and-suspenders that keep an unconfigured Fetch out of
the live transport path.

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


def _on_post_llm_call(*, session_id: str = "", assistant_response: str = "", **_kwargs) -> None:
    """A turn completed (final_response present, not interrupted)."""
    if _is_kanban_worker():
        return  # FET-5: dispatched workers report on the task, never push.
    body = (assistant_response or "").strip() or "Finished working."
    _relay.send_event_background(
        kind="replies", session_id=session_id or None, title="Fetch replied", body=body
    )


def _on_pre_approval_request(
    *, command: str = "", description: str = "", session_key: str = "", **_kwargs
) -> None:
    """The agent is blocking on an approval / question / secret."""
    if _is_kanban_worker():
        return  # FET-5: a worker's approval prompt is automation, not a message.
    detail = (description or command or "").strip() or "Open Fetch to continue."
    _relay.send_event_background(
        kind="attention", session_id=session_key or None, title="Fetch needs your attention", body=detail
    )


def _inert_adapter(_config):
    """Adapter factory for the Fetch platform — deliberately not implemented.

    The live transport is the reverse tunnel (``_spawn_tunnel``), selected by the
    app's relay pairing — not a gateway connect adapter. Pairing writes no
    ``platforms.fetch`` config, so the gateway never selects Fetch for connect
    and never calls this. If something ever does, fail loudly rather than
    half-connect a non-existent transport.
    """
    raise NotImplementedError(
        "Fetch transport adapter is not available — Fetch reaches the agent via "
        "the reverse tunnel (relay pairing), not a gateway connect adapter."
    )


def _spawn_tunnel() -> None:
    """Start the agent-side reverse-tunnel client on a daemon thread, so the
    phone can reach this NAT'd agent with no inbound port. Gated behind
    HERMES_FETCH_TUNNEL_ENABLED (default off) and spawn-and-return so it never
    blocks plugin load. The tunnel module + its `websockets` dep are imported
    lazily here, so a host without them is unaffected unless the flag is set."""
    if not _truthy(os.environ.get("HERMES_FETCH_TUNNEL_ENABLED")):
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
                adapter_factory=_inert_adapter,
                check_fn=lambda: True,            # pairing has no host deps
                is_connected=lambda _cfg: False,  # never join the connect set (tunnel is the path)
                setup_fn=_pairing.interactive_setup,
                emoji="📲",
                install_hint="",
            )
            log.info("Fetch platform registered (pairing setup_fn; transport adapter inert)")
        except Exception:
            log.warning("Fetch platform registration failed; push hooks still active", exc_info=True)
    else:
        log.info("Hermes build has no register_platform; Fetch running as push-only")

    # Reverse tunnel: hold an outbound channel to the relay (gated, default off).
    _spawn_tunnel()

    log.info("Fetch plugin registered (push hooks + pairing + reverse tunnel)")
