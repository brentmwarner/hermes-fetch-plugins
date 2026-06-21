"""Fetch pairing — the agent-side onboarding flow (link + QR).

Run from ``hermes setup`` when the user picks the **Fetch** channel. Produces a
relay setup link the Fetch iOS app understands (``SetupLink.parse`` in the app)
and renders it as an in-terminal QR so the user can scan it from a fresh app
install — the WhatsApp Web pairing experience.

Fetch setup has one supported connection shape:

  * **Relay** (``?agent=&pairing=``) — reaches the agent through the hosted relay
    over the agent's outbound reverse tunnel. No inbound port, no Tailscale.
    Authorized by a per-agent capability token minted by the relay (the relay
    keeps only its hash). It needs the agent's tunnel enabled
    (``HERMES_FETCH_TUNNEL_ENABLED=1``) and a relay started with
    ``HERMES_RELAY_ENABLE_TUNNEL``.

Loaded by file path from ``__init__.py`` (same pattern as ``_relay.py``) so it
has no dependency on the plugin namespace being importable.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path
from urllib.parse import quote

# Universal-link host the app is entitled for (``applinks:tryfetchapp.com``).
# A link under this host opens the Fetch app directly when tapped, and
# ``SetupLink.parseFetchSetupURL`` accepts the relay (``?agent=&pairing=``)
# shape.
_SETUP_LINK_HOST = "https://tryfetchapp.com/setup"
# Mirrors ``SetupLink.defaultRelayURL`` in the app: when the relay link points at
# the hosted relay we omit ``&relay=`` (smaller QR; the app fills in this same
# default). Override only adds the param when a custom relay is in use.
_DEFAULT_RELAY_URL = "https://push.tryfetchapp.com"


def _relay_module():
    """Load the shared ``_relay`` client by file path (reuse the already-loaded
    instance when the plugin runtime imported it first)."""
    existing = sys.modules.get("fetch_plugin_relay")
    if existing is not None:
        return existing
    path = Path(__file__).resolve().parent / "_relay.py"
    spec = importlib.util.spec_from_file_location("fetch_plugin_relay", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _runtime_module():
    """Load the relay runtime helper by file path."""
    existing = sys.modules.get("fetch_plugin_runtime")
    if existing is not None:
        return existing
    path = Path(__file__).resolve().parent / "_runtime.py"
    spec = importlib.util.spec_from_file_location("fetch_plugin_runtime", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _inbox_module():
    """Load the Fetch inbox helper by file path."""
    existing = sys.modules.get("fetch_plugin_inbox")
    if existing is not None:
        return existing
    path = Path(__file__).resolve().parent / "_inbox.py"
    spec = importlib.util.spec_from_file_location("fetch_plugin_inbox", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _hermes_home() -> Path:
    try:
        from hermes_cli.config import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


def _has_relay_pairing_credentials() -> bool:
    try:
        path = _hermes_home() / "push" / "fetch-relay.json"
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    agent_id = str(data.get("agent_id") or "").strip()
    pairing = str(data.get("pairing") or "").strip()
    return bool(agent_id and pairing)


def is_pairing_configured() -> bool:
    """True when setup can be re-run as a reconfiguration flow."""
    return _has_relay_pairing_credentials()


def build_relay_link(*, agent_id: str, pairing: str, relay_url: str) -> str:
    """Assemble a **relay** setup link (agent handle + capability token).

    ``relay=`` is omitted when the relay is the hosted default — the app fills in
    the same default, and a shorter payload makes a denser, easier-to-scan QR.
    """
    params = [
        f"agent={quote(agent_id, safe='')}",
        f"pairing={quote(pairing, safe='')}",
    ]
    if relay_url and relay_url.rstrip("/") != _DEFAULT_RELAY_URL:
        params.append(f"relay={quote(relay_url, safe='')}")
    return f"{_SETUP_LINK_HOST}?{'&'.join(params)}"


def _try_build_relay_link() -> str | None:
    """Best-effort relay pairing link. Returns None if the relay can't be reached."""
    try:
        relay = _relay_module()
        relay_url, agent_id, pairing = asyncio.run(relay.relay_client().relay_pairing())
        if not agent_id or not pairing:
            return None
        return build_relay_link(agent_id=agent_id, pairing=pairing, relay_url=relay_url)
    except Exception:
        return None


