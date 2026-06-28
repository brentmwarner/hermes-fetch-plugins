"""Tests for the Fetch pairing link/QR builders (agent-side onboarding)."""

import importlib.util
import json
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


def test_is_pairing_configured_false_without_credentials(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(pairing, "_hermes_home", lambda: tmp_path)

    assert not pairing.is_pairing_configured()


def test_is_pairing_configured_true_with_relay_pairing(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(pairing, "_hermes_home", lambda: tmp_path)
    push_dir = tmp_path / "push"
    push_dir.mkdir()
    (push_dir / "fetch-relay.json").write_text(
        json.dumps(
            {
                "agent_id": "agent-1",
                "agent_secret": "secret",
                "pairing": "pairing-token",
                "relay_url": pairing._DEFAULT_RELAY_URL,
            }
        ),
        encoding="utf-8",
    )

    assert pairing.is_pairing_configured()


def test_is_pairing_configured_false_with_only_saved_dashboard_token(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(pairing, "_hermes_home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_DASHBOARD_SESSION_TOKEN", "direct-token")

    assert not pairing.is_pairing_configured()


class _FakeInbox:
    def enable_delivery_for_future_starts(self):
        pass


class _FakeRuntime:
    def __init__(self, status="started"):
        self.status = status

    def enable_tunnel_for_future_starts(self):
        pass

    def ensure_relay_runtime(self):
        return self.status


class _FakeRelayClient:
    def __init__(self, status):
        self.status = status

    async def wait_for_tunnel_online(self):
        return self.status


def test_interactive_setup_hides_link_when_tunnel_not_online(monkeypatch, capsys, tmp_path) -> None:
    link = "https://tryfetchapp.com/setup?agent=a1&pairing=p1"
    monkeypatch.setattr(pairing, "_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(pairing, "is_pairing_configured", lambda: False)
    monkeypatch.setattr(pairing, "_inbox_module", lambda: _FakeInbox())
    monkeypatch.setattr(pairing, "_runtime_module", lambda: _FakeRuntime())
    monkeypatch.setattr(
        pairing,
        "_try_build_relay_pairing",
        lambda: {
            "client": _FakeRelayClient({"ok": False, "agent_online": False, "reason": "agent_offline"}),
            "agent_id": "a1",
            "link": link,
        },
    )

    pairing.interactive_setup()

    out = capsys.readouterr().out
    assert "tunnel is not online yet" in out
    assert "setup QR is hidden" in out
    assert link not in out


def test_interactive_setup_prints_link_when_tunnel_online(monkeypatch, capsys, tmp_path) -> None:
    link = "https://tryfetchapp.com/setup?agent=a1&pairing=p1"
    monkeypatch.setattr(pairing, "_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(pairing, "is_pairing_configured", lambda: False)
    monkeypatch.setattr(pairing, "_inbox_module", lambda: _FakeInbox())
    monkeypatch.setattr(pairing, "_runtime_module", lambda: _FakeRuntime())
    monkeypatch.setattr(pairing, "render_qr", lambda data: None)
    monkeypatch.setattr(
        pairing,
        "_try_build_relay_pairing",
        lambda: {
            "client": _FakeRelayClient({"ok": True, "agent_online": True}),
            "agent_id": "a1",
            "link": link,
        },
    )

    pairing.interactive_setup()

    out = capsys.readouterr().out
    assert "Fetch pairing ready" in out
    assert link in out
