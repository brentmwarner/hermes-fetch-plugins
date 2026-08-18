"""Verified window-frame control for Fetch computer sessions."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest


PLUGIN_DIR = Path(__file__).resolve().parents[1]


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "fetch_plugin_visible_computer_test", PLUGIN_DIR / "_visible_computer.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def visible_computer():
    return _load_module()


def test_window_move_preserves_size_and_requires_confirmed_readback(
    visible_computer, monkeypatch
):
    calls = []

    def fake_driver_call(tool_name, args):
        calls.append((tool_name, args))
        if tool_name == "list_windows":
            return {
                "windows": [{
                    "app_name": "Google Chrome",
                    "title": "Fetch",
                    "pid": 123,
                    "window_id": 456,
                    "is_on_screen": True,
                    "on_current_space": True,
                    "bounds": {"x": 20, "y": 30, "width": 900, "height": 700},
                }]
            }
        return {
            "effect": "confirmed",
            "route": "accessibility",
            "evidence": [{"kind": "value_readback"}],
        }

    monkeypatch.setattr(visible_computer, "_driver_call", fake_driver_call)

    result = json.loads(visible_computer.handle_window_control(
        {"pid": 123, "window_id": 456, "x": 220, "y": 130},
        session_id="fetch-session",
    ))

    assert result["ok"] is True
    assert result["effect"] == "confirmed"
    assert result["previous_frame"] == {
        "x": 20,
        "y": 30,
        "width": 900,
        "height": 700,
    }
    assert calls[1] == (
        "set_window_frame",
        {
            "pid": 123,
            "window_id": 456,
            "x": 220.0,
            "y": 130.0,
            "width": 900.0,
            "height": 700.0,
            "session": "fetch-session",
        },
    )
    assert calls[2] == ("list_windows", {})


def test_window_move_rejects_stale_or_hidden_target(visible_computer, monkeypatch):
    monkeypatch.setattr(
        visible_computer,
        "_driver_call",
        lambda tool_name, args: {
            "windows": [{
                "pid": 123,
                "window_id": 456,
                "is_on_screen": False,
                "bounds": {"x": 20, "y": 30, "width": 900, "height": 700},
            }]
        },
    )

    result = json.loads(visible_computer.handle_window_control(
        {"pid": 123, "window_id": 456, "x": 220, "y": 130}
    ))

    assert "no longer visible" in result["error"]


@pytest.mark.parametrize(
    "args, message",
    [
        ({"window_id": 2, "x": 0, "y": 0}, "pid must be an integer"),
        (
            {"pid": 1.9, "window_id": 2, "x": 0, "y": 0},
            "pid must be an integer",
        ),
        ({"pid": 1, "window_id": 2, "x": True, "y": 0}, "x must be a number"),
        (
            {"pid": 1, "window_id": 2, "x": 0, "y": 0, "width": 0},
            "width must be greater than zero",
        ),
    ],
)
def test_window_move_validates_geometry(visible_computer, monkeypatch, args, message):
    monkeypatch.setattr(
        visible_computer,
        "_driver_call",
        lambda tool_name, call_args: {
            "windows": [{
                "pid": 1,
                "window_id": 2,
                "is_on_screen": True,
                "on_current_space": True,
                "bounds": {"x": 0, "y": 0, "width": 800, "height": 600},
            }]
        },
    )

    result = json.loads(visible_computer.handle_window_control(args))

    assert result["error"] == message


def test_window_move_restores_frame_if_destination_is_not_visible(
    visible_computer, monkeypatch
):
    calls = []
    list_count = 0

    def fake_driver_call(tool_name, args):
        nonlocal list_count
        calls.append((tool_name, args))
        if tool_name == "list_windows":
            list_count += 1
            return {
                "windows": [{
                    "app_name": "TextEdit",
                    "title": "Test",
                    "pid": 123,
                    "window_id": 456,
                    "is_on_screen": list_count == 1,
                    "on_current_space": True,
                    "bounds": {"x": 20, "y": 30, "width": 900, "height": 700},
                }]
            }
        return {"effect": "confirmed", "route": "accessibility"}

    monkeypatch.setattr(visible_computer, "_driver_call", fake_driver_call)

    result = json.loads(visible_computer.handle_window_control(
        {"pid": 123, "window_id": 456, "x": 100000, "y": 100000},
        session_id="fetch-session",
    ))

    assert result["ok"] is False
    assert "restored" in result["error"]
    assert calls[-1] == (
        "set_window_frame",
        {
            "pid": 123,
            "window_id": 456,
            "x": 20,
            "y": 30,
            "width": 900,
            "height": 700,
            "session": "fetch-session",
        },
    )


def test_unconfirmed_move_restores_previous_frame(visible_computer, monkeypatch):
    calls = []

    def fake_driver_call(tool_name, args):
        calls.append((tool_name, args))
        if tool_name == "list_windows":
            return {
                "windows": [{
                    "pid": 123,
                    "window_id": 456,
                    "is_on_screen": True,
                    "on_current_space": True,
                    "bounds": {"x": 20, "y": 30, "width": 900, "height": 700},
                }]
            }
        if len([call for call in calls if call[0] == "set_window_frame"]) == 1:
            return {"effect": "unverifiable", "route": "accessibility"}
        return {"effect": "confirmed", "route": "accessibility"}

    monkeypatch.setattr(visible_computer, "_driver_call", fake_driver_call)

    result = json.loads(visible_computer.handle_window_control(
        {"pid": 123, "window_id": 456, "x": 100000, "y": 100000},
        session_id="fetch-session",
    ))

    assert result["ok"] is False
    assert result["effect"] == "unverifiable"
    assert result["restore_effect"] == "confirmed"
    assert "restored the previous frame" in result["error"]
    assert calls[-1][1]["x"] == 20
    assert calls[-1][1]["y"] == 30


def test_driver_command_uses_profile_display(visible_computer, monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_FETCH_COMPUTER_HOST_OPT_IN", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    class Manager:
        @staticmethod
        def profile_exec_args(engine, *command, **kwargs):
            return [engine, "exec", "-e", "DISPLAY=:2", "fetch-computer", *command]

    monkeypatch.setattr(visible_computer, "_load_computer_manager", lambda: Manager)
    monkeypatch.setattr(visible_computer, "host_desktop_opt_in", lambda: False)

    command = visible_computer._driver_command("list_windows", "{}")

    assert command[:2] == ["docker", "exec"]
    assert "DISPLAY=:2" in command
    assert "cua-driver" in command
    assert command[-3:] == ["call", "list_windows", "{}"]