def render_qr(data: str) -> str | None:
    """Render ``data`` as a compact half-block QR, or None if ``qrcode`` is missing.

    Two QR rows per text line via upper/lower half-block glyphs (mirrors the
    DingTalk auth renderer). Returns the printable string; the caller prints it
    so this stays testable.
    """
    try:
        import qrcode
    except ImportError:
        return None

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=1,
        border=1,
    )
    qr.add_data(data)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    rows = len(matrix)

    TOP_HALF = "▀"     # ▀
    BOTTOM_HALF = "▄"  # ▄
    FULL_BLOCK = "█"   # █
    EMPTY = " "

    lines: list[str] = []
    for r in range(0, rows, 2):
        chars: list[str] = []
        for c in range(len(matrix[r])):
            top = matrix[r][c]
            bottom = matrix[r + 1][c] if r + 1 < rows else False
            if top and bottom:
                chars.append(FULL_BLOCK)
            elif top:
                chars.append(TOP_HALF)
            elif bottom:
                chars.append(BOTTOM_HALF)
            else:
                chars.append(EMPTY)
        lines.append("    " + "".join(chars))
    return "\n".join(lines)


def interactive_setup() -> None:
    """``setup_fn`` for the Fetch platform — pair the iOS app to this agent.

    Idempotent and side-effect-light: mints/persists a relay pairing token, then
    prints the pairing link + QR.
    """
    from hermes_cli.cli_output import (
        prompt_yes_no,
        print_header,
        print_info,
        print_success,
        print_warning,
    )

    def _print_pairing(title: str, link: str, *, with_qr: bool) -> None:
        print_success(title)
        print()
        if with_qr:
            qr = render_qr(link)
            if qr:
                print_info("Scan this from the Fetch app's Connect screen:")
                print()
                print(qr)
                print()
            else:
                print_warning("Install 'qrcode' to render a scannable code (pip install qrcode).")
                print()
        print_info("Or paste this link into the app:")
        print(f"    {link}")
        print()

    print_header("Fetch")
    if is_pairing_configured():
        _inbox_module().enable_delivery_for_future_starts()
        print_info("Fetch: already configured")
        if not prompt_yes_no("Reconfigure Fetch?", False):
            return
        print()

    print_info("Pair the Fetch iOS app to this agent — like linking WhatsApp Web.")
    print()

    relay_link = _try_build_relay_link()

    if relay_link:
        _inbox_module().enable_delivery_for_future_starts()
        runtime = _runtime_module()
        runtime.enable_tunnel_for_future_starts()
        runtime_status = runtime.ensure_relay_runtime()

        # Relay is the headline path: works anywhere, no Tailscale. QR + link.
        _print_pairing(
            "Fetch pairing ready — Relay (works anywhere, no Tailscale).",
            relay_link,
            with_qr=True,
        )
        if runtime_status in {"started", "already-running", "self"}:
            print_info("Fetch relay runtime is running in the background (no browser required).")
        elif runtime_status == "disabled":
            print_warning(
                "Fetch relay runtime autostart is disabled. Keep this running yourself:\n"
                "      HERMES_FETCH_TUNNEL_ENABLED=1 hermes dashboard --no-open"
            )
        else:
            print_warning(
                "Fetch could not start the relay runtime automatically. Keep this running yourself:\n"
                "      HERMES_FETCH_TUNNEL_ENABLED=1 hermes dashboard --no-open"
            )
        print()

        return

    # Relay is the only supported setup path. Failing closed avoids producing a
    # second, confusing URL/token pairing mode.
    _inbox_module().enable_delivery_for_future_starts()
    print_warning(
        "Relay pairing unavailable. Check that the relay is reachable and "
        "started with HERMES_RELAY_ENABLE_TUNNEL=1, then run Fetch setup again."
    )
    print()
