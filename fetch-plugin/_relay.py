"""Shared client for the Fetch push relay.

This module is loaded **by file path** from both halves of the Fetch plugin —
the agent-runtime hooks in ``__init__.py`` (which run inside the TUI / gateway /
dashboard-chat agent process) and the device-registration route in
``dashboard/plugin_api.py`` (which runs inside the dashboard web_server process).
Those are two independent processes loaded by two different discovery systems;
they share nothing but this relay and the on-disk credentials file. Keeping the
client here (rather than importing ``hermes_plugins.fetch``) avoids depending on
the plugin namespace being registered in whichever process is importing.

The relay holds the single Fetch APNs key and fans out to the user's registered
devices. No Apple credentials ever live on this host — only an anonymous,
per-agent ``agent_id`` + ``agent_secret`` minted on first use.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

log = logging.getLogger("fetch_plugin.relay")

# Hosted Fetch push relay. Override with HERMES_FETCH_RELAY_URL (e.g. point at a
# locally-run relay during development: http://127.0.0.1:8787).
DEFAULT_RELAY_URL = "https://push.tryfetchapp.com"
REGISTRATION_TOKEN_ENV = "HERMES_FETCH_RELAY_REGISTRATION_TOKEN"
ENROLLMENT_TOKEN_ENV = "HERMES_FETCH_ENROLLMENT_TOKEN"

_DEDUPE_WINDOW_S = 10.0

_SYSTEM_INBOX_AGENT_SLUGS = {"default"}


def _clean_agent_slug(value: object) -> str:
    """Return a bounded profile identity that is safe for a push payload."""
    text = str(value or "").strip()
    if not text:
        return ""
    return text[:80]


def _agent_from_session(session_id: str | None) -> str:
    """Best-effort profile identity encoded by Fetch/Hermes session keys."""
    raw = str(session_id or "").strip()
    parts = raw.split(":")
    if len(parts) >= 2 and parts[0] == "agent":
        return _clean_agent_slug(parts[1])
    if not raw.startswith("inbox_"):
        return ""
    slug = _clean_agent_slug(raw.removeprefix("inbox_"))
    if slug in _SYSTEM_INBOX_AGENT_SLUGS or slug.startswith(("cron-", "thread-")):
        return "default"
    return slug


def _active_agent() -> str:
    """Resolve the current profile without leaking across concurrent sessions."""
    try:
        from gateway.session_context import get_session_env

        profile = _clean_agent_slug(get_session_env("HERMES_SESSION_PROFILE", ""))
        if profile:
            return profile
    except Exception:
        pass
    profile = _clean_agent_slug(os.environ.get("HERMES_PROFILE"))
    if profile:
        return profile
    try:
        from hermes_cli.profiles import get_active_profile_name

        return _clean_agent_slug(get_active_profile_name())
    except Exception:
        return ""


def _agent_display_name(agent_id: str) -> str:
    if not agent_id or agent_id.lower() in {"default", "custom"}:
        return "Fetch"
    return " ".join(
        part[:1].upper() + part[1:]
        for part in agent_id.replace("_", "-").split("-")
        if part
    )[:120] or "Fetch"


def _with_agent_identity(
    *, session_id: str | None, data: dict | None
) -> dict:
    """Stamp every notification with one stable sender id + display name.

    Explicit task metadata wins, then an identity encoded in the session, then
    the task-local Hermes profile. Resolving this before starting the daemon
    thread is important: contextvars do not automatically cross that boundary.
    """
    merged = dict(data) if isinstance(data, dict) else {}
    explicit = merged.get("agent_id") or merged.get("assignee")
    agent_id = _clean_agent_slug(explicit) or _agent_from_session(session_id) or _active_agent()
    if not agent_id:
        agent_id = "default"
    merged["agent_id"] = agent_id
    merged["agent_name"] = _clean_agent_slug(merged.get("agent_name")) or _agent_display_name(agent_id)
    return merged


def _hermes_home(hermes_home: Path | None = None) -> Path:
    if hermes_home is not None:
        return Path(hermes_home).expanduser()
    store_home = os.environ.get("HERMES_FETCH_STORE_HOME", "").strip()
    if store_home:
        return Path(os.path.expanduser(store_home))
    try:
        from hermes_cli.config import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if value[:1] in {"'", '"'}:
            quote = value[0]
            end = value.find(quote, 1)
            value = value[1:end] if end != -1 else value[1:]
        if key:
            values[key] = value
    return values


def _config_value(name: str, default: str | None = None, *, hermes_home: Path | None = None) -> str | None:
    """Read a config value from the environment, falling back to a Hermes home's .env."""
    val = os.environ.get(name)
    if val:
        return val
    file_val = _parse_env_file(_hermes_home(hermes_home) / ".env").get(name)
    return file_val if file_val else default


