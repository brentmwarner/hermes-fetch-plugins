"""Agent-side bridge for Fetch's ephemeral computer stream.

The relay connection is outbound and authenticated with the existing Fetch
agent credentials. Each relay viewer is bridged to a fresh loopback VNC
connection (or a legacy local websockify WebSocket). Fetch never opens or
forwards to a LAN/public VNC endpoint.
"""

from __future__ import annotations

import asyncio
import importlib.util
import ipaddress
import json
import logging
import platform
import random
import socket
import sys
import uuid
from pathlib import Path
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

_RFB_VERSION_38 = b"RFB 003.008\n"
_RFB_SECURITY_NONE = 1
_RFB_SECURITY_VNC_AUTH = 2
_RFB_SECURITY_ARD = 30


class VNCSetupError(RuntimeError):
    """The loopback VNC server is reachable but not safely configured."""


def _load_vnc_auth_module():
    name = "fetch_plugin_vnc_auth"
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    path = Path(__file__).resolve().parent / "_vnc_auth.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise VNCSetupError("Fetch could not load local VNC authentication support")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


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

    async def readexactly(self, size: int) -> bytes:
        return await self.reader.readexactly(size)

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


class _PreauthenticatedVNCConnection:
    """Present a no-auth RFB 3.8 handshake after host-side authentication."""

    def __init__(self, conn: _TCPConnection) -> None:
        self.conn = conn
        self._viewer_input = bytearray()
        self._viewer_state = "version"
        self._initial_banner_pending = True
        self._responses: asyncio.Queue[bytes] = asyncio.Queue()

    async def send(self, data: bytes) -> None:
        if not isinstance(data, bytes) or not data:
            raise VNCSetupError("The viewer sent an invalid RFB frame")
        self._viewer_input.extend(data)
        while self._viewer_input:
            if self._viewer_state == "version":
                if len(self._viewer_input) < 12:
                    return
                version = bytes(self._viewer_input[:12])
                del self._viewer_input[:12]
                if not _valid_rfb_banner(version):
                    raise VNCSetupError("The viewer sent an invalid RFB version")
                self._viewer_state = "security"
                self._responses.put_nowait(bytes((_RFB_SECURITY_NONE, _RFB_SECURITY_NONE)))
                continue
            if self._viewer_state == "security":
                security_type = self._viewer_input.pop(0)
                if security_type != _RFB_SECURITY_NONE:
                    raise VNCSetupError("The viewer rejected the private RFB session")
                self._viewer_state = "proxy"
                self._responses.put_nowait(b"\0\0\0\0")
                continue
            payload = bytes(self._viewer_input)
            self._viewer_input.clear()
            await self.conn.send(payload)

    async def close(self) -> None:
        await self.conn.close()

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        if self._initial_banner_pending:
            self._initial_banner_pending = False
            return _RFB_VERSION_38
        if self._viewer_state != "proxy" or not self._responses.empty():
            return await self._responses.get()
        return await self.conn.__anext__()


def _valid_rfb_banner(value: bytes) -> bool:
    if len(value) != 12 or not value.startswith(b"RFB ") or not value.endswith(b"\n"):
        return False
    try:
        major, minor = value[4:11].decode("ascii").split(".", maxsplit=1)
        return len(major) == 3 and len(minor) == 3 and major.isdigit() and minor.isdigit()
    except (UnicodeDecodeError, ValueError):
        return False


def _rfb_version(value: bytes) -> tuple[int, int]:
    if not _valid_rfb_banner(value):
        raise VNCSetupError("The local computer did not provide a valid RFB version")
    major, minor = value[4:11].decode("ascii").split(".", maxsplit=1)
    return int(major), int(minor)


async def _rfb_failure_reason(conn: _TCPConnection) -> str:
    try:
        length = int.from_bytes(await conn.readexactly(4), "big")
        if length <= 0 or length > 4096:
            return "authentication rejected"
        value = await conn.readexactly(length)
        return value.decode("utf-8", errors="replace")[:200]
    except (asyncio.IncompleteReadError, OSError):
        return "authentication rejected"


