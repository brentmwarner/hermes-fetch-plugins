"""Agent-side bridge for Fetch's ephemeral computer stream.

The relay connection is outbound and authenticated with the existing Fetch
agent credentials. Each relay viewer is bridged to a fresh loopback VNC
connection (or a legacy local websockify WebSocket). Fetch never opens or
forwards to a LAN/public VNC endpoint.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import platform
import random
import socket
import uuid
from typing import Any
from urllib.parse import urlsplit

log = logging.getLogger("fetch_plugin.computer")

COMPUTER_TARGET_ENV = "HERMES_FETCH_COMPUTER_TARGET"
COMPUTER_WS_URL_ENV = "HERMES_FETCH_COMPUTER_WS_URL"
COMPUTER_NAME_ENV = "HERMES_FETCH_COMPUTER_NAME"
COMPUTER_KIND_ENV = "HERMES_FETCH_COMPUTER_KIND"

T_OPEN = "open"
T_CLOSE = "close"
T_ERROR = "error"

CID_BYTES = 16
DEFAULT_MAX_FRAME_BYTES = 8 * 1024 * 1024
_LOCAL_CONNECT_TIMEOUT_S = 5.0
_RECONNECT_CAP_S = 30.0


def _http_to_ws(url: str) -> str:
    if url.startswith("https://"):
        return "wss://" + url[len("https://"):]
    if url.startswith("http://"):
        return "ws://" + url[len("http://"):]
    return url


def validate_local_target(url: str) -> str:
    value = (url or "").strip()
    parsed = urlsplit(value)
    if parsed.scheme not in {"tcp", "ws", "wss"} or not parsed.hostname:
        raise ValueError(
            f"{COMPUTER_TARGET_ENV} must be a tcp://, ws://, or wss:// loopback URL"
        )
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise ValueError(f"{COMPUTER_TARGET_ENV} must not contain credentials or a fragment")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{COMPUTER_TARGET_ENV} has an invalid port") from exc
    if port is None:
        raise ValueError(f"{COMPUTER_TARGET_ENV} must include a port")
    if parsed.scheme == "tcp" and (parsed.path not in {"", "/"} or parsed.query):
        raise ValueError(f"{COMPUTER_TARGET_ENV} tcp targets cannot include a path or query")
    hostname = parsed.hostname.lower()
    loopback = hostname == "localhost"
    if not loopback:
        try:
            loopback = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            loopback = False
    if not loopback:
        raise ValueError(f"{COMPUTER_TARGET_ENV} must point to localhost/loopback")
    return value


def validate_local_ws_url(url: str) -> str:
    """Backward-compatible validator kept for third-party plugin integrations."""
    value = validate_local_target(url)
    if urlsplit(value).scheme not in {"ws", "wss"}:
        raise ValueError(f"{COMPUTER_WS_URL_ENV} must be a ws:// or wss:// loopback URL")
    return value


def _header_value(value: str, *, fallback: str) -> str:
    clean = " ".join(value.replace("\r", " ").replace("\n", " ").split())
    clean = clean.encode("ascii", "ignore").decode("ascii")
    return (clean or fallback)[:80]


def default_computer_platform() -> str:
    return {
        "Darwin": "macOS",
        "Linux": "Linux",
        "Windows": "Windows",
    }.get(platform.system(), platform.system() or "Computer")


def default_computer_kind() -> str:
    return {
        "Darwin": "Mac desktop",
        "Linux": "Linux desktop",
        "Windows": "Windows desktop",
    }.get(platform.system(), "Computer")


def _cid_bytes(cid: str) -> bytes:
    return uuid.UUID(hex=cid).bytes


def _cid_from_bytes(data: bytes) -> str:
    return uuid.UUID(bytes=data[:CID_BYTES]).hex


def _ws_connect(
    url: str,
    headers: dict[str, str] | None = None,
    *,
    subprotocols: list[str] | None = None,
    max_size: int | None = None,
) -> Any:
    """Return an awaitable WebSocket across websockets 12–16."""
    try:
        from websockets.asyncio.client import connect

        kwargs: dict[str, object] = {"additional_headers": headers} if headers else {}
        if subprotocols is not None:
            kwargs["subprotocols"] = subprotocols
        if max_size is not None:
            kwargs["max_size"] = max_size
        return connect(url, **kwargs)
    except ImportError:
        import websockets

        kwargs = {"extra_headers": headers} if headers else {}
        if subprotocols is not None:
            kwargs["subprotocols"] = subprotocols
        if max_size is not None:
            kwargs["max_size"] = max_size
        return websockets.connect(url, **kwargs)


class _LocalSession:
    def __init__(self, conn) -> None:
        self.conn = conn
        self.pump_task: asyncio.Task[None] | None = None

    async def send(self, data: bytes) -> None:
        await self.conn.send(data)

    async def close(self) -> None:
        if self.pump_task is not None and self.pump_task is not asyncio.current_task():
            self.pump_task.cancel()
        try:
            await self.conn.close()
        except Exception:
            log.debug("Fetch could not close the local WebSocket cleanly", exc_info=True)


class _TCPConnection:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        read_size: int,
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.read_size = read_size

    async def send(self, data: bytes) -> None:
        self.writer.write(data)
        await self.writer.drain()

    async def close(self) -> None:
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except Exception:
            log.debug("Fetch could not close the local VNC socket cleanly", exc_info=True)

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        data = await self.reader.read(self.read_size)
        if not data:
            raise StopAsyncIteration
        return data


class AgentComputer:
    def __init__(
        self,
        *,
        relay_url: str,
        agent_id: str,
        agent_secret: str,
        local_target: str,
        computer_name: str | None = None,
        computer_platform: str | None = None,
        computer_kind: str | None = None,
        max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
        relay_connect=None,
        local_connect=None,
    ) -> None:
        self.relay_ws_url = _http_to_ws(relay_url).rstrip("/") + "/v1/computer/agent"
        self.agent_id = agent_id
        self.agent_secret = agent_secret
        self.local_target = validate_local_target(local_target)
        self.computer_name = _header_value(
            computer_name or socket.gethostname(),
            fallback="Computer",
        )
        self.computer_platform = _header_value(
            computer_platform or default_computer_platform(),
            fallback="Computer",
        )
        self.computer_kind = _header_value(
            computer_kind or default_computer_kind(),
            fallback="Computer",
        )
        self.max_frame_bytes = max(1024, max_frame_bytes)
        self._relay_connect = relay_connect or self._default_relay_connect
        self._local_connect = local_connect or self._default_local_connect
        self._sessions: dict[str, _LocalSession] = {}
        self._send_lock = asyncio.Lock()
        self._stop = False

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "X-Hermes-Agent-Id": self.agent_id,
            "Authorization": f"Bearer {self.agent_secret}",
            "X-Fetch-Computer-Name": self.computer_name,
            "X-Fetch-Computer-Platform": self.computer_platform,
            "X-Fetch-Computer-Kind": self.computer_kind,
        }

    def stop(self) -> None:
        self._stop = True

    async def run_forever(self) -> None:
        backoff = 0.5
        while not self._stop:
            try:
                await self._serve_once()
                backoff = 0.5
            except asyncio.CancelledError:
                raise
            except Exception:
                log.debug("Fetch computer stream ended; will retry", exc_info=True)
            if self._stop:
                break
            delay = min(_RECONNECT_CAP_S, backoff)
            await asyncio.sleep(delay + random.uniform(0, delay * 0.25))
            backoff = min(_RECONNECT_CAP_S, backoff * 2)

    async def _serve_once(self) -> None:
        relay = await self._relay_connect(self.relay_ws_url, self._headers)
        try:
            async for raw in relay:
                if isinstance(raw, bytes):
                    await self._dispatch_bytes(relay, raw)
                    continue
                frame = self._decode_control(raw)
                if frame is None:
                    raise ValueError("relay sent an invalid computer control frame")
                await self._dispatch_control(relay, frame)
        finally:
            await self._close_all_sessions()
            try:
                await relay.close()
            except Exception:
                log.debug("Fetch could not close the relay socket cleanly", exc_info=True)

    async def _dispatch_control(self, relay, frame: dict[str, object]) -> None:
        frame_type = frame.get("t")
        cid = frame.get("cid")
        if not isinstance(cid, str) or not self._valid_cid(cid):
            raise ValueError("relay sent an invalid computer cid")
        if frame_type == T_OPEN:
            await self._ensure_session(relay, cid)
        elif frame_type == T_CLOSE:
            await self._close_session(cid)
        else:
            raise ValueError("relay sent an unknown computer control frame")

    async def _dispatch_bytes(self, relay, data: bytes) -> None:
        if len(data) <= CID_BYTES or len(data) > self.max_frame_bytes + CID_BYTES:
            raise ValueError("relay sent an invalid computer data frame")
        cid = _cid_from_bytes(data)
        session = self._sessions.get(cid)
        if session is None:
            await self._send_control(
                relay,
                {"t": T_ERROR, "cid": cid, "reason": "session_unavailable"},
            )
            return
        try:
            await session.send(data[CID_BYTES:])
        except Exception:
            await self._close_session(cid)
            await self._send_control(
                relay,
                {"t": T_ERROR, "cid": cid, "reason": "local_unavailable"},
            )

    async def _ensure_session(self, relay, cid: str) -> _LocalSession | None:
        existing = self._sessions.get(cid)
        if existing is not None:
            return existing
        try:
            conn = await asyncio.wait_for(
                self._local_connect(self.local_target),
                timeout=_LOCAL_CONNECT_TIMEOUT_S,
            )
        except Exception:
            log.debug("Fetch could not open local computer stream", exc_info=True)
            await self._send_control(
                relay,
                {"t": T_ERROR, "cid": cid, "reason": "local_unavailable"},
            )
            return None
        session = _LocalSession(conn)
        self._sessions[cid] = session
        session.pump_task = asyncio.create_task(self._pump_local(relay, cid, session))
        return session

    async def _pump_local(self, relay, cid: str, session: _LocalSession) -> None:
        try:
            async for raw in session.conn:
                if not isinstance(raw, bytes) or not raw or len(raw) > self.max_frame_bytes:
                    await self._send_control(
                        relay,
                        {"t": T_ERROR, "cid": cid, "reason": "invalid_local_frame"},
                    )
                    return
                await self._send_bytes(relay, _cid_bytes(cid) + raw)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug("Fetch local computer stream ended", exc_info=True)
        finally:
            if self._sessions.get(cid) is session:
                self._sessions.pop(cid, None)
                await self._send_control(relay, {"t": T_CLOSE, "cid": cid})
            await session.close()

    async def _send_bytes(self, relay, data: bytes) -> None:
        async with self._send_lock:
            await relay.send(data)

    async def _send_control(self, relay, frame: dict[str, object]) -> None:
        payload = json.dumps(frame, separators=(",", ":"))
        async with self._send_lock:
            await relay.send(payload)

    async def _close_session(self, cid: str) -> None:
        session = self._sessions.pop(cid, None)
        if session is not None:
            await session.close()

    async def _close_all_sessions(self) -> None:
        for cid in list(self._sessions):
            await self._close_session(cid)

    @staticmethod
    def _decode_control(raw: object) -> dict[str, object] | None:
        if not isinstance(raw, str):
            return None
        try:
            frame = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
        return frame if isinstance(frame, dict) else None

    @staticmethod
    def _valid_cid(cid: str) -> bool:
        try:
            return uuid.UUID(hex=cid).hex == cid.lower()
        except (ValueError, AttributeError):
            return False

    async def _default_relay_connect(self, url: str, headers: dict[str, str]):
        return await _ws_connect(
            url,
            headers,
            max_size=self.max_frame_bytes + CID_BYTES,
        )

    async def _default_local_connect(self, url: str):
        parsed = urlsplit(url)
        if parsed.scheme in {"ws", "wss"}:
            return await _ws_connect(
                url,
                subprotocols=["binary"],
                max_size=self.max_frame_bytes,
            )
        if parsed.scheme != "tcp" or parsed.hostname is None or parsed.port is None:
            raise ValueError(f"{COMPUTER_TARGET_ENV} is invalid")
        reader, writer = await asyncio.open_connection(parsed.hostname, parsed.port)
        return _TCPConnection(
            reader,
            writer,
            read_size=min(self.max_frame_bytes, 64 * 1024),
        )
