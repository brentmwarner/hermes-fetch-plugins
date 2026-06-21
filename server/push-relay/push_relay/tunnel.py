"""Reverse-tunnel broker.

The relay is a content-blind pipe. The agent holds ONE persistent outbound
WebSocket (``/v1/tunnel/agent``); each app device holds its own
(``/v1/tunnel/app``). Frames are forwarded between them keyed by a relay-minted
connection id (``cid``); the app correlates its own request ids (``sid``)
itself, as the existing ``tui_gateway`` client already does.

Auth: the agent uplink presents its ``agent_secret``; the app downlink presents
a SEPARATE per-agent **pairing secret** (a capability token, never the
``agent_id``) as a bearer header. Both routes are gated behind a feature flag
(``HERMES_RELAY_ENABLE_TUNNEL``) so the relay never registers an app channel
until the iOS cutover is ready.

The only state the relay keeps is an in-memory routing map (apps tracked per
agent_id so they survive an agent reconnect) plus a short-lived,
delete-after-send transit buffer so a chat message sent to a sleeping agent
survives until it reconnects. The relay brokers and forwards but never becomes a
durable data custodian.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from dataclasses import dataclass, field

from fastapi import WebSocket, WebSocketDisconnect

log = logging.getLogger("push_relay.tunnel")

# Frame types (the envelope's "t").
T_REST_REQ = "rest-req"
T_REST_RESP = "rest-resp"
T_WS_OPEN = "ws-open"
T_WS_FRAME = "ws-frame"
T_WS_CLOSE = "ws-close"
T_BUFFERED = "buffered"
T_AGENT_OFFLINE = "agent-offline"
T_DRAIN_BEGIN = "buffer-drain-begin"
T_DRAIN_END = "buffer-drain-end"

# Only these app→agent intents survive a closed agent (store-and-forward). Live
# reads fail fast with "agent-offline" rather than queuing a stale read.
BUFFERABLE_WS_METHODS = frozenset({"prompt.submit", "session.steer", "session.create"})

WS_CLOSE_UNAUTHORIZED = 4401
WS_CLOSE_RATE_LIMITED = 4429
WS_CLOSE_TOO_MANY = 4409


@dataclass
class _Conn:
    """A WebSocket with a send lock so concurrent senders (e.g. several app
    coroutines writing the one agent uplink) can't interleave/corrupt frames."""

    ws: WebSocket
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send(self, data: dict) -> bool:
        async with self.lock:
            try:
                await self.ws.send_json(data)
                return True
            except Exception:
                return False


class TunnelRegistry:
    """In-memory routing. Apps are tracked per agent_id INDEPENDENTLY of the
    uplink, so a continuously-connected app survives an agent reconnect (its cid
    keeps routing). Single-instance only (matches today's deploy)."""

    def __init__(self) -> None:
        self._uplinks: dict[str, _Conn] = {}
        self._apps: dict[str, dict[str, _Conn]] = {}

    def uplink(self, agent_id: str) -> _Conn | None:
        return self._uplinks.get(agent_id)

    def set_uplink(self, agent_id: str, conn: _Conn) -> _Conn | None:
        old = self._uplinks.get(agent_id)
        self._uplinks[agent_id] = conn
        return old

    def clear_uplink(self, agent_id: str, conn: _Conn) -> None:
        if self._uplinks.get(agent_id) is conn:
            del self._uplinks[agent_id]

    def app_cids(self, agent_id: str) -> list[str]:
        return list(self._apps.get(agent_id, {}).keys())

    def app(self, agent_id: str, cid: str) -> _Conn | None:
        return self._apps.get(agent_id, {}).get(cid)

    def add_app(self, agent_id: str, cid: str, conn: _Conn, cap: int) -> bool:
        apps = self._apps.setdefault(agent_id, {})
        if cid not in apps and len(apps) >= cap:
            return False
        apps[cid] = conn
        return True

    def remove_app(self, agent_id: str, cid: str) -> None:
        apps = self._apps.get(agent_id)
        if apps is not None:
            apps.pop(cid, None)
            if not apps:
                self._apps.pop(agent_id, None)


class TransitCipher:
    """AES-GCM at-rest encryption for buffered frames when a key is configured;
    a transparent pass-through otherwise (TLS-only transit + fast eviction)."""

    def __init__(self, key: str | None) -> None:
        self._aead = None
        if key:
            import hashlib

            from cryptography.hazmat.primitives.ciphers.aead import AESGCM

            self._aead = AESGCM(hashlib.sha256(key.encode("utf-8")).digest())

    def encrypt(self, data: bytes) -> bytes:
        if self._aead is None:
            return data
        nonce = os.urandom(12)
        return nonce + self._aead.encrypt(nonce, data, None)

    def decrypt(self, blob: bytes) -> bytes:
        if self._aead is None:
            return blob
        return self._aead.decrypt(blob[:12], blob[12:], None)


def _is_bufferable(frame: dict) -> bool:
    return frame.get("t") == T_WS_FRAME and frame.get("method") in BUFFERABLE_WS_METHODS


def _bearer(authorization: str) -> str:
    prefix = "Bearer "
    return authorization[len(prefix):] if authorization.startswith(prefix) else ""


def _client_ip(ws: WebSocket) -> str:
    return ws.client.host if ws.client else "unknown"


