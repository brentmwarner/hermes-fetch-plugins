"""Reliable visible-window movement for Fetch computer sessions.

Hermes' generic ``computer_use.drag`` is a pointer gesture. Window-manager
title bars are a special case on every desktop OS and a driver can report that
the gesture was posted even when the window manager declined to move the
window. cua-driver exposes a stronger cross-platform primitive,
``set_window_frame``, which performs an independent geometry readback. This
module gives the Fetch plugin a narrow tool around that verified primitive.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any


FETCH_WINDOW_CONTROL_SCHEMA = {
    "name": "fetch_window_control",
    "description": (
        "Move or resize one exact visible desktop window and verify its final "
        "geometry. First call computer_use with action=list_windows, then pass "
        "the returned pid and window_id here. Use this instead of dragging a "
        "title bar when the person asks to move or resize a window."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "pid": {
                "type": "integer",
                "minimum": 1,
                "description": "Exact owner pid returned by computer_use list_windows.",
            },
            "window_id": {
                "type": "integer",
                "minimum": 1,
                "description": "Exact window id returned by computer_use list_windows.",
            },
            "x": {
                "type": "number",
                "description": "New left edge in desktop coordinates.",
            },
            "y": {
                "type": "number",
                "description": "New top edge in desktop coordinates.",
            },
            "width": {
                "type": "number",
                "minimum": 1,
                "description": "Optional new width. Omit to preserve the current width.",
            },
            "height": {
                "type": "number",
                "minimum": 1,
                "description": "Optional new height. Omit to preserve the current height.",
            },
        },
        "required": ["pid", "window_id", "x", "y"],
        "additionalProperties": False,
    },
}


def check_requirements() -> bool:
    """Return whether the public cua-driver dependency is installed."""
    return shutil.which("cua-driver") is not None


def _driver_call(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    binary = shutil.which("cua-driver")
    if binary is None:
        return {
            "error": (
                "cua-driver is unavailable. Run `hermes computer-use install` "
                "on this Hermes host."
            )
        }

    try:
        completed = subprocess.run(
            [binary, "call", tool_name, json.dumps(args, separators=(",", ":"))],
            capture_output=True,
            check=False,
            text=True,
            timeout=20,
        )
    except subprocess.TimeoutExpired:
        return {"error": f"cua-driver {tool_name} timed out after 20 seconds"}
    except OSError as exc:
        return {"error": f"cua-driver {tool_name} could not start: {exc}"}
    output = completed.stdout.strip()
    if completed.returncode != 0:
        detail = output or completed.stderr.strip() or "cua-driver call failed"
        return {"error": detail[:2000]}
    try:
        decoded = json.loads(output)
    except json.JSONDecodeError:
        return {"error": (output or "cua-driver returned no result")[:2000]}
    return decoded if isinstance(decoded, dict) else {"result": decoded}


def _number(value: Any, field: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a number")
    number = float(value)
    if positive and number <= 0:
        raise ValueError(f"{field} must be greater than zero")
    return number


def _visible_window(
    windows_payload: dict[str, Any], pid: int, window_id: int
) -> dict[str, Any] | None:
    windows = windows_payload.get("windows")
    if not isinstance(windows, list):
        return None
    for candidate in windows:
        if not isinstance(candidate, dict):
            continue
        if candidate.get("pid") != pid or candidate.get("window_id") != window_id:
            continue
        if candidate.get("is_on_screen") is not True:
            return None
        if candidate.get("on_current_space") is False:
            return None
        return candidate
    return None


def handle_window_control(
    args: dict[str, Any], *, session_id: str = ""
) -> str:
    """Move one exact current-space window through verified frame mutation."""
    try:
        pid = int(_number(args.get("pid"), "pid", positive=True))
        window_id = int(
            _number(args.get("window_id"), "window_id", positive=True)
        )
        x = _number(args.get("x"), "x")
        y = _number(args.get("y"), "y")
    except (TypeError, ValueError) as exc:
        return json.dumps({"error": str(exc)})

    windows_payload = _driver_call("list_windows", {})
    if windows_payload.get("error"):
        return json.dumps(windows_payload)
    window = _visible_window(windows_payload, pid, window_id)
    if window is None:
        return json.dumps({
            "error": (
                "The exact window is no longer visible on the current desktop. "
                "Call computer_use list_windows again before retrying."
            ),
            "pid": pid,
            "window_id": window_id,
        })

    bounds = window.get("bounds")
    if not isinstance(bounds, dict):
        return json.dumps({"error": "The selected window has no readable frame."})
    try:
        width = _number(
            args.get("width", bounds.get("width")), "width", positive=True
        )
        height = _number(
            args.get("height", bounds.get("height")), "height", positive=True
        )
    except ValueError as exc:
        return json.dumps({"error": str(exc)})

    requested_frame = {
        "x": x,
        "y": y,
        "width": width,
        "height": height,
    }
    result = _driver_call("set_window_frame", {
        "pid": pid,
        "window_id": window_id,
        **requested_frame,
        "session": session_id or "fetch-window-control",
    })
    if result.get("error"):
        return json.dumps(result)

    return json.dumps({
        "ok": result.get("effect") == "confirmed",
        "effect": result.get("effect"),
        "route": result.get("route"),
        "evidence": result.get("evidence", []),
        "app": window.get("app_name", ""),
        "title": window.get("title", ""),
        "pid": pid,
        "window_id": window_id,
        "previous_frame": bounds,
        "requested_frame": requested_frame,
    })
