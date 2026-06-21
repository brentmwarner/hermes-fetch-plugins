"""Tests for the Fetch pairing link/QR builders (agent-side onboarding)."""

import importlib.util
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

# Load _pairing.py by path the same way the plugin does.
_p = Path(__file__).resolve().parent.parent / "_pairing.py"
_spec = importlib.util.spec_from_file_location("fetch_plugin_pairing_test", _p)
pairing = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = pairing
_spec.loader.exec_module(pairing)


def _query(link: str) -> dict:
    return {k: v[0] for k, v in parse_qs(urlsplit(link).query).items()}


def test_build_setup_link_direct_carries_url_and_token() -> None:
    link = pairing.build_setup_link(base_url="http://192.168.1.5:9119", token="tok 123")
    assert link.startswith("https://tryfetchapp.com/setup?")
    q = _query(link)
    assert q["url"] == "http://192.168.1.5:9119"
    assert q["token"] == "tok 123"  # decoded back; the raw link percent-encodes it
    assert "agent" not in q and "pairing" not in q


def test_build_relay_link_omits_relay_param_for_hosted_default() -> None:
    link = pairing.build_relay_link(
        agent_id="agent-1", pairing="cap-token", relay_url=pairing._DEFAULT_RELAY_URL
    )
    q = _query(link)
    assert q["agent"] == "agent-1"
    assert q["pairing"] == "cap-token"
    # Default relay is implied by the app — keep it out of the QR payload.
    assert "relay" not in q


def test_build_relay_link_includes_custom_relay() -> None:
    link = pairing.build_relay_link(
        agent_id="a/b", pairing="t&t", relay_url="https://relay.example.com"
    )
    q = _query(link)
    assert q["agent"] == "a/b"
    assert q["pairing"] == "t&t"
    assert q["relay"] == "https://relay.example.com"


def test_render_qr_returns_string_or_none() -> None:
    out = pairing.render_qr("https://tryfetchapp.com/setup?agent=x&pairing=y")
    # qrcode is optional; when present we get printable block art, else None.
    if out is not None:
        assert isinstance(out, str)
        assert out.strip()  # non-empty
