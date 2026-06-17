"""Fetch push notifications — Hermes Agent plugin (runtime half).

Registers two agent hooks so the Fetch iOS app gets notified like a messaging
app, with NO Hermes core patches and NO Apple credentials on this host:

  * ``post_llm_call``       — a turn finished anywhere (phone, web dashboard, or
                              terminal) → a "Fetch replied" push.
  * ``pre_approval_request`` — the agent is waiting on an approval / clarifying
                              question / secret → a "needs attention" push.

Each hook fires-and-forgets an HTTPS POST to the Fetch push relay, which holds
the single APNs key and fans out to the user's registered devices. Device-token
registration is handled by the dashboard half (``dashboard/plugin_api.py``).

This file is imported by the Hermes ``PluginManager`` as ``hermes_plugins.fetch``
and its ``register(ctx)`` is called once at agent startup.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

log = logging.getLogger("fetch_plugin")

# Load the shared relay client by file path. The dashboard half loads the same
# module the same way from its own process; the two never share a Python import,
# only the relay and the on-disk credentials file. Register in sys.modules
# BEFORE exec so the module's @dataclass annotations resolve (it uses
# `from __future__ import annotations`).
_relay_path = Path(__file__).resolve().parent / "_relay.py"
_spec = importlib.util.spec_from_file_location("fetch_plugin_relay", _relay_path)
assert _spec is not None and _spec.loader is not None
_relay = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _relay
_spec.loader.exec_module(_relay)


def _on_post_llm_call(*, session_id: str = "", assistant_response: str = "", **_kwargs) -> None:
    """A turn completed (final_response present, not interrupted)."""
    body = (assistant_response or "").strip() or "Finished working."
    _relay.send_event_background(
        kind="replies", session_id=session_id or None, title="Fetch replied", body=body
    )


def _on_pre_approval_request(
    *, command: str = "", description: str = "", session_key: str = "", **_kwargs
) -> None:
    """The agent is blocking on an approval / question / secret."""
    detail = (description or command or "").strip() or "Open Fetch to continue."
    _relay.send_event_background(
        kind="attention", session_id=session_key or None, title="Fetch needs your attention", body=detail
    )


def register(ctx) -> None:
    ctx.register_hook("post_llm_call", _on_post_llm_call)
    ctx.register_hook("pre_approval_request", _on_pre_approval_request)
    log.info("Fetch push plugin registered (post_llm_call + pre_approval_request)")
