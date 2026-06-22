from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import httpx
import jwt
from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket
from pydantic import BaseModel, Field

from push_relay import tunnel

logger = logging.getLogger(__name__)

PushKind = Literal["replies", "attention", "proactive"]

_PROD_HOST = "https://api.push.apple.com"
_SANDBOX_HOST = "https://api.sandbox.push.apple.com"
_CATEGORY_COLUMN = {
    "replies": "notify_replies",
    "attention": "notify_attention",
    "proactive": "notify_proactive",
}
_GENERIC_COPY: dict[PushKind, tuple[str, str]] = {
    "replies": ("Fetch replied", "Open Fetch to continue."),
    "attention": ("Fetch needs your attention", "Open Fetch to continue."),
    "proactive": ("Fetch update", "Open Fetch to view the update."),
}
# Default App ID(s) the relay's single APNs key is authorized for. A device that
# registers any other bundle id (e.g. a stale ``com.brentwarner.hermes`` token
# from before the rename) is rejected, so it can never be stored and silently
# fail forever with DeviceTokenNotForTopic.
_DEFAULT_ALLOWED_BUNDLE_IDS = frozenset({"com.brentwarner.fetch"})


def _split_csv(value: str | None) -> frozenset[str]:
    return frozenset(part.strip() for part in (value or "").split(",") if part.strip())


@dataclass(frozen=True)
class Settings:
    database_path: Path
    apns_key_pem: str | None
    apns_key_id: str | None
    apns_team_id: str | None
    registration_token: str | None
    allow_custom_body: bool
    # Hardening knobs (all have safe defaults so existing call sites keep working).
    allowed_bundle_ids: frozenset[str] = _DEFAULT_ALLOWED_BUNDLE_IDS
    allow_open_registration: bool = False
    secret_pepper: str = ""
    max_devices_per_agent: int = 50
    rate_limit_per_min: int = 120
    require_attestation: bool = False
    apple_app_id: str | None = None
    attest_production: bool = True
    # Accept BOTH Apple App Attest environments (development + production) so one
    # relay serves Xcode/dev builds and TestFlight/App Store builds at once.
    attest_allow_both_environments: bool = False
    max_agents_per_attest_key: int = 5
    attest_challenge_ttl_s: int = 300
    # Reverse-tunnel broker. Both WS routes stay UNREGISTERED unless enabled, so
    # the relay never exposes an app channel before the iOS cutover is ready.
    enable_tunnel: bool = False
    max_apps_per_agent: int = 16
    # Reverse-tunnel transit buffer (store-and-forward for a sleeping agent).
    transit_ttl_s: int = 900
    max_transit_per_agent: int = 200
    max_transit_bytes_per_agent: int = 4 * 1024 * 1024
    # Optional at-rest key for buffered frames; unset = TLS-only + fast eviction.
    transit_key: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        db_path = Path(
            os.environ.get("HERMES_RELAY_DATABASE_PATH")
            or (Path(__file__).resolve().parents[1] / "data" / "push-relay.db")
        )
        key_pem = os.environ.get("HERMES_RELAY_APNS_KEY") or os.environ.get("HERMES_APNS_KEY")
        key_path = os.environ.get("HERMES_RELAY_APNS_KEY_PATH") or os.environ.get("HERMES_APNS_KEY_PATH")
        if not key_pem and key_path:
            key_pem = Path(key_path).read_text(encoding="utf-8")
        allowed = _split_csv(os.environ.get("HERMES_RELAY_ALLOWED_BUNDLE_IDS")) or _DEFAULT_ALLOWED_BUNDLE_IDS
        return cls(
            database_path=db_path,
            apns_key_pem=key_pem,
            apns_key_id=os.environ.get("HERMES_RELAY_APNS_KEY_ID") or os.environ.get("HERMES_APNS_KEY_ID"),
            apns_team_id=os.environ.get("HERMES_RELAY_APNS_TEAM_ID") or os.environ.get("HERMES_APNS_TEAM_ID"),
            registration_token=os.environ.get("HERMES_RELAY_REGISTRATION_TOKEN"),
            allow_custom_body=_bool_env("HERMES_RELAY_ALLOW_CUSTOM_BODY", False),
            allowed_bundle_ids=allowed,
            allow_open_registration=_truthy(os.environ.get("HERMES_RELAY_ALLOW_OPEN_REGISTRATION")),
            secret_pepper=os.environ.get("HERMES_RELAY_SECRET_PEPPER") or "",
            max_devices_per_agent=_int_env("HERMES_RELAY_MAX_DEVICES_PER_AGENT", 50),
            rate_limit_per_min=_int_env("HERMES_RELAY_RATE_LIMIT_PER_MIN", 120),
            require_attestation=_truthy(os.environ.get("HERMES_RELAY_REQUIRE_ATTESTATION")),
            apple_app_id=os.environ.get("HERMES_RELAY_APPLE_APP_ID"),
            attest_production=_truthy(os.environ.get("HERMES_RELAY_ATTEST_PRODUCTION") or "true"),
            attest_allow_both_environments=_truthy(os.environ.get("HERMES_RELAY_ATTEST_ALLOW_BOTH")),
            max_agents_per_attest_key=_int_env("HERMES_RELAY_MAX_AGENTS_PER_ATTEST_KEY", 5),
            attest_challenge_ttl_s=_int_env("HERMES_RELAY_ATTEST_CHALLENGE_TTL_S", 300),
            enable_tunnel=_truthy(os.environ.get("HERMES_RELAY_ENABLE_TUNNEL")),
            max_apps_per_agent=_int_env("HERMES_RELAY_MAX_APPS_PER_AGENT", 16),
            transit_ttl_s=_int_env("HERMES_RELAY_TRANSIT_TTL_S", 900),
            max_transit_per_agent=_int_env("HERMES_RELAY_MAX_TRANSIT_PER_AGENT", 200),
            max_transit_bytes_per_agent=_int_env("HERMES_RELAY_MAX_TRANSIT_BYTES_PER_AGENT", 4 * 1024 * 1024),
            transit_key=os.environ.get("HERMES_RELAY_TRANSIT_KEY") or None,
        )

    @property
    def apns_configured(self) -> bool:
        return bool(self.apns_key_pem and self.apns_key_id and self.apns_team_id)