class NeedsAttestation(Exception):
    """Relay requires an App Attest attestation to enroll this agent."""


class RelayRegistrationError(Exception):
    """Relay rejected first-time agent enrollment with an actionable setup error."""


@dataclass(frozen=True)
class RelayCredentials:
    relay_url: str
    agent_id: str
    agent_secret: str
    # App-tunnel pairing capability token (plaintext). Minted by the relay and
    # returned once at registration; the relay keeps only its hash, so this is
    # the agent's only copy. Carried in the relay setup link / QR so a device can
    # reach this agent through the tunnel. None for agents enrolled before
    # pairing capture existed — re-minted on demand via ``relay_pairing()``.
    pairing: str | None = None


class RelayClient:
    """Talks the Fetch push relay's ``/v1/*`` contract with per-agent auth."""

    def __init__(
        self,
        *,
        relay_url: str,
        credentials_path: Path,
        registration_token: str | None = None,
        enrollment_token: str | None = None,
    ) -> None:
        self.relay_url = relay_url.rstrip("/")
        self.credentials_path = Path(credentials_path)
        self.registration_token = registration_token
        self.enrollment_token = enrollment_token

    async def get_attest_challenge(self) -> str:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{self.relay_url}/v1/attest/challenge")
        response.raise_for_status()
        return str(response.json()["challenge"])

    async def register_device(
        self, *, token: str, platform: str, environment: str, bundle_id: str, preferences: dict,
        attestation: dict | None = None,
    ) -> None:
        await self._post(
            "/v1/devices/register",
            {
                "token": token,
                "platform": platform,
                "environment": environment,
                "bundle_id": bundle_id,
                "preferences": preferences,
            },
            authenticated=True,
            attestation=attestation,
        )

    async def unregister_device(self, *, token: str) -> None:
        await self._post("/v1/devices/unregister", {"token": token}, authenticated=True)

    async def report_badge(self, *, token: str, count: int) -> None:
        await self._post("/v1/devices/badge", {"token": token, "count": int(count)}, authenticated=True)

    async def send_event(self, *, kind: str, session_id: str | None, title: str, body: str,
                         source: str | None = None, data: dict | None = None) -> None:
        await self._post(
            "/v1/push/events",
            {"type": kind, "session_id": session_id, "title": title, "body": body,
             "source": source, "data": data or {}},
            authenticated=True,
        )

    async def _post(self, path: str, json_body: dict, *, authenticated: bool, attestation: dict | None = None) -> None:
        response: httpx.Response | None = None
        attempts = 2 if authenticated else 1
        for attempt in range(attempts):
            headers: dict[str, str] = {}
            if authenticated:
                creds = await self._credentials(attestation=attestation)
                headers["X-Hermes-Agent-Id"] = creds.agent_id
                headers["Authorization"] = f"Bearer {creds.agent_secret}"
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(f"{self.relay_url}{path}", headers=headers, json=json_body)
            # A 401 means our cached credentials were revoked/rotated server-side;
            # drop them and re-mint once.
            if authenticated and response.status_code == 401 and attempt == 0:
                self._clear_credentials()
                continue
            break
        if response is None:
            # The loop body always assigns at least once, but assert would be
            # stripped under `python -O`; guard explicitly so a future refactor
            # that skips the loop fails loudly instead of crashing on None.
            raise RuntimeError("relay request produced no response")
        response.raise_for_status()

    async def _credentials(self, attestation: dict | None = None) -> RelayCredentials:
        existing = self._read_credentials()
        if existing is not None:
            return existing
        headers: dict[str, str] = {}
        if self.registration_token:
            headers["X-Hermes-Relay-Registration-Token"] = self.registration_token
        body: dict = {"app": "fetch-ios"}
        if self.enrollment_token:
            body["enrollment_token"] = self.enrollment_token
        if attestation:
            try:
                body.update({"attestation": attestation["attestation"],
                             "key_id": attestation["key_id"],
                             "challenge": attestation["challenge"]})
            except KeyError as exc:
                raise ValueError(f"attestation dict missing key: {exc}") from exc
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{self.relay_url}/v1/agents/register", headers=headers, json=body
            )
        # Relay returns 400 with detail "attestation required" when App Attest enrollment
        # is required but no attestation was supplied. Prefer the structured detail field;
        # fall back to response text. Match exactly "attestation required" so other 400s
        # ("invalid or expired challenge") don't false-trigger.
        if response.status_code == 400:
            detail = ""
            try:
                detail = str(response.json().get("detail", ""))
            except Exception:
                detail = response.text or ""
            if "attestation required" in detail.lower():
                raise NeedsAttestation("relay requires attestation to enroll")
        if response.status_code in {401, 403, 503}:
            detail = _response_detail(response)
            if self.enrollment_token:
                raise RelayRegistrationError(
                    "The Fetch setup code was rejected. Generate a fresh code in the Fetch app, "
                    "then run `hermes setup` again."
                )
            if self.registration_token:
                raise RelayRegistrationError(
                    "The relay rejected HERMES_FETCH_RELAY_REGISTRATION_TOKEN. "
                    "Check that it matches the relay's configured registration token."
                )
            raise RelayRegistrationError(
                "Fetch setup needs a setup code from the signed-in iOS app. "
                "Open Fetch, tap Create setup code, then paste that code here."
                + (f" Relay response: {detail}" if detail else "")
            )
        response.raise_for_status()
        data = response.json()
        pairing = data.get("pairing_secret")
        creds = RelayCredentials(
            relay_url=self.relay_url,
            agent_id=str(data["agent_id"]),
            agent_secret=str(data["agent_secret"]),
            pairing=str(pairing) if pairing else None,
        )
        self._write_credentials(creds)
        # Re-read so two processes that mint concurrently converge on whichever
        # identity won the atomic file write.
        return self._read_credentials() or creds

    async def relay_pairing(self) -> tuple[str, str, str]:
        """Return ``(relay_url, agent_id, pairing)`` for building a relay setup
        link. Always mints a fresh token so rerunning Hermes setup after a
        successful app claim cannot reprint a stale one-time pairing secret.
        """
        creds = await self._credentials()
        pairing = await self._mint_pairing(creds)
        return self.relay_url, creds.agent_id, pairing

    async def tunnel_status(self) -> dict:
        """Return the relay's view of this agent's tunnel readiness."""
        creds = await self._credentials()
        headers = {
            "X-Hermes-Agent-Id": creds.agent_id,
            "Authorization": f"Bearer {creds.agent_secret}",
        }
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                f"{self.relay_url}/v1/agents/tunnel/status", headers=headers
            )
        if response.status_code == 404:
            return {"ok": False, "reason": "status_unavailable", "status_code": 404}
        if response.status_code in {200, 503}:
            try:
                return response.json()
            except ValueError:
                return {"ok": False, "reason": "invalid_status_response"}
        response.raise_for_status()
        return response.json()

    async def wait_for_tunnel_online(
        self, *, timeout_s: float = 20.0, interval_s: float = 0.5, on_poll=None
    ) -> dict:
        """Poll until the relay sees this agent's tunnel uplink or time expires.

        ``on_poll`` (elapsed seconds -> None) lets interactive callers show
        progress while a cold-started runtime spawns and dials the relay.
        """
        start = time.monotonic()
        deadline = start + max(0.0, timeout_s)
        last: dict = {"ok": False, "reason": "not_checked"}
        while True:
            try:
                last = await self.tunnel_status()
            except Exception as exc:
                last = {"ok": False, "reason": type(exc).__name__}
            if bool(last.get("ok") or last.get("agent_online")):
                return last
            if time.monotonic() >= deadline:
                return last
            if on_poll is not None:
                try:
                    on_poll(time.monotonic() - start)
                except Exception:
                    pass
            await asyncio.sleep(max(0.1, interval_s))

    async def _mint_pairing(self, creds: RelayCredentials) -> str:
        """Rotate + fetch a fresh pairing token for an already-enrolled agent."""
        headers = {
            "X-Hermes-Agent-Id": creds.agent_id,
            "Authorization": f"Bearer {creds.agent_secret}",
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{self.relay_url}/v1/agents/pairing", headers=headers, json={}
            )
        response.raise_for_status()
        pairing = str(response.json()["pairing_secret"])
        # Persist alongside the existing identity so the file stays a valid
        # pairing record (drives `is_pairing_configured()` / tunnel autostart).
        # Setup always re-mints, so this is not reused as a setup link.
        self._write_credentials(
            RelayCredentials(
                relay_url=creds.relay_url,
                agent_id=creds.agent_id,
                agent_secret=creds.agent_secret,
                pairing=pairing,
            )
        )
        return pairing

    def _read_credentials(self) -> RelayCredentials | None:
        try:
            data = json.loads(self.credentials_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if data.get("relay_url") != self.relay_url:
            return None
        agent_id = str(data.get("agent_id") or "")
        agent_secret = str(data.get("agent_secret") or "")
        if not agent_id or not agent_secret:
            return None
        pairing = data.get("pairing")
        return RelayCredentials(
            relay_url=self.relay_url,
            agent_id=agent_id,
            agent_secret=agent_secret,
            pairing=str(pairing) if pairing else None,
        )

    def _write_credentials(self, creds: RelayCredentials) -> None:
        self.credentials_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.credentials_path.with_suffix(".tmp")
        payload = {
            "relay_url": creds.relay_url,
            "agent_id": creds.agent_id,
            "agent_secret": creds.agent_secret,
        }
        if creds.pairing:
            payload["pairing"] = creds.pairing
        tmp.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.credentials_path)

    def _clear_credentials(self) -> None:
        try:
            self.credentials_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            log.debug("Could not remove stale Fetch relay credentials", exc_info=True)


