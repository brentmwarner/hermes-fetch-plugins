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
import sys
from pathlib import Path
from typing import Dict

from fastapi import APIRouter, HTTPException
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
