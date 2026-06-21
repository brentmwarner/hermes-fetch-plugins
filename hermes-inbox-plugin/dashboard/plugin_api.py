"""Dashboard API for the Fetch Inbox plugin.

Mounted by Hermes at ``/api/plugins/hermes-inbox/``.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from hermes_plugins.hermes_inbox import (
    DEFAULT_CHANNEL,
    ENABLED_ENV,
    HOME_CHANNEL_ENV,
    deliver_to_inbox,
    hermes_home_env_path,
)

router = APIRouter()
DELIVERY_TARGET = "fetch"
DISPLAY_TITLE = "Fetch"


class EnableInboxBody(BaseModel):
    enabled: bool = True
    channel: str = Field(default=DEFAULT_CHANNEL, max_length=80)


class TestInboxBody(BaseModel):
    channel: str = Field(default=DEFAULT_CHANNEL, max_length=80)
    message: str = Field(default="Fetch is ready.", max_length=1000)


@router.get("/status")
def status() -> dict:
    enabled = _truthy(os.environ.get(ENABLED_ENV, ""))
    channel = os.environ.get(HOME_CHANNEL_ENV, "").strip() or DEFAULT_CHANNEL
    return {
        "installed": True,
        "enabled": enabled,
        "delivery_target": DELIVERY_TARGET,
        "home_channel": channel,
        "home_channel_env": HOME_CHANNEL_ENV,
    }


@router.post("/enable")
def enable(body: EnableInboxBody) -> dict:
    channel = (body.channel or DEFAULT_CHANNEL).strip() or DEFAULT_CHANNEL
    _upsert_env_values({
        ENABLED_ENV: "true" if body.enabled else "false",
        HOME_CHANNEL_ENV: channel,
    })
    os.environ[ENABLED_ENV] = "true" if body.enabled else "false"
    os.environ[HOME_CHANNEL_ENV] = channel
    return {
        "ok": True,
        "installed": True,
        "enabled": body.enabled,
        "delivery_target": DELIVERY_TARGET,
        "home_channel": channel,
        "home_channel_env": HOME_CHANNEL_ENV,
        "restart_required": True,
    }


@router.post("/test")
def test(body: TestInboxBody) -> dict:
    if not _truthy(os.environ.get(ENABLED_ENV, "")):
        raise HTTPException(status_code=400, detail="Fetch Inbox is not enabled")
    try:
        delivery = deliver_to_inbox(
            channel=(body.channel or DEFAULT_CHANNEL).strip() or DEFAULT_CHANNEL,
            content=body.message,
            title=DISPLAY_TITLE,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {
        "ok": True,
        "session_id": delivery.session_id,
        "message_id": delivery.message_id,
    }


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _upsert_env_values(values: dict[str, str]) -> None:
    path = Path(hermes_home_env_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = existing.splitlines()
    seen: set[str] = set()
    updated: list[str] = []

    for line in lines:
        stripped = line.strip()
        prefix = "export " if stripped.startswith("export ") else ""
        candidate = stripped[len(prefix):] if prefix else stripped
        key, sep, _value = candidate.partition("=")
        if sep and key in values:
            updated.append(f"{prefix}{key}={values[key]}")
            seen.add(key)
        else:
            updated.append(line)

    for key, value in values.items():
        if key not in seen:
            updated.append(f"{key}={value}")

    path.write_text("\n".join(updated).rstrip() + "\n", encoding="utf-8")
