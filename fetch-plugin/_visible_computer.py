"""Reliable visible-window movement for Fetch computer sessions.

Hermes' generic ``computer_use.drag`` is a pointer gesture. Window-manager
title bars are a special case on every desktop OS and a driver can report that
the gesture was posted even when the window manager declined to move the
window. cua-driver exposes a stronger cross-platform primitive,
``set_window_frame``, which performs an independent geometry readback. This
module gives the Fetch plugin a narrow tool around that verified primitive.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from _computer_displays import host_desktop_opt_in  # noqa: E402


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
    """Return whether this host can drive the streamed Fetch desktop."""
    if host_desktop_opt_in():
        return shutil.which("cua-driver") is not None
    return shutil.which("docker") is not None or shutil.which("cua-driver") is not None


def _load_computer_manager():
    existing = sys.modules.get("fetch_plugin_linux_computer")
    if existing is not None:
        return existing
    path = Path(__file__).resolve().parent / "linux-computer" / "manage.py"
    spec = importlib.util.spec_from_file_location("fetch_plugin_linux_computer", path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _driver_command(tool_name: str, payload: str) -> list[str] | None:
    """cua-driver argv bound to this profile's Ubuntu DISPLAY=:N."""

    if host_desktop_opt_in():
        binary = shutil.which("cua-driver")
        if binary is None:
            return None
        return [binary, "call", tool_name, payload]
    manager = _load_computer_manager()
    if manager is None:
        return None
    return manager.profile_exec_args(
        "docker",
        "cua-driver",
        "call",
        tool_name,
        payload,
    )


def _driver_call(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    payload = json.dumps(args, separators=(",", ":"))
    command = _driver_command(tool_name, payload)
    if command is None:
        return {
            "error": (
                "cua-driver is unavailable. Fetch drives the Ubuntu container "
                "via docker exec; install Docker, or run "
                "`hermes computer-use install` for an opt-in host desktop."
            )
        }

    try:
        completed = subprocess.run(
            command,
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


def _positive_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    if value <= 0:
        raise ValueError(f"{field} must be greater than zero")
    return value


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


def _restore_frame(
    *,
    pid: int,
    window_id: int,
    bounds: dict[str, Any],
    session_id: str,
) -> dict[str, Any]:
    return _driver_call("set_window_frame", {
        "pid": pid,
        "window_id": window_id,
        "x": bounds.get("x"),
        "y": bounds.get("y"),
        "width": bounds.get("width"),
        "height": bounds.get("height"),
        "session": session_id or "fetch-window-control",
    })


def handle_window_control(
    args: dict[str, Any], *, session_id: str = ""
) -> str:
    """Move one exact current-space window through verified frame mutation."""
    try:
        pid = _positive_integer(args.get("pid"), "pid")
        window_id = _positive_integer(args.get("window_id"), "window_id")
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

    if result.get("effect") != "confirmed":
        # An unconfirmed write can still have moved or OS-clamped the window.
        # Restore before returning so a false/ambiguous result never strands it
        # at the edge of (or outside) the desktop Fetch is streaming.
        restore_result = _restore_frame(
            pid=pid,
            window_id=window_id,
            bounds=bounds,
            session_id=session_id,
        )
        restore_confirmed = restore_result.get("effect") == "confirmed"
        return json.dumps({
            "ok": False,
            "error": (
                "cua-driver did not confirm the requested window frame; "
                + (
                    "Fetch restored the previous frame."
                    if restore_confirmed
                    else "the automatic restore could not be confirmed."
                )
            ),
            "effect": result.get("effect"),
            "route": result.get("route"),
            "evidence": result.get("evidence", []),
            "pid": pid,
            "window_id": window_id,
            "previous_frame": bounds,
            "requested_frame": requested_frame,
            "restore_effect": restore_result.get("effect"),
            "restore_error": restore_result.get("error"),
        })

    # A geometry readback proves the mutation happened, but it does not prove
    # the new frame is still part of the desktop Fetch streams. Re-read the
    # public window inventory and fail closed. If the window left the visible
    # current desktop, restore the exact prior frame before returning.
    post_move_windows = _driver_call("list_windows", {})
    post_move_window = _visible_window(post_move_windows, pid, window_id)
    if post_move_windows.get("error") or post_move_window is None:
        restore_result = _restore_frame(
            pid=pid,
            window_id=window_id,
            bounds=bounds,
            session_id=session_id,
        )
        return json.dumps({
            "ok": False,
            "error": (
                "The requested frame left the visible current desktop, so "
                "Fetch restored the window's previous frame."
            ),
            "pid": pid,
            "window_id": window_id,
            "previous_frame": bounds,
            "requested_frame": requested_frame,
            "restore_effect": restore_result.get("effect"),
            "restore_error": restore_result.get("error"),
        })

    return json.dumps({
        "ok": True,
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
