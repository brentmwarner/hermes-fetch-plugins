"""One Ubuntu container, several X displays — Cloud's model on a Hermes host.

Each Hermes profile (bot) gets DISPLAY=:N inside fetch-computer. The host
Mac/Windows/Linux desktop is opt-in only (HERMES_FETCH_COMPUTER_HOST_OPT_IN).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

FIRST_DISPLAY = 1
MAX_DISPLAYS = 16
HOST_OPT_IN_ENV = "HERMES_FETCH_COMPUTER_HOST_OPT_IN"


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


def displays_path() -> Path:
    return hermes_home() / "fetch-computer-displays.json"


def vnc_port_for(display_num: int) -> int:
    """Keep :1 on 5901 (existing plugin mapping). Extra bots are 5900+N."""
    if display_num <= 1:
        return 5901
    return 5900 + display_num


def display_name(display_num: int) -> str:
    return f":{display_num}"


def current_profile() -> str:
    raw = (
        os.environ.get("HERMES_PROFILE")
        or os.environ.get("FETCH_BOT_SLUG")
        or "default"
    )
    return raw.strip().lower() or "default"


def hermes_profile_names() -> list[str]:
    """Hermes bot profile slugs that should get their own DISPLAY=:N."""

    names: set[str] = {current_profile(), "default"}
    names.update(load_profile_displays().keys())
    profiles_dir = hermes_home() / "profiles"
    try:
        if profiles_dir.is_dir():
            for entry in profiles_dir.iterdir():
                if entry.is_dir() and not entry.name.startswith("."):
                    names.add(entry.name.strip().lower())
    except OSError:
        pass
    return sorted(name for name in names if name)


def computer_target_for(display_num: int) -> str:
    return f"tcp://127.0.0.1:{vnc_port_for(display_num)}"


def host_desktop_opt_in() -> bool:
    raw = (os.environ.get(HOST_OPT_IN_ENV) or "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    env_path = hermes_home() / ".env"
    if not env_path.is_file():
        return False
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError:
        return False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        if key.strip() != HOST_OPT_IN_ENV:
            continue
        value = value.strip().strip("\"'")
        return value.lower() in {"1", "true", "yes", "on"}
    return False


def load_profile_displays() -> dict[str, int]:
    path = displays_path()
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    rows = payload.get("profiles") if isinstance(payload, dict) else None
    if not isinstance(rows, dict):
        return {}
    out: dict[str, int] = {}
    for key, value in rows.items():
        try:
            display_num = int(value)
        except (TypeError, ValueError):
            continue
        if FIRST_DISPLAY <= display_num <= MAX_DISPLAYS:
            out[str(key).strip().lower()] = display_num
    return out


def save_profile_displays(mapping: dict[str, int]) -> None:
    path = displays_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"profiles": mapping}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def allocate_display(profile: str | None = None) -> int:
    key = (profile or current_profile()).strip().lower() or "default"
    mapping = load_profile_displays()
    existing = mapping.get(key)
    if existing is not None:
        return existing
    used = set(mapping.values())
    display_num = FIRST_DISPLAY
    while display_num in used:
        display_num += 1
    if display_num > MAX_DISPLAYS:
        raise RuntimeError(
            f"Fetch computer supports at most {MAX_DISPLAYS} bot desktops."
        )
    mapping[key] = display_num
    save_profile_displays(mapping)
    return display_num