@dataclass(frozen=True)
class AgentAuth:
    agent_id: str


@dataclass(frozen=True)
class Device:
    token: str
    platform: str
    environment: str
    bundle_id: str
    sound: bool


class DeviceOwnershipError(Exception):
    """Raised when an agent tries to register a token owned by a different agent."""


class TooManyDevicesError(Exception):
    """Raised when an agent exceeds its device cap."""


class RegisterAgentBody(BaseModel):
    app: str = Field(default="fetch-ios", max_length=80)
    agent_version: str | None = Field(default=None, max_length=80)
    attestation: str | None = Field(default=None, max_length=8192)
    key_id: str | None = Field(default=None, max_length=128)
    challenge: str | None = Field(default=None, max_length=128)


class RegisterDeviceBody(BaseModel):
    token: str = Field(min_length=1, max_length=512)
    platform: str = Field(default="ios", max_length=32)
    environment: Literal["sandbox", "production"]
    bundle_id: str = Field(min_length=1, max_length=160)
    preferences: dict[str, bool] = Field(default_factory=dict)


class UnregisterDeviceBody(BaseModel):
    token: str = Field(min_length=1, max_length=512)


class PushEventBody(BaseModel):
    type: PushKind
    session_id: str | None = Field(default=None, max_length=160)
    title: str | None = Field(default=None, max_length=120)
    body: str | None = Field(default=None, max_length=500)
    source: str | None = Field(default=None, max_length=80)


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    assert table.isidentifier(), f"unsafe table name: {table!r}"
    return any(r["name"] == column for r in conn.execute(f"PRAGMA table_info({table})").fetchall())