async def _authenticate_local_vnc(
    conn: _TCPConnection,
    password: str,
) -> _PreauthenticatedVNCConnection:
    banner = await conn.readexactly(12)
    major, minor = _rfb_version(banner)
    if major != 3 or minor < 3:
        raise VNCSetupError(f"Unsupported local RFB version {major}.{minor}")
    await conn.send(banner)

    security_type: int
    if minor == 3:
        security_type = int.from_bytes(await conn.readexactly(4), "big")
        if security_type == 0:
            reason = await _rfb_failure_reason(conn)
            raise VNCSetupError(f"The local VNC server refused access: {reason}")
        if security_type not in {_RFB_SECURITY_NONE, _RFB_SECURITY_VNC_AUTH}:
            raise VNCSetupError(
                f"The local VNC server requires unsupported security type {security_type}"
            )
    else:
        count = (await conn.readexactly(1))[0]
        if count == 0:
            reason = await _rfb_failure_reason(conn)
            raise VNCSetupError(f"The local VNC server refused access: {reason}")
        offered = set(await conn.readexactly(count))
        if _RFB_SECURITY_VNC_AUTH in offered and password:
            security_type = _RFB_SECURITY_VNC_AUTH
        elif _RFB_SECURITY_NONE in offered:
            security_type = _RFB_SECURITY_NONE
        elif _RFB_SECURITY_VNC_AUTH in offered:
            raise VNCSetupError(
                "The local VNC password is not configured in Fetch computer setup"
            )
        elif _RFB_SECURITY_ARD in offered:
            raise VNCSetupError(
                "macOS Screen Sharing is using account authentication. Enable "
                "'VNC viewers may control screen with password' and rerun Fetch computer setup."
            )
        else:
            offered_label = ", ".join(str(value) for value in sorted(offered))
            raise VNCSetupError(
                f"The local VNC server requires unsupported security types: {offered_label}"
            )
        await conn.send(bytes((security_type,)))

    if security_type == _RFB_SECURITY_VNC_AUTH:
        if not password:
            raise VNCSetupError(
                "The local VNC password is not configured in Fetch computer setup"
            )
        challenge = await conn.readexactly(16)
        response = _load_vnc_auth_module().challenge_response(password, challenge)
        await conn.send(response)

    # RFC 6143 Appendix A.2: RFB 3.7 None skips SecurityResult and goes
    # straight to ClientInit/ServerInit. VNC Authentication still sends it.
    # RFB 3.8 always sends SecurityResult, including for None.
    expects_result = security_type == _RFB_SECURITY_VNC_AUTH or minor >= 8
    if expects_result:
        result = int.from_bytes(await conn.readexactly(4), "big")
        if result != 0:
            reason = await _rfb_failure_reason(conn) if minor >= 8 else "authentication rejected"
            raise VNCSetupError(f"The local VNC server rejected its saved password: {reason}")
    return _PreauthenticatedVNCConnection(conn)


async def open_local_vnc(
    target: str,
    *,
    password: str = "",
    read_size: int = 64 * 1024,
) -> _PreauthenticatedVNCConnection:
    parsed = urlsplit(validate_local_target(target))
    if parsed.scheme != "tcp" or parsed.hostname is None or parsed.port is None:
        raise VNCSetupError("Automatic local authentication requires a tcp:// VNC target")
    reader, writer = await asyncio.open_connection(parsed.hostname, parsed.port)
    conn = _TCPConnection(reader, writer, read_size=read_size)
    try:
        return await _authenticate_local_vnc(conn, password)
    except BaseException:
        await conn.close()
        raise


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
        vnc_password: str | None = None,
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
        self.vnc_password = vnc_password or ""
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
        return await open_local_vnc(
            url,
            password=self.vnc_password,
            read_size=min(self.max_frame_bytes, 64 * 1024),
        )
