"""Fetch push plugin — device-registration routes (dashboard half).

Mounted at ``/api/plugins/fetch/`` by the dashboard plugin system, behind the
dashboard's session-token auth middleware — the iOS app already sends
``X-Hermes-Session-Token`` on every REST call, so these routes are authenticated
with zero extra plumbing.

Device tokens are proxied straight to the Fetch push relay; nothing is stored on
this host. The relay owns the token → device fan-out and holds the single APNs
key.

This module is exec'd standalone by the dashboard plugin loader (via
``spec_from_file_location``), so it has no package context — the shared relay
client is loaded by file path, exactly as the runtime half does.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Dict, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

log = logging.getLogger("fetch_plugin.api")

# Shared relay client (one directory up: ~/.hermes/plugins/fetch/_relay.py).
# Register in sys.modules BEFORE exec so its @dataclass annotations resolve
# (the module uses `from __future__ import annotations`).
_relay_path = Path(__file__).resolve().parent.parent / "_relay.py"
_spec = importlib.util.spec_from_file_location("fetch_plugin_relay_api", _relay_path)
assert _spec is not None and _spec.loader is not None
_relay = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _relay
_spec.loader.exec_module(_relay)


_inbox = None
_inbox_lock = threading.Lock()


def _load_inbox():
    """Lazily load the sibling ``_inbox.py`` by path.

    ``_inbox`` imports ``gateway`` / ``hermes_state`` at module load, which are
    present in the dashboard process but not on a minimal host — load it lazily
    so the device-registration routes above keep working regardless.

    Uses a double-checked lock so concurrent threadpool requests don't race to
    exec the module twice or observe a partially-initialized module via
    sys.modules.
    """
    global _inbox
    if _inbox is not None:
        return _inbox
    with _inbox_lock:
        if _inbox is not None:  # re-check under the lock
            return _inbox
        path = Path(__file__).resolve().parent.parent / "_inbox.py"
        spec = importlib.util.spec_from_file_location("fetch_plugin_inbox_api", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _inbox = module
    return _inbox


router = APIRouter()


class RegisterBody(BaseModel):
    token: str = Field(min_length=1, max_length=512)
    platform: str = Field(default="ios", max_length=32)
    environment: str = Field(max_length=20)
    bundle_id: str = Field(min_length=1, max_length=160)
    preferences: Dict[str, bool] = Field(default_factory=dict)
    attestation: str | None = Field(default=None, min_length=1, max_length=8192)
    key_id: str | None = Field(default=None, min_length=1, max_length=256)
    challenge: str | None = Field(default=None, min_length=1, max_length=256)


class UnregisterBody(BaseModel):
    token: str = Field(min_length=1, max_length=512)


@router.get("/attest/challenge")
async def attest_challenge() -> dict:
    try:
        return {"challenge": await _relay.relay_client().get_attest_challenge()}
    except Exception as exc:
        log.warning("Fetch push: challenge fetch failed: %s", exc)
        raise HTTPException(status_code=502, detail="push relay challenge failed") from exc


@router.post("/register")
async def register(body: RegisterBody) -> dict:
    present = [body.attestation, body.key_id, body.challenge]
    if any(present) and not all(present):
        raise HTTPException(status_code=422, detail="attestation, key_id, and challenge must all be provided together")
    attestation = None
    if all(present):
        attestation = {"attestation": body.attestation, "key_id": body.key_id, "challenge": body.challenge}
    try:
        await _relay.relay_client().register_device(
            token=body.token,
            platform=body.platform,
            environment=body.environment,
            bundle_id=body.bundle_id,
            preferences=body.preferences,
            attestation=attestation,
        )
    except _relay.NeedsAttestation as exc:
        raise HTTPException(status_code=428, detail="attestation required") from exc
    except Exception as exc:  # relay unreachable / rejected — surface as a gateway error
        log.warning("Fetch push device registration failed: %s", exc)
        raise HTTPException(status_code=502, detail="push relay registration failed") from exc
    return {"ok": True}


@router.post("/unregister")
async def unregister(body: UnregisterBody) -> dict:
    try:
        await _relay.relay_client().unregister_device(token=body.token)
    except Exception as exc:  # best-effort; the device ages out via APNs feedback anyway
        log.warning("Fetch push device unregister failed: %s", exc)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Inbox delivery configuration (status / enable / test)
# ---------------------------------------------------------------------------
# Lets the dashboard or app turn Fetch into a cron/webhook delivery target and
# fire a test push, all under the single ``fetch`` product. Fetch relay setup
# already enables delivery automatically; these routes are the explicit control.


class EnableInboxBody(BaseModel):
    enabled: bool = True
    channel: str = Field(default="default", max_length=80)


class TestInboxBody(BaseModel):
    channel: str = Field(default="default", max_length=80)
    message: str = Field(default="Fetch is ready.", min_length=1, max_length=1000)


@router.get("/inbox/status")
def inbox_status() -> dict:
    inbox = _load_inbox()
    channel = os.environ.get(inbox.HOME_CHANNEL_ENV, "").strip() or inbox.DEFAULT_CHANNEL
    return {
        "installed": True,
        "enabled": inbox.is_delivery_enabled(),
        "delivery_target": inbox.PLATFORM_NAME,
        "home_channel": channel,
        "home_channel_env": inbox.HOME_CHANNEL_ENV,
    }


@router.post("/inbox/enable")
def inbox_enable(body: EnableInboxBody) -> dict:
    inbox = _load_inbox()
    channel = (body.channel or inbox.DEFAULT_CHANNEL).strip() or inbox.DEFAULT_CHANNEL
    inbox.set_delivery_enabled(body.enabled, channel=channel)
    return {
        "ok": True,
        "installed": True,
        "enabled": body.enabled,
        "delivery_target": inbox.PLATFORM_NAME,
        "home_channel": channel,
        "home_channel_env": inbox.HOME_CHANNEL_ENV,
        "restart_required": True,
    }


@router.post("/inbox/test")
def inbox_test(body: TestInboxBody) -> dict:
    inbox = _load_inbox()
    if not inbox.is_delivery_enabled():
        raise HTTPException(status_code=400, detail="Fetch delivery is not enabled")
    channel = (body.channel or inbox.DEFAULT_CHANNEL).strip() or inbox.DEFAULT_CHANNEL
    try:
        delivery = inbox.deliver_to_inbox(channel=channel, content=body.message, title="Fetch")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("Fetch delivery test failed unexpectedly")
        raise HTTPException(status_code=500, detail="delivery failed") from exc
    return {"ok": True, "session_id": delivery.session_id, "message_id": delivery.message_id}


# ---------------------------------------------------------------------------
# Kanban task reactivation (FET-15)
# ---------------------------------------------------------------------------

# Serialises the brief, process-wide guard swap in _force_dispatch below.
_reactivate_lock = threading.Lock()


def _force_dispatch(kanban_db, conn, task_id: str, board) -> bool:
    """Run one ``dispatch_once`` tick with ``check_respawn_guard`` neutralised
    for ``task_id`` only, and report whether that task spawned.

    Why swap a core global instead of replicating the spawn: ``dispatch_once``
    owns claim → workspace resolution → the worker subprocess launch
    (``_default_spawn``), all of which change between Hermes releases. Copying
    that here would rot. Instead we reuse it verbatim and override the one
    decision we need — the auto-guard that defers respawning a task which
    completed in the last hour / has an open PR. An explicit user reply is a
    "do more" signal the auto-guard doesn't model.

    The swap is serialised (``_reactivate_lock``) and restored in ``finally``.
    Only ``task_id`` is bypassed, so every other task still faces the real
    guard — the tick is "what the dispatcher would do anyway, plus this one
    task". Retries while another dispatcher (the gateway's) holds the board
    tick lock, which frees between ticks. If the guard symbol is gone in a
    future Hermes, we don't patch and fall back to a plain dispatch.
    """
    orig_guard = getattr(kanban_db, "check_respawn_guard", None)
    orig_claim_task = getattr(kanban_db, "claim_task", None)

    def _patched(c, tid, _orig=orig_guard):
        if tid == task_id:
            return None
        return _orig(c, tid) if _orig is not None else None

    def _claim_only_target(c, tid, *args, _orig=orig_claim_task, **kwargs):
        if tid != task_id:
            return None
        return _orig(c, tid, *args, **kwargs)

    with _reactivate_lock:
        for _ in range(8):
            if orig_guard is not None:
                kanban_db.check_respawn_guard = _patched
            if orig_claim_task is not None:
                kanban_db.claim_task = _claim_only_target
            try:
                result = kanban_db.dispatch_once(conn, max_spawn=16, board=board)
            finally:
                if orig_guard is not None:
                    kanban_db.check_respawn_guard = orig_guard
                if orig_claim_task is not None:
                    kanban_db.claim_task = orig_claim_task
            if getattr(result, "skipped_locked", False):
                time.sleep(0.1)
                continue
            spawned_ids = {s[0] for s in getattr(result, "spawned", [])}
            if task_id in spawned_ids:
                return True
            # Another concurrent tick may already have claimed it.
            t = kanban_db.get_task(conn, task_id)
            return bool(t is not None and (t.status or "") == "running")
        return False


@router.post("/tasks/{task_id}/reactivate")
def reactivate_task(task_id: str, board: Optional[str] = Query(None)) -> dict:
    """Force a worker respawn for a task the user explicitly replied to (FET-15).

    The auto-dispatcher's ``check_respawn_guard`` defers respawning a task for up
    to an hour after it completes (or a day after a PR link) — so a follow-up
    reply on a just-finished task silently fails to reactivate it. The iOS app
    has already posted the comment and moved the task to ``ready``; this route
    forces the spawn, bypassing only that guard, reusing Hermes' real spawn path.

    Lives in the Fetch plugin (a supported extension point) rather than a patch
    to ``check_respawn_guard``, so a ``hermes update`` can't silently drop it.
    """
    try:
        from hermes_cli import kanban_db
    except Exception as exc:  # not a kanban-capable host
        raise HTTPException(status_code=501, detail="kanban is not available on this host") from exc

    try:
        conn = kanban_db.connect(board=board)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid board: {exc}") from exc
    try:
        task = kanban_db.get_task(conn, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"task {task_id} not found")
        status = (task.status or "")
        if status == "running":
            # A live worker holds the claim; it picks up the new comment on its
            # next kanban_show. Nothing to spawn — report success.
            return {"ok": True, "spawned": False, "reason": "already_running"}
        if status != "ready":
            raise HTTPException(
                status_code=409,
                detail=f"task must be 'ready' to reactivate (status={status!r})",
            )
        if not (task.assignee or "").strip():
            raise HTTPException(status_code=409, detail="assign a profile before reactivating")
        spawned = _force_dispatch(kanban_db, conn, task_id, board)
        return {"ok": True, "spawned": spawned}
    finally:
        conn.close()