class RelayStore:
    def __init__(self, db_path: Path, *, secret_pepper: str = "", max_devices_per_agent: int = 50) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._secret_pepper = secret_pepper
        self._max_devices_per_agent = max_devices_per_agent
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        # WAL lets reads run alongside writes; busy_timeout avoids spurious
        # "database is locked" under concurrent fan-out. (Real multi-instance
        # scale should move to Postgres — see README.)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _hash_secret(self, secret: str) -> str:
        if self._secret_pepper:
            return hmac.new(self._secret_pepper.encode("utf-8"), secret.encode("utf-8"), hashlib.sha256).hexdigest()
        return hashlib.sha256(secret.encode("utf-8")).hexdigest()

    @staticmethod
    def _legacy_hash_secret(secret: str) -> str:
        # Pre-pepper credentials were stored as a bare SHA-256 of the secret. Kept
        # so agents registered before HERMES_RELAY_SECRET_PEPPER was set can still
        # authenticate (and be migrated to the peppered hash) instead of being
        # locked out the moment a pepper is introduced.
        return hashlib.sha256(secret.encode("utf-8")).hexdigest()

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS agents(
                    id TEXT PRIMARY KEY,
                    secret_hash TEXT NOT NULL,
                    app TEXT NOT NULL,
                    agent_version TEXT,
                    created_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL,
                    revoked_at REAL,
                    attest_key_id TEXT
                )"""
            )
            if not _column_exists(conn, "agents", "attest_key_id"):
                conn.execute("ALTER TABLE agents ADD COLUMN attest_key_id TEXT")
            # Per-agent app-tunnel pairing capability token (hashed). Distinct
            # from the agent_secret and from the (public) agent_id.
            if not _column_exists(conn, "agents", "pairing_hash"):
                conn.execute("ALTER TABLE agents ADD COLUMN pairing_hash TEXT")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS devices(
                    id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    token TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    environment TEXT NOT NULL,
                    bundle_id TEXT NOT NULL,
                    notify_replies INTEGER NOT NULL DEFAULT 1,
                    notify_attention INTEGER NOT NULL DEFAULT 1,
                    notify_proactive INTEGER NOT NULL DEFAULT 1,
                    sound INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL,
                    UNIQUE(token, environment, bundle_id),
                    FOREIGN KEY(agent_id) REFERENCES agents(id)
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS push_events(
                    id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    type TEXT NOT NULL,
                    session_id TEXT,
                    device_count INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY(agent_id) REFERENCES agents(id)
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS attest_challenges (
                    challenge TEXT PRIMARY KEY,
                    created_at REAL NOT NULL
                )"""
            )
            # Short-lived store-and-forward buffer: holds app→agent frames (chat
            # sends) while the agent uplink is down, evicted on delivery / TTL.
            conn.execute(
                """CREATE TABLE IF NOT EXISTS transit (
                    id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    cid TEXT,
                    payload_enc BLOB NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                )"""
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_transit_agent ON transit(agent_id, created_at)")

    def register_agent(self, *, app: str, agent_version: str | None, attest_key_id: str | None = None) -> tuple[str, str]:
        agent_id = uuid.uuid4().hex
        secret = secrets.token_urlsafe(32)
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO agents
                    (id, secret_hash, app, agent_version, created_at, last_seen_at, attest_key_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (agent_id, self._hash_secret(secret), app, agent_version, now, now, attest_key_id),
            )
        return agent_id, secret

    def count_agents_for_key(self, attest_key_id: str) -> int:
        # Total agents ever enrolled under this attest key (no active/revoked filter):
        # the per-key cap is anti-abuse, so revoke+re-mint must not reset the count.
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM agents WHERE attest_key_id = ?", (attest_key_id,)
            ).fetchone()
        return int(row["n"])

    def authenticate_agent(self, *, agent_id: str, secret: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT secret_hash, revoked_at FROM agents WHERE id = ?",
                (agent_id,),
            ).fetchone()
            if row is None or row["revoked_at"] is not None:
                return False
            stored = str(row["secret_hash"])
            expected = self._hash_secret(secret)
            ok = hmac.compare_digest(stored, expected)
            migrate = False
            if not ok and self._secret_pepper:
                # The stored hash may predate the pepper. Accept the legacy bare
                # SHA-256 and transparently upgrade it to the peppered hash so the
                # credential keeps working after the pepper rollout.
                if hmac.compare_digest(stored, self._legacy_hash_secret(secret)):
                    ok = True
                    migrate = True
            if ok:
                if migrate:
                    conn.execute(
                        "UPDATE agents SET secret_hash = ?, last_seen_at = ? WHERE id = ?",
                        (expected, time.time(), agent_id),
                    )
                else:
                    conn.execute(
                        "UPDATE agents SET last_seen_at = ? WHERE id = ?",
                        (time.time(), agent_id),
                    )
            return ok

    def agent_exists(self, agent_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM agents WHERE id = ? AND revoked_at IS NULL", (agent_id,)
            ).fetchone()
        return row is not None

    def set_pairing(self, agent_id: str) -> str:
        """Mint + store (hashed) a per-agent app-tunnel pairing capability token,
        returning the plaintext secret once (the agent passes it to the device)."""
        secret = secrets.token_urlsafe(32)
        with self._connect() as conn:
            conn.execute(
                "UPDATE agents SET pairing_hash = ? WHERE id = ?",
                (self._hash_secret(secret), agent_id),
            )
        return secret

    def authenticate_pairing(self, *, agent_id: str, pairing_secret: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT pairing_hash, revoked_at FROM agents WHERE id = ?", (agent_id,)
            ).fetchone()
        if row is None or row["revoked_at"] is not None or not row["pairing_hash"]:
            return False
        return hmac.compare_digest(str(row["pairing_hash"]), self._hash_secret(pairing_secret))

    # --- Reverse-tunnel transit buffer (store-and-forward) ---

    def enqueue_transit(self, *, agent_id: str, payload: bytes, ttl_s: int, max_per_agent: int,
                        max_bytes: int | None = None) -> bool:
        """Buffer one app→agent frame. Returns False (rejected) if the per-agent
        count OR byte budget is reached, so the caller can tell the user it
        didn't land."""
        now = time.time()
        with self._connect() as conn:
            conn.execute("DELETE FROM transit WHERE expires_at <= ?", (now,))
            row = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(LENGTH(payload_enc)), 0) AS bytes FROM transit WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
            if int(row["n"]) >= max_per_agent:
                return False
            if max_bytes is not None and int(row["bytes"]) + len(payload) > max_bytes:
                return False
            conn.execute(
                "INSERT INTO transit (id, agent_id, cid, payload_enc, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                (uuid.uuid4().hex, agent_id, None, payload, now, now + ttl_s),
            )
        return True

    def peek_transit(self, agent_id: str) -> list[tuple[str, bytes]]:
        """Return live buffered frames (id, payload) WITHOUT deleting, FIFO. The
        caller deletes each via delete_transit only after a successful send, so a
        mid-drain WS drop leaves undelivered frames buffered for next reconnect."""
        now = time.time()
        with self._connect() as conn:
            conn.execute("DELETE FROM transit WHERE expires_at <= ?", (now,))
            rows = conn.execute(
                # rowid tiebreaks coarse same-tick created_at so FIFO is stable
                # regardless of query-plan / future index changes.
                "SELECT id, payload_enc FROM transit WHERE agent_id = ? ORDER BY created_at, rowid",
                (agent_id,),
            ).fetchall()
        return [(str(r["id"]), bytes(r["payload_enc"])) for r in rows]

    def delete_transit(self, ids: list[str]) -> None:
        if not ids:
            return
        with self._connect() as conn:
            conn.executemany("DELETE FROM transit WHERE id = ?", [(i,) for i in ids])

    def drain_transit(self, agent_id: str) -> list[bytes]:
        """Peek + delete all live buffered frames at once (evict-on-read). Kept
        for callers that don't need delete-after-send semantics."""
        rows = self.peek_transit(agent_id)
        self.delete_transit([rid for rid, _ in rows])
        return [payload for _, payload in rows]

    def count_transit(self, agent_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM transit WHERE agent_id = ?", (agent_id,)
            ).fetchone()
        return int(row["n"])

    def upsert_device(
        self,
        *,
        agent_id: str,
        token: str,
        platform: str,
        environment: str,
        bundle_id: str,
        preferences: dict[str, bool],
    ) -> str:
        now = time.time()
        with self._connect() as conn:
            # Bind a device token to its first agent. Refuse to silently re-own a
            # token already claimed by a different agent — that would let any
            # authenticated agent hijack a victim's phone (steal delivery + push
            # to their lock screen). Re-claiming requires the current owner to
            # unregister first.
            existing = conn.execute(
                "SELECT id, agent_id FROM devices WHERE token = ? AND environment = ? AND bundle_id = ?",
                (token, environment, bundle_id),
            ).fetchone()
            if existing is not None and str(existing["agent_id"]) != agent_id:
                logger.warning(
                    "Rejected device re-bind: token …%s owned by another agent", token[-8:]
                )
                raise DeviceOwnershipError("device token is registered to another agent")

            if existing is None and self._max_devices_per_agent > 0:
                count = conn.execute(
                    "SELECT COUNT(*) AS n FROM devices WHERE agent_id = ?", (agent_id,)
                ).fetchone()["n"]
                if count >= self._max_devices_per_agent:
                    raise TooManyDevicesError("device cap reached for this agent")

            conn.execute(
                """INSERT INTO devices
                    (id, agent_id, token, platform, environment, bundle_id,
                     notify_replies, notify_attention, notify_proactive, sound,
                     created_at, last_seen_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(token, environment, bundle_id) DO UPDATE SET
                        platform = excluded.platform,
                        notify_replies = excluded.notify_replies,
                        notify_attention = excluded.notify_attention,
                        notify_proactive = excluded.notify_proactive,
                        sound = excluded.sound,
                        last_seen_at = excluded.last_seen_at
                """,
                (
                    existing["id"] if existing else uuid.uuid4().hex,
                    agent_id,
                    token,
                    platform,
                    environment,
                    bundle_id,
                    int(bool(preferences.get("replies", True))),
                    int(bool(preferences.get("attention", True))),
                    int(bool(preferences.get("proactive", True))),
                    int(bool(preferences.get("sound", True))),
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT id FROM devices WHERE token = ? AND environment = ? AND bundle_id = ?",
                (token, environment, bundle_id),
            ).fetchone()
        if row is None:
            raise RuntimeError("device registration failed to persist")
        return str(row["id"])

    def unregister_device(self, *, agent_id: str, token: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM devices WHERE agent_id = ? AND token = ?",
                (agent_id, token),
            )

    def list_devices(self, *, agent_id: str, category: PushKind) -> list[Device]:
        col = _CATEGORY_COLUMN[category]
        with self._connect() as conn:
            # NOTE: filters to ios today. When adding Android (FCM), loosen this filter AND
            # register an "android" sender in _senders — send_event already routes by platform.
            rows = conn.execute(
                f"""SELECT token, platform, environment, bundle_id, sound
                    FROM devices
                    WHERE agent_id = ? AND platform = 'ios' AND {col} = 1""",
                (agent_id,),
            ).fetchall()
        return [
            Device(
                token=str(row["token"]),
                platform=str(row["platform"]),
                environment=str(row["environment"]),
                bundle_id=str(row["bundle_id"]),
                sound=bool(row["sound"]),
            )
            for row in rows
        ]

    def prune_device(self, *, token: str, environment: str, bundle_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM devices WHERE token = ? AND environment = ? AND bundle_id = ?",
                (token, environment, bundle_id),
            )

    def record_event(
        self,
        *,
        agent_id: str,
        kind: PushKind,
        session_id: str | None,
        device_count: int,
    ) -> str:
        event_id = uuid.uuid4().hex
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO push_events
                    (id, agent_id, type, session_id, device_count, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                (event_id, agent_id, kind, session_id, device_count, time.time()),
            )
        return event_id

    def create_challenge(self) -> str:
        challenge = secrets.token_hex(32)
        now = time.time()
        with self._connect() as conn:
            conn.execute("INSERT INTO attest_challenges (challenge, created_at) VALUES (?, ?)", (challenge, now))
            # Opportunistic GC so abandoned challenges don't accumulate (TTL ~ a few min).
            conn.execute("DELETE FROM attest_challenges WHERE created_at < ?", (now - 3600,))
        return challenge

    def consume_challenge(self, challenge: str, *, ttl_s: int) -> bool:
        now = time.time()
        with self._connect() as conn:
            # Atomic single-use: DELETE ... RETURNING removes and returns in one statement,
            # so two concurrent callers can't both observe the row.
            row = conn.execute(
                "DELETE FROM attest_challenges WHERE challenge = ? RETURNING created_at",
                (challenge,),
            ).fetchone()
            conn.execute("DELETE FROM attest_challenges WHERE created_at < ?", (now - max(ttl_s, 0) - 1,))
        if row is None:
            return False
        return (now - float(row["created_at"])) <= ttl_s


@dataclass(frozen=True)
class APNsResult:
    ok: bool
    status: int
    reason: str | None
    should_prune: bool


# APNs response reasons that mean the token is permanently dead and should be
# removed. NOTE: BadDeviceToken / DeviceTokenNotForTopic are deliberately NOT
# here — they are often a transient env/topic mismatch (e.g. a sandbox token
# hitting prod, or a stale bundle id) and must not mass-evict live devices.
_PRUNE_REASONS = {"Unregistered"}
# Reasons that indicate the relay/key is misconfigured (not a per-device fault).
# These should be loud, not silently retried per device.
_KEY_ERROR_REASONS = {"InvalidProviderToken", "BadTopic", "TopicDisallowed", "MissingTopic", "ExpiredProviderToken"}


class APNsClient:
    _REFRESH_AFTER_S = 50 * 60

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient) -> None:
        if not settings.apns_configured:
            raise RuntimeError("APNs credentials are not configured")
        self._settings = settings
        self._client = client
        self._cached_jwt: str | None = None
        self._issued_at = 0.0

    async def send(
        self,
        *,
        device: Device,
        payload: dict,
        collapse_id: str | None,
    ) -> APNsResult:
        url = f"{_apns_host(device.environment)}/3/device/{device.token}"
        headers = {
            "authorization": f"bearer {self._jwt()}",
            "apns-topic": device.bundle_id,
            "apns-push-type": "alert",
            "apns-priority": "10",
        }
        if collapse_id:
            headers["apns-collapse-id"] = collapse_id[:64]

        last: httpx.Response | None = None
        for _ in range(2):
            try:
                last = await self._client.post(url, headers=headers, json=payload)
            except httpx.HTTPError:
                continue
            if last.status_code == 200:
                return APNsResult(True, 200, None, False)
            reason = _apns_reason(last)
            if reason in _KEY_ERROR_REASONS or last.status_code == 403:
                # Misconfigured key/topic — affects every device, so log loudly
                # and stop. Do not prune (the token is fine; the key is not).
                logger.error(
                    "APNs provider/key error status=%s reason=%s topic=%s — pushes will fail until fixed",
                    last.status_code,
                    reason,
                    device.bundle_id,
                )
                return APNsResult(False, last.status_code, reason, False)
            if last.status_code == 410 and reason in _PRUNE_REASONS:
                return APNsResult(False, last.status_code, reason, True)
            if last.status_code < 500 and last.status_code != 429:
                return APNsResult(False, last.status_code, reason, False)

        status = last.status_code if last is not None else 0
        return APNsResult(False, status, _apns_reason(last) if last else "no_response", False)

    def _jwt(self) -> str:
        now = time.time()
        if self._cached_jwt is None or (now - self._issued_at) >= self._REFRESH_AFTER_S:
            self._issued_at = now
            self._cached_jwt = jwt.encode(
                {"iss": self._settings.apns_team_id, "iat": int(now)},
                self._settings.apns_key_pem,
                algorithm="ES256",
                headers={"alg": "ES256", "kid": self._settings.apns_key_id},
            )
        return self._cached_jwt


