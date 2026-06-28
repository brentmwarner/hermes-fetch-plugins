"""Agent-side reverse-tunnel client.

Holds ONE persistent outbound WebSocket from the agent host to the Fetch relay
(``/v1/tunnel/agent``) so the phone can reach this NAT'd agent with no inbound
port / no Tailscale. The relay forwards app requests over this socket; the client
serves each against the agent's own local dashboard (``127.0.0.1:9119``) and
streams the response back. The relay never sees plaintext beyond TLS transit.

Frame envelope (matches ``push_relay/tunnel.py``): ``{t, cid, sid, ...}``.
  * rest-req  → HTTP to the local dashboard; reply rest-resp (text body, or
                base64 ``body`` with ``body_b64=true`` for binary media).
  * ws-open   → open a local ``/api/ws`` session for this cid; its first frame
                (``gateway.ready``) and every subsequent frame pump back as
                ws-frame.
  * ws-frame  → forward the carried JSON-RPC (``data``) to the local ws session;
                lazily open one if none exists yet (a drained buffered send).
  * ws-close  → close the local session.

Auth reuses the existing anonymous agent_id + agent_secret (no new credentials).
Reconnect backoff is capped BELOW the dashboard's ~20s ws-orphan grace so a
tunnel blip reattaches a parked chat session instead of reaping it.

Loaded by file path from ``__init__.py`` (same mechanism as ``_relay.py``); the
network transports are injectable so the dispatch logic is unit-testable without
a live dashboard.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import random
import re
import time
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("fetch_plugin.tunnel")

T_REST_REQ = "rest-req"
T_REST_RESP = "rest-resp"
T_WS_OPEN = "ws-open"
T_WS_FRAME = "ws-frame"
T_WS_CLOSE = "ws-close"

DEFAULT_DASHBOARD = "http://127.0.0.1:9119"
_RECONNECT_CAP_S = 5.0  # < dashboard HERMES_TUI_WS_ORPHAN_REAP_GRACE_S (~20s)
_UNHEALTHY_RECONNECT_CAP_S = 30.0
_LOOP_WINDOW_S = 30.0
_LOOP_THRESHOLD = 6

# Content types streamed back as a UTF-8 string; everything else is base64'd.
_TEXTUAL_PREFIXES = ("text/", "application/json", "application/javascript", "application/xml")


def _http_to_ws(url: str) -> str:
    if url.startswith("https://"):
        return "wss://" + url[len("https://"):]
    if url.startswith("http://"):
        return "ws://" + url[len("http://"):]
    return url


def _is_textual(content_type: str) -> bool:
    ct = (content_type or "").lower()
    return any(ct.startswith(p) for p in _TEXTUAL_PREFIXES)


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _safe_lock_name(agent_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", agent_id).strip("-")
    return safe or "unknown"


class TunnelOwnerLock:
    """Cross-process ownership guard for one agent's local Fetch tunnel."""

    def __init__(self, *, agent_id: str, lock_dir: str | Path) -> None:
        self.agent_id = agent_id
        self.path = Path(lock_dir) / f"fetch-tunnel-{_safe_lock_name(agent_id)}.pid"
        self.owner_pid: int | None = None
        self.acquired = False

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                status = self.status()
                owner = status["owner_pid"]
                if status["state"] == "owned":
                    self.owner_pid = owner
                    return False
                log.info(
                    "Fetch tunnel owner lock is %s for agent %s (pid=%s path=%s); reclaiming",
                    status["state"],
                    self.agent_id,
                    owner or "unknown",
                    self.path,
                )
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    self.owner_pid = owner
                    return False
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(str(os.getpid()))
            self.owner_pid = os.getpid()
            self.acquired = True
            return True

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            if self._read_owner() == os.getpid():
                self.path.unlink()
        except OSError:
            pass
        self.acquired = False

    def _read_owner(self) -> int | None:
        try:
            return int(self.path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None

    def status(self) -> dict[str, Any]:
        try:
            raw = self.path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            owner_pid = None
            state = "unowned"
            owner_alive = False
        except OSError as exc:
            return {
                "state": "unreadable",
                "agent_id": self.agent_id,
                "path": str(self.path),
                "owner_pid": None,
                "owner_alive": False,
                "owner_current_process": False,
                "error": str(exc),
                "meaning": "Fetch could not inspect the local tunnel-owner lock file.",
            }
        else:
            try:
                owner_pid = int(raw)
            except ValueError:
                owner_pid = None
                state = "invalid"
                owner_alive = False
            else:
                owner_alive = _process_alive(owner_pid)
                state = "owned" if owner_alive else "stale"

        owner_current_process = owner_pid == os.getpid()
        if state == "owned" and not owner_current_process:
            meaning = (
                "Another local Hermes process owns this agent's single relay uplink. "
                "That is expected when several Fetch app clients share one Hermes agent; "
                "it is not a device or app-client limit."
            )
        elif state == "owned":
            meaning = "This process owns the single relay uplink for the agent."
        elif state == "stale":
            meaning = "The previous tunnel owner process is gone; the next tunnel start can reclaim this lock."
        elif state == "invalid":
            meaning = "The tunnel-owner lock file is corrupt; the next tunnel start can replace it."
        else:
            meaning = "No local process currently owns the relay uplink for this agent."

        return {
            "state": state,
            "agent_id": self.agent_id,
            "path": str(self.path),
            "owner_pid": owner_pid,
            "owner_alive": owner_alive,
            "owner_current_process": owner_current_process,
            "meaning": meaning,
        }


class _LoopHealth:
    def __init__(self, *, window_s: float = _LOOP_WINDOW_S, threshold: int = _LOOP_THRESHOLD) -> None:
        self.window_s = window_s
        self.threshold = threshold
        self.events: list[float] = []
        self._reason: str | None = None

    def _prune(self) -> None:
        cutoff = time.monotonic() - self.window_s
        self.events = [t for t in self.events if t >= cutoff]

    def record(self, *, reason: str) -> bool:
        self._reason = reason
        self._prune()
        self.events.append(time.monotonic())
        return len(self.events) >= self.threshold

    @property
    def unhealthy(self) -> bool:
        # Derive health from the live event window so the loop recovers once a
        # transient reconnect burst ages out — never a sticky one-shot latch.
        self._prune()
        return len(self.events) >= self.threshold

    @property
    def unhealthy_reason(self) -> str | None:
        return self._reason if self.unhealthy else None


def _jittered_delay(base: float, *, unhealthy: bool = False) -> float:
    cap = _UNHEALTHY_RECONNECT_CAP_S if unhealthy else _RECONNECT_CAP_S
    bounded = min(cap, base)
    return bounded + random.uniform(0, bounded * 0.25)


def _ws_connect(url: str, headers: dict[str, str] | None = None) -> Any:
    """Return an awaitable WebSocket connection compatible with websockets 12–15.

    websockets ≥ 14 ships ``websockets.asyncio.client`` and uses
    ``additional_headers``; older releases expose only the top-level
    ``websockets.connect`` which accepts ``extra_headers``.
    """
    try:
        from websockets.asyncio.client import connect  # websockets ≥ 14
        kw: dict = {"additional_headers": headers} if headers else {}
        return connect(url, **kw)
    except ImportError:
        import websockets  # websockets ≤ 13
        kw = {"extra_headers": headers} if headers else {}
        return websockets.connect(url, **kw)


class _LocalSession:
    """One local ``/api/ws`` connection bridging a single app cid."""

    def __init__(self, conn) -> None:
        self.conn = conn
        self.pump_task: asyncio.Task | None = None

    async def send(self, text: str) -> None:
        await self.conn.send(text)

    async def close(self) -> None:
        if self.pump_task is not None:
            self.pump_task.cancel()
        try:
            await self.conn.close()
        except Exception:
            pass


class AgentTunnel:
    def __init__(
        self,
        *,
        relay_url: str,
        agent_id: str,
        agent_secret: str,
        dashboard_base: str = DEFAULT_DASHBOARD,
        dashboard_token: str | None = None,
        relay_connect=None,
        local_ws_connect=None,
        http_client_factory=None,
    ) -> None:
        self.relay_ws_url = _http_to_ws(relay_url).rstrip("/") + "/v1/tunnel/agent"
        self.agent_id = agent_id
        self.agent_secret = agent_secret
        self.dashboard_base = dashboard_base.rstrip("/")
        self.dashboard_token = dashboard_token
        self._sessions: dict[str, _LocalSession] = {}
        self._send_lock = asyncio.Lock()
        self._stop = False
        self._reconnect_health = _LoopHealth()
        self._local_ws_health = _LoopHealth()
        # Injectable transports (production defaults below).
        self._relay_connect = relay_connect or self._default_relay_connect
        self._local_ws_connect = local_ws_connect or self._default_local_ws_connect
        self._http_client_factory = http_client_factory or (lambda: httpx.AsyncClient(timeout=30.0))

    @property
    def _headers(self) -> dict[str, str]:
        return {"X-Hermes-Agent-Id": self.agent_id, "Authorization": f"Bearer {self.agent_secret}"}

    # --- lifecycle ---

    def stop(self) -> None:
        self._stop = True

    def health_snapshot(self) -> dict[str, Any]:
        reasons = [
            r for r in [
                self._reconnect_health.unhealthy_reason,
                self._local_ws_health.unhealthy_reason,
            ]
            if r
        ]
        return {
            "ok": not reasons,
            "state": "unhealthy" if reasons else "healthy",
            "reasons": reasons,
            "open_sessions": len(self._sessions),
        }

    async def run_forever(self) -> None:
        backoff = 0.25
        while not self._stop:
            try:
                await self._serve_once()
                backoff = 0.25
            except Exception:
                log.debug("Fetch tunnel connection ended; will retry", exc_info=True)
            if self._reconnect_health.record(reason="relay reconnect loop"):
                log.warning("Fetch tunnel unhealthy: rapid relay reconnect loop detected")
            if self._stop:
                break
            await asyncio.sleep(_jittered_delay(backoff, unhealthy=self._reconnect_health.unhealthy))
            backoff *= 2

    async def _serve_once(self) -> None:
        ws = await self._relay_connect(self.relay_ws_url, self._headers)
        try:
            async for raw in ws:
                try:
                    frame = json.loads(raw)
                except Exception:
                    continue   # malformed frame: skip, don't drop the tunnel
                await self._dispatch(ws, frame)
        finally:
            await self._close_all_sessions()
            try:
                await ws.close()
            except Exception:
                pass

    # --- dispatch ---

    async def _dispatch(self, ws, frame: dict) -> None:
        t = frame.get("t")
        if t == T_REST_REQ:
            asyncio.create_task(self._handle_rest(ws, frame))
        elif t == T_WS_OPEN:
            await self._ensure_session(ws, frame.get("cid"))
        elif t == T_WS_FRAME:
            await self._handle_ws_frame(ws, frame)
        elif t == T_WS_CLOSE:
            await self._close_session(frame.get("cid"))
        # buffer-drain-begin/end are informational; the frames between them are
        # ordinary rest-req/ws-frame handled above.

    async def _send(self, ws, frame: dict) -> None:
        async with self._send_lock:
            try:
                await ws.send(json.dumps(frame))
            except Exception:
                log.debug("Fetch tunnel send failed", exc_info=True)

    async def _handle_rest(self, ws, frame: dict) -> None:
        cid, sid = frame.get("cid"), frame.get("sid")
        try:
            status, headers, body, is_b64 = await self._rest_call(frame)
        except Exception as exc:
            await self._send(ws, {"t": T_REST_RESP, "cid": cid, "sid": sid, "status": 502, "error": str(exc)})
            return
        await self._send(ws, {"t": T_REST_RESP, "cid": cid, "sid": sid, "status": status,
                              "headers": headers, "body": body, "body_b64": is_b64})

    async def _rest_call(self, frame: dict) -> tuple[int, dict, str, bool]:
        method = (frame.get("method") or "GET").upper()
        path = frame.get("path") or "/"
        query = frame.get("query")
        body = frame.get("body")
        if isinstance(body, str):
            content: bytes | None = body.encode("utf-8")
        elif isinstance(body, (bytes, bytearray)):
            content = bytes(body)
        else:
            content = None
        headers = dict(frame.get("headers") or {})
        if self.dashboard_token:
            headers.setdefault("X-Hermes-Session-Token", self.dashboard_token)
        async with self._http_client_factory() as client:
            resp = await client.request(method, self.dashboard_base + path,
                                        params=query, headers=headers, content=content)
        raw = resp.content
        ctype = resp.headers.get("content-type", "")
        if _is_textual(ctype):
            return resp.status_code, dict(resp.headers), raw.decode("utf-8", "replace"), False
        return resp.status_code, dict(resp.headers), base64.b64encode(raw).decode("ascii"), True

    async def _ensure_session(self, ws, cid: str | None) -> _LocalSession | None:
        if not cid:
            return None
        existing = self._sessions.get(cid)
        if existing is not None:
            return existing
        if self._local_ws_health.unhealthy:
            await asyncio.sleep(_jittered_delay(1.0, unhealthy=True))
        conn = await self._local_ws_connect(self.dashboard_base, self.dashboard_token)
        sess = _LocalSession(conn)
        self._sessions[cid] = sess
        sess.pump_task = asyncio.create_task(self._pump_local(ws, cid, sess))
        return sess

    async def _handle_ws_frame(self, ws, frame: dict) -> None:
        cid = frame.get("cid")
        sess = self._sessions.get(cid) or await self._ensure_session(ws, cid)  # lazy-open for a drained send
        if sess is None:
            return
        data = frame.get("data")
        await sess.send(data if isinstance(data, str) else json.dumps(data))

    async def _pump_local(self, ws, cid: str, sess: _LocalSession) -> None:
        try:
            async for raw in sess.conn:
                try:
                    data = json.loads(raw)
                except Exception:
                    data = raw
                await self._send(ws, {"t": T_WS_FRAME, "cid": cid, "data": data})
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug("Fetch tunnel local pump ended for cid=%s", cid, exc_info=True)
        finally:
            if self._local_ws_health.record(reason="local /api/ws open/close loop"):
                log.warning("Fetch tunnel unhealthy: rapid local /api/ws open/close loop detected")
            await self._send(ws, {"t": T_WS_CLOSE, "cid": cid})

    async def _close_session(self, cid: str | None) -> None:
        if not cid:
            return
        sess = self._sessions.pop(cid, None)
        if sess is not None:
            await sess.close()

    async def _close_all_sessions(self) -> None:
        for cid in list(self._sessions):
            await self._close_session(cid)

    # --- default production transports ---

    async def _default_relay_connect(self, url: str, headers: dict[str, str]):
        return await _ws_connect(url, headers)

    async def _default_local_ws_connect(self, dashboard_base: str, token: str | None):
        ws_url = _http_to_ws(dashboard_base).rstrip("/") + "/api/ws"
        if token:
            ws_url += f"?token={token}"
        return await _ws_connect(ws_url)
