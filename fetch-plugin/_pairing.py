"""Fetch pairing — the agent-side onboarding flow (link + QR).

Run from ``hermes setup`` when the user picks the **Fetch** channel. Produces a
setup link the Fetch iOS app understands (``SetupLink.parse`` in the app) and
renders it as an in-terminal QR so the user can scan it from a fresh app
install — the WhatsApp Web pairing experience.

Two pairing shapes, both accepted by the app's one parser/scanner:

  * **Relay** (primary, ``?agent=&pairing=``) — reaches the agent through the
    hosted relay over the agent's outbound reverse tunnel. No inbound port, no
    Tailscale. Authorized by a per-agent capability token minted by the relay
    (the relay keeps only its hash). This is the strategic, "works anywhere"
    path; it needs the agent's tunnel enabled (``HERMES_FETCH_TUNNEL_ENABLED=1``)
    and a relay started with ``HERMES_RELAY_ENABLE_TUNNEL``.
  * **Direct** (fallback, ``?url=&token=``) — the dashboard URL + a stable
    ``HERMES_DASHBOARD_SESSION_TOKEN``. The app reaches that host over LAN /
    Tailscale. Available today with no extra infrastructure.

If the relay can't be reached at setup time, this degrades gracefully to a
direct-only pairing so the flow always completes.

Loaded by file path from ``__init__.py`` (same pattern as ``_relay.py``) so it
has no dependency on the plugin namespace being importable.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import secrets
import socket
import sys
from pathlib import Path
from urllib.parse import quote

# Universal-link host the app is entitled for (``applinks:tryfetchapp.com``).
# A link under this host opens the Fetch app directly when tapped, and
# ``SetupLink.parseFetchSetupURL`` accepts both the relay (``?agent=&pairing=``)
# and direct (``?url=&token=``) shapes.
_SETUP_LINK_HOST = "https://tryfetchapp.com/setup"
# Mirrors ``SetupLink.defaultRelayURL`` in the app: when the relay link points at
# the hosted relay we omit ``&relay=`` (smaller QR; the app fills in this same
# default). Override only adds the param when a custom relay is in use.
_DEFAULT_RELAY_URL = "https://push.tryfetchapp.com"
_DEFAULT_DASHBOARD_PORT = 9119


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


def _hermes_home() -> Path:
    try:
        from hermes_cli.config import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


def _lan_ip() -> str | None:
    """Best-effort primary LAN IPv4 for this host.

    Opens a throwaway UDP socket to a public address (no packets are sent) so
    the OS picks the egress interface; returns that interface's address. Used
    only to suggest a reachable host in the printed link — the user can edit it.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return None


def _ensure_stable_token() -> str:
    """Return a durable dashboard session token, minting one if needed.

    The dashboard's in-memory ``_SESSION_TOKEN`` is regenerated on every server
    start (``web_server.py``), so it can't anchor a saved app connection. We
    persist one to ``~/.hermes/.env`` and the user runs the dashboard with it.
    """
    existing = (os.environ.get("HERMES_DASHBOARD_SESSION_TOKEN") or "").strip()
    if existing:
        return existing
    try:
        from hermes_cli.config import get_env_value, save_env_value

        saved = (get_env_value("HERMES_DASHBOARD_SESSION_TOKEN") or "").strip()
        if saved:
            return saved
        token = secrets.token_urlsafe(24)
        save_env_value("HERMES_DASHBOARD_SESSION_TOKEN", token)
        return token
    except Exception:
        # Config helpers unavailable (shouldn't happen under `hermes setup`) —
        # fall back to a process-local token so the flow still completes.
        return secrets.token_urlsafe(24)


def _saved_dashboard_token() -> str:
    existing = (os.environ.get("HERMES_DASHBOARD_SESSION_TOKEN") or "").strip()
    if existing:
        return existing
    try:
        from hermes_cli.config import get_env_value

        return (get_env_value("HERMES_DASHBOARD_SESSION_TOKEN") or "").strip()
    except Exception:
        return ""


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
    return _has_relay_pairing_credentials() or bool(_saved_dashboard_token())


def _dashboard_base_url(host: str) -> str:
    return f"http://{host}:{_DEFAULT_DASHBOARD_PORT}"


def build_setup_link(*, base_url: str, token: str) -> str:
    """Assemble a **direct** setup link (LAN / Tailscale host + token)."""
    return f"{_SETUP_LINK_HOST}?url={quote(base_url, safe='')}&token={quote(token, safe='')}"


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
    """Best-effort relay pairing link. Returns None if the relay can't be reached
    (so the caller degrades to direct-only) — pairing must never hard-fail."""
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

    Idempotent and side-effect-light: mints/persists a relay pairing token and a
    stable dashboard token (only if not already set), then prints the pairing
    link(s) + QR. Does NOT write a ``platforms.fetch`` config block, so the
    gateway never tries to connect a transport adapter.
    """
    from hermes_cli.cli_output import (
        prompt,
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
        print_info("Fetch: already configured")
        if not prompt_yes_no("Reconfigure Fetch?", False):
            return
        print()

    print_info("Pair the Fetch iOS app to this agent — like linking WhatsApp Web.")
    print()

    relay_link = _try_build_relay_link()

    if relay_link:
        # Relay is the headline path: works anywhere, no Tailscale. QR + link.
        _print_pairing(
            "Fetch pairing ready — Relay (works anywhere, no Tailscale).",
            relay_link,
            with_qr=True,
        )
        print_info(
            "Relay pairing needs the agent's reverse tunnel — run the dashboard with:\n"
            "      HERMES_FETCH_TUNNEL_ENABLED=1 hermes dashboard\n"
            "  (and a relay started with HERMES_RELAY_ENABLE_TUNNEL)."
        )
        print()

        # Direct as a text-only fallback for LAN / Tailscale users — no second
        # QR (one code keeps the screen scannable), auto-using the detected IP.
        token = _ensure_stable_token()
        host = _lan_ip() or "127.0.0.1"
        direct_link = build_setup_link(base_url=_dashboard_base_url(host), token=token)
        print_info("Prefer a direct LAN / Tailscale connection? Paste this instead:")
        print(f"    {direct_link}")
        print()
        print_info(
            f"Launch the dashboard with this token so the direct link keeps working:\n"
            f"      HERMES_DASHBOARD_SESSION_TOKEN={token} hermes dashboard"
        )
        print()
        return

    # Relay unavailable — degrade to a full direct pairing (QR + link), prompting
    # for the reachable host as the original direct flow did.
    print_warning(
        "Relay pairing unavailable (relay unreachable) — using direct pairing. "
        "The app reaches this machine over your LAN or Tailscale."
    )
    print()
    token = _ensure_stable_token()
    suggested_ip = _lan_ip()
    if suggested_ip:
        print_info(f"Detected this machine at {suggested_ip} on your local network.")
        print_info("For access away from home, use your Tailscale IP or hostname instead.")
    default_host = suggested_ip or "127.0.0.1"
    host = (prompt(f"Dashboard host or IP [{default_host}]") or default_host).strip()

    direct_link = build_setup_link(base_url=_dashboard_base_url(host), token=token)
    _print_pairing("Fetch pairing ready — Direct (LAN / Tailscale).", direct_link, with_qr=True)
    print_info(
        f"Launch the dashboard with this token so the link keeps working:\n"
        f"      HERMES_DASHBOARD_SESSION_TOKEN={token} hermes dashboard"
    )
    print()