async def agent_tunnel(ws: WebSocket, *, store, registry: TunnelRegistry, cipher: TransitCipher, settings, allow) -> None:
    """Agent uplink: authenticate with agent credentials, announce any apps
    already waiting, drain the transit buffer (delete-after-send), then pump
    agent→app responses keyed by cid."""
    if not allow(f"tunnel-agent:{_client_ip(ws)}"):
        await ws.close(code=WS_CLOSE_RATE_LIMITED)
        return
    agent_id = ws.headers.get("x-hermes-agent-id", "")
    secret = _bearer(ws.headers.get("authorization", ""))
    if not agent_id or not secret or not await asyncio.to_thread(
        store.authenticate_agent, agent_id=agent_id, secret=secret
    ):
        await ws.close(code=WS_CLOSE_UNAUTHORIZED)
        return

    await ws.accept()
    conn = _Conn(ws=ws)
    old = registry.set_uplink(agent_id, conn)
    if old is not None:
        try:
            await old.ws.close()
        except Exception:
            pass

    try:
        # Apps that connected while the agent was down are still routed (apps are
        # keyed per agent_id, not on the uplink). Tell the agent to (re)open a
        # session for each so it can route replies back on those cids.
        for cid in registry.app_cids(agent_id):
            await conn.send({"t": T_WS_OPEN, "cid": cid})

        rows = await asyncio.to_thread(store.peek_transit, agent_id)  # [(id, blob)]
        if rows:
            await conn.send({"t": T_DRAIN_BEGIN, "count": len(rows)})
            for rid, blob in rows:
                try:
                    frame = json.loads(cipher.decrypt(blob).decode("utf-8"))
                except Exception:
                    log.warning("transit frame undecodable (agent=%s id=%s); dropping", agent_id, rid)
                    await asyncio.to_thread(store.delete_transit, [rid])
                    continue
                if await conn.send(frame):
                    await asyncio.to_thread(store.delete_transit, [rid])
                else:
                    break  # uplink dropped mid-drain → remaining frames stay buffered
            await conn.send({"t": T_DRAIN_END})

        while True:
            try:
                frame = await ws.receive_json()
            except WebSocketDisconnect:
                break
            except Exception:
                continue  # malformed/binary/scalar frame: skip, never tear down the uplink
            cid = frame.get("cid")
            app = registry.app(agent_id, cid) if cid else None
            if app is not None and not await app.send(frame):
                registry.remove_app(agent_id, cid)
    finally:
        registry.clear_uplink(agent_id, conn)


async def app_tunnel(ws: WebSocket, *, store, registry: TunnelRegistry, cipher: TransitCipher, settings, allow) -> None:
    """App downlink: authenticate with the per-agent pairing capability token,
    then stamp each frame with this connection's cid and forward it to the
    agent. When the agent is offline, buffer chat-sends and fail-fast the rest."""
    if not allow(f"tunnel-app:{_client_ip(ws)}"):
        await ws.close(code=WS_CLOSE_RATE_LIMITED)
        return
    # Auth is a SECRET pairing token (bearer header), NOT the routing agent_id.
    agent_id = ws.headers.get("x-hermes-agent-id", "")
    pairing = _bearer(ws.headers.get("authorization", ""))
    if not agent_id or not pairing or not await asyncio.to_thread(
        store.authenticate_pairing, agent_id=agent_id, pairing_secret=pairing
    ):
        await ws.close(code=WS_CLOSE_UNAUTHORIZED)
        return

    await ws.accept()
    cid = uuid.uuid4().hex
    conn = _Conn(ws=ws)
    if not registry.add_app(agent_id, cid, conn, settings.max_apps_per_agent):
        await ws.close(code=WS_CLOSE_TOO_MANY)
        return

    try:
        uplink = registry.uplink(agent_id)
        if uplink is not None:
            await uplink.send({"t": T_WS_OPEN, "cid": cid})

        while True:
            try:
                frame = await ws.receive_json()
            except WebSocketDisconnect:
                break
            except Exception:
                continue
            frame["cid"] = cid
            uplink = registry.uplink(agent_id)   # re-check: the agent may have dropped
            if uplink is not None and await uplink.send(frame):
                continue
            await _offline_or_buffer(conn, frame, store=store, cipher=cipher, agent_id=agent_id, settings=settings)
    finally:
        registry.remove_app(agent_id, cid)
        uplink = registry.uplink(agent_id)
        if uplink is not None:
            await uplink.send({"t": T_WS_CLOSE, "cid": cid})


async def _offline_or_buffer(conn: _Conn, frame: dict, *, store, cipher: TransitCipher, agent_id: str, settings) -> None:
    sid = frame.get("sid")
    if _is_bufferable(frame):
        ok = await asyncio.to_thread(
            store.enqueue_transit,
            agent_id=agent_id,
            payload=cipher.encrypt(json.dumps(frame).encode("utf-8")),
            ttl_s=settings.transit_ttl_s,
            max_per_agent=settings.max_transit_per_agent,
            max_bytes=settings.max_transit_bytes_per_agent,
        )
        await conn.send({"t": T_BUFFERED if ok else T_AGENT_OFFLINE, "sid": sid,
                         "reason": None if ok else "transit full"})
    else:
        await conn.send({"t": T_AGENT_OFFLINE, "sid": sid})