_client_singletons: dict[Path, RelayClient] = {}
_client_lock = threading.RLock()


def relay_client(*, hermes_home: Path | None = None) -> RelayClient:
    home = _hermes_home(hermes_home)
    with _client_lock:
        client = _client_singletons.get(home)
        if client is None:
            relay_url = _config_value(
                "HERMES_FETCH_RELAY_URL", DEFAULT_RELAY_URL, hermes_home=home
            ) or DEFAULT_RELAY_URL
            token = _config_value(REGISTRATION_TOKEN_ENV, hermes_home=home)
            enrollment_token = _config_value(ENROLLMENT_TOKEN_ENV, hermes_home=home)
            client = RelayClient(
                relay_url=relay_url,
                credentials_path=home / "push" / "fetch-relay.json",
                registration_token=token,
                enrollment_token=enrollment_token,
            )
            _client_singletons[home] = client
        return client


def _response_detail(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return (response.text or "").strip()
    detail = data.get("detail") if isinstance(data, dict) else None
    return str(detail or "").strip()


_recent: dict[str, float] = {}
_recent_lock = threading.Lock()


def _is_duplicate(key: str) -> bool:
    now = time.time()
    with _recent_lock:
        last = _recent.get(key)
        _recent[key] = now
        if len(_recent) > 512:  # opportunistic cleanup
            for k, t in list(_recent.items()):
                if now - t > _DEDUPE_WINDOW_S:
                    _recent.pop(k, None)
    return last is not None and (now - last) < _DEDUPE_WINDOW_S


def send_event_background(
    *,
    kind: str,
    session_id: str | None,
    title: str,
    body: str,
    source: str | None = None,
    data: dict | None = None,
    hermes_home: Path | None = None,
) -> None:
    """Fire-and-forget a push event to the relay. Never blocks the caller.

    Runs the HTTPS POST on a daemon thread so a finished turn is never delayed by
    push delivery, and de-dupes a short window so the same reply can't double-fire.

    ``source`` is the Hermes session's channel (e.g. "telegram", "" for a gateway
    Fetch chat, "inbox" for a proactive inbox delivery). It rides the push to the
    device so the phone can decide inbox membership agent-agnostically (only
    Fetch-channel pushes become inbox threads) instead of maintaining a
    Hermes-specific denylist.
    """
    data = _with_agent_identity(session_id=session_id, data=data)
    data_key = str(data.get("target") or "") + ":" + str(data.get("task_id") or "")
    if _is_duplicate(f"{kind}:{session_id or ''}:{data_key}:{(body or '')[:80]}"):
        return
    threading.Thread(
        target=_send_sync, args=(kind, session_id, title, body, source, data, hermes_home),
        daemon=True, name=f"fetch-push-{kind}"
    ).start()


def _send_sync(kind: str, session_id: str | None, title: str, body: str,
               source: str | None, data: dict | None, hermes_home: Path | None) -> None:
    try:
        asyncio.run(relay_client(hermes_home=hermes_home).send_event(
            kind=kind,
            session_id=session_id,
            title=title,
            body=body,
            source=source,
            data=data,
        ))
    except Exception:
        log.debug("Fetch push event delivery failed", exc_info=True)