class PushService:
    def __init__(self, *, store: RelayStore, settings: Settings, apns: APNsClient | None = None) -> None:
        self._store = store
        self._settings = settings
        self._apns = apns
        self._senders: dict[str, object] = {}
        if apns is not None:
            # APNs IS the ios sender; _senders is the routing map (_apns stays the handle
            # used for the apns-configured/503 check and lifespan teardown).
            self._senders["ios"] = apns

    def sender_for(self, platform: str):
        return self._senders.get(platform)

    async def send_event(
        self,
        *,
        agent_id: str,
        kind: PushKind,
        session_id: str | None,
        title: str | None,
        body: str | None,
        source: str | None,
    ) -> dict:
        devices = self._store.list_devices(agent_id=agent_id, category=kind)
        event_id = self._store.record_event(
            agent_id=agent_id,
            kind=kind,
            session_id=session_id,
            device_count=len(devices),
        )
        if not devices:
            return {"ok": True, "event_id": event_id, "sent": 0}

        if self._apns is None and not self._senders:
            raise HTTPException(status_code=503, detail="APNs is not configured")

        push_title, push_body = _notification_copy(
            kind=kind,
            title=title,
            body=body,
            allow_custom_body=self._settings.allow_custom_body,
        )
        sent = 0
        failed = 0
        for device in devices:
            sender = self.sender_for(device.platform)
            if sender is None:
                logger.warning("no push sender for platform %s", device.platform)
                continue
            payload = _payload(
                kind=kind,
                title=push_title,
                body=push_body,
                session_id=session_id,
                sound=device.sound,
                source=source,
            )
            # No apns-collapse-id: each reply/attention/proactive message is its own
            # notification. Grouping is handled by aps "thread-id" (see _payload), so
            # messages still thread by session without coalescing into one alert.
            result = await sender.send(device=device, payload=payload, collapse_id=None)
            if result.should_prune:
                self._store.prune_device(
                    token=device.token,
                    environment=device.environment,
                    bundle_id=device.bundle_id,
                )
            if result.ok:
                sent += 1
            else:
                failed += 1
                logger.info(
                    "APNs relay send failed status=%s reason=%s env=%s topic=%s",
                    result.status,
                    result.reason,
                    device.environment,
                    device.bundle_id,
                )
        return {"ok": failed == 0, "event_id": event_id, "sent": sent, "failed": failed}


class _RateLimiter:
    """Tiny in-process token bucket. Good enough for a single-instance pilot;
    multi-instance scale needs a shared store / an edge WAF (see README)."""

    def __init__(self, per_min: int) -> None:
        self.capacity = float(max(1, per_min))
        self.refill_per_s = self.capacity / 60.0
        self._bucket_ttl_s = 300.0
        self._max_buckets = 10_000
        self._gc_interval_s = 30.0
        self._last_gc = 0.0
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def _prune_locked(self, now: float) -> None:
        cutoff = now - self._bucket_ttl_s
        self._buckets = {k: v for k, v in self._buckets.items() if v[1] >= cutoff}
        while len(self._buckets) > self._max_buckets:
            oldest_key = min(self._buckets, key=lambda k: self._buckets[k][1])
            self._buckets.pop(oldest_key, None)

    def allow(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            if now - self._last_gc >= self._gc_interval_s:
                self._prune_locked(now)
                self._last_gc = now
            tokens, last = self._buckets.get(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.refill_per_s)
            if tokens < 1.0:
                self._buckets[key] = (tokens, now)
                return False
            self._buckets[key] = (tokens - 1.0, now)
            if len(self._buckets) > self._max_buckets:
                self._prune_locked(now)
            return True


def create_app(
    *,
    settings: Settings | None = None,
    store: RelayStore | None = None,
    push_service: PushService | None = None,
    verifier=None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    store = store or RelayStore(
        settings.database_path,
        secret_pepper=settings.secret_pepper,
        max_devices_per_agent=settings.max_devices_per_agent,
    )
    push_service = push_service or PushService(store=store, settings=settings)
    rate_limiter = _RateLimiter(settings.rate_limit_per_min)
    if verifier is None and settings.require_attestation:
        if not settings.apple_app_id:
            raise RuntimeError("HERMES_RELAY_APPLE_APP_ID must be set when HERMES_RELAY_REQUIRE_ATTESTATION is true")
        from push_relay.attestation import AppAttestVerifier
        verifier = AppAttestVerifier(
            app_id=settings.apple_app_id,
            production=settings.attest_production,
            try_both_environments=settings.attest_allow_both_environments,
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # One shared HTTP/2 client + APNsClient for the whole process, so APNs
        # connections and the provider JWT are reused across the fan-out instead
        # of re-handshaking per push (which trips 429/TooManyProviderTokenUpdates).
        # Only spun up when APNs is configured (keeps the h2 dep off test paths).
        client: httpx.AsyncClient | None = None
        if settings.apns_configured and push_service._apns is None:
            client = httpx.AsyncClient(http2=True, timeout=httpx.Timeout(10.0))
            push_service._apns = APNsClient(settings, client=client)
            # APNs IS the ios sender; _senders is the routing map (_apns stays the handle
            # used for the apns-configured/503 check and lifespan teardown).
            push_service._senders["ios"] = push_service._apns
        app.state.http_client = client
        try:
            yield
        finally:
            if client is not None:
                await client.aclose()

    app = FastAPI(title="Fetch Push Relay", version="0.2.2", lifespan=lifespan)
    app.state.settings = settings
    app.state.store = store
    app.state.push_service = push_service
    app.state.rate_limiter = rate_limiter
    tunnel_registry = tunnel.TunnelRegistry()
    transit_cipher = tunnel.TransitCipher(settings.transit_key)
    app.state.tunnel_registry = tunnel_registry

    def _rate_limit(request: Request, key: str) -> None:
        ip = request.client.host if request.client else "unknown"
        if not rate_limiter.allow(f"{key}:{ip}"):
            raise HTTPException(status_code=429, detail="rate limit exceeded")

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True, "apns_configured": settings.apns_configured}

    @app.post("/v1/agents/register")
    async def register_agent(
        request: Request,
        body: RegisterAgentBody,
        x_hermes_relay_registration_token: str | None = Header(default=None),
    ) -> dict:
        _rate_limit(request, "agents-register")
        attest_key_id = None
        if settings.require_attestation:
            from push_relay.attestation import AttestationError
            if not body.attestation or not body.key_id or not body.challenge:
                raise HTTPException(status_code=400, detail="attestation required")
            if not store.consume_challenge(body.challenge, ttl_s=settings.attest_challenge_ttl_s):
                raise HTTPException(status_code=400, detail="invalid or expired challenge")
            try:
                ident = verifier.verify(platform="ios",
                                        payload={"attestation": body.attestation, "key_id": body.key_id},
                                        challenge=body.challenge)
            except AttestationError as exc:
                raise HTTPException(status_code=403, detail="attestation verification failed") from exc
            if store.count_agents_for_key(ident.key_id) >= settings.max_agents_per_attest_key:
                raise HTTPException(status_code=429, detail="enrollment cap reached for this device")
            attest_key_id = ident.key_id
        elif settings.registration_token:
            if not hmac.compare_digest(x_hermes_relay_registration_token or "", settings.registration_token):
                raise HTTPException(status_code=401, detail="Unauthorized")
        elif not settings.allow_open_registration:
            # Fail closed: no token configured and open registration not
            # explicitly allowed → don't mint anonymous identities.
            raise HTTPException(status_code=503, detail="agent registration is disabled")
        agent_id, agent_secret = store.register_agent(
            app=body.app,
            agent_version=body.agent_version,
            attest_key_id=attest_key_id,
        )
        # Mint the app-tunnel pairing capability token alongside the agent
        # credentials so the agent can hand it to a device via the setup link.
        pairing_secret = store.set_pairing(agent_id)
        return {"agent_id": agent_id, "agent_secret": agent_secret, "pairing_secret": pairing_secret}

    @app.post("/v1/agents/pairing")
    async def mint_pairing(
        request: Request, auth: AgentAuth = Depends(_require_agent)
    ) -> dict:
        """Rotate + return a fresh app-tunnel pairing token for an enrolled
        agent. ``hermes setup`` calls this to build a relay setup link when the
        agent registered before pairing capture existed (the relay keeps only the
        hash, so the original token can't be recovered — only re-minted)."""
        _rate_limit(request, "agents-pairing")
        pairing_secret = store.set_pairing(auth.agent_id)
        return {"pairing_secret": pairing_secret}

    @app.get("/v1/attest/challenge")
    async def attest_challenge(request: Request) -> dict:
        if not settings.require_attestation:
            raise HTTPException(status_code=404, detail="attestation not enabled")
        _rate_limit(request, "attest-challenge")
        return {"challenge": store.create_challenge()}

    @app.post("/v1/devices/register")
    async def register_device(
        request: Request, body: RegisterDeviceBody, auth: AgentAuth = Depends(_require_agent)
    ) -> dict:
        _rate_limit(request, "devices-register")
        if body.platform != "ios":
            raise HTTPException(status_code=400, detail="only ios devices are supported")
        if settings.allowed_bundle_ids and body.bundle_id not in settings.allowed_bundle_ids:
            raise HTTPException(status_code=400, detail="unsupported bundle id")
        try:
            device_id = store.upsert_device(
                agent_id=auth.agent_id,
                token=body.token,
                platform=body.platform,
                environment=body.environment,
                bundle_id=body.bundle_id,
                preferences=body.preferences,
            )
        except DeviceOwnershipError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except TooManyDevicesError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        return {"ok": True, "device_id": device_id}

    @app.post("/v1/devices/unregister")
    async def unregister_device(body: UnregisterDeviceBody, auth: AgentAuth = Depends(_require_agent)) -> dict:
        store.unregister_device(agent_id=auth.agent_id, token=body.token)
        return {"ok": True}

    @app.post("/v1/push/events")
    async def push_event(
        request: Request, body: PushEventBody, auth: AgentAuth = Depends(_require_agent)
    ) -> dict:
        _rate_limit(request, "push-events")
        return await push_service.send_event(
            agent_id=auth.agent_id,
            kind=body.type,
            session_id=body.session_id,
            title=body.title,
            body=body.body,
            source=body.source,
        )

    if settings.enable_tunnel:
        @app.websocket("/v1/tunnel/agent")
        async def agent_tunnel_ws(ws: WebSocket) -> None:
            await tunnel.agent_tunnel(
                ws, store=store, registry=tunnel_registry, cipher=transit_cipher,
                settings=settings, allow=rate_limiter.allow,
            )

        @app.websocket("/v1/tunnel/app")
        async def app_tunnel_ws(ws: WebSocket) -> None:
            await tunnel.app_tunnel(
                ws, store=store, registry=tunnel_registry, cipher=transit_cipher,
                settings=settings, allow=rate_limiter.allow,
            )

    return app


def _require_agent(
    request: Request,
    x_hermes_agent_id: str = Header(default=""),
    authorization: str = Header(default=""),
) -> AgentAuth:
    prefix = "Bearer "
    if not x_hermes_agent_id or not authorization.startswith(prefix):
        raise HTTPException(status_code=401, detail="Unauthorized")
    secret = authorization[len(prefix) :]
    store: RelayStore = request.app.state.store
    if not store.authenticate_agent(agent_id=x_hermes_agent_id, secret=secret):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return AgentAuth(agent_id=x_hermes_agent_id)


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _bool_env(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return _truthy(value)


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _apns_host(environment: str) -> str:
    if environment == "sandbox":
        return _SANDBOX_HOST
    if environment == "production":
        return _PROD_HOST
    raise ValueError(f"unknown APNs environment: {environment!r}")


def _apns_reason(response: httpx.Response | None) -> str | None:
    if response is None:
        return None
    try:
        data = response.json()
    except Exception:
        return None
    return data.get("reason") if isinstance(data, dict) else None


def _notification_copy(
    *,
    kind: PushKind,
    title: str | None,
    body: str | None,
    allow_custom_body: bool,
) -> tuple[str, str]:
    fallback_title, fallback_body = _GENERIC_COPY[kind]
    if not allow_custom_body:
        return fallback_title, fallback_body
    return (title or fallback_title)[:120], (body or fallback_body)[:500]


def _payload(
    *,
    kind: PushKind,
    title: str,
    body: str,
    session_id: str | None,
    sound: bool,
    source: str | None,
) -> dict:
    aps = {
        "alert": {"title": title, "body": body[:160]},
        "thread-id": session_id or "",
    }
    if kind == "attention":
        aps["interruption-level"] = "time-sensitive"
    if sound:
        aps["sound"] = "default"
    return {"aps": aps, "session_id": session_id or "", "type": kind, "source": source}


app = create_app()
