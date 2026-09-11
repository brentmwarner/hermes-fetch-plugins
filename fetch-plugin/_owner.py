"""Fetch's machine-wide mobile owner policy.

Hermes profiles are independent bot homes, but Fetch intentionally exposes one
mobile identity and one fixed loopback dashboard (127.0.0.1:9119).  This module
keeps the ownership decision and owner-home routing identical in the plugin,
dashboard, relay, pairing, and gateway-adapter processes.

Loaded by file path, like the other Fetch support modules, so it must not rely
on a package import.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

OWNER_PROFILE_ENV = "HERMES_FETCH_OWNER_PROFILE"
STORE_HOME_ENV = "HERMES_FETCH_STORE_HOME"
DEFAULT_OWNER_PROFILE = "default"

_PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def normalize_profile_name(value: object) -> str:
    """Normalize exactly as Hermes profile ingress does, then validate.

    Hermes accepts title/mixed case at ingress and stores lowercase profile
    ids.  Keeping a small fallback here lets the installed plugin work on older
    Hermes builds that do not yet export ``normalize_profile_name``.
    """
    text = str(value or "").strip()
    if not text:
        raise ValueError("profile name cannot be empty")
    try:
        from hermes_cli.profiles import normalize_profile_name as normalize

        normalized = normalize(text)
    except (ImportError, AttributeError):
        normalized = DEFAULT_OWNER_PROFILE if text.casefold() == "default" else text.lower()
    if normalized != DEFAULT_OWNER_PROFILE and not _PROFILE_ID_RE.fullmatch(normalized):
        raise ValueError(
            f"invalid Fetch owner profile {text!r}; expected "
            "[a-z0-9][a-z0-9_-]{0,63}"
        )
    try:
        from hermes_cli.profiles import validate_profile_name

        validate_profile_name(normalized)
    except (ImportError, AttributeError):
        pass
    return normalized


def _current_home() -> Path:
    try:
        from hermes_cli.config import get_hermes_home

        return Path(get_hermes_home()).expanduser()
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")).expanduser()


def default_home() -> Path:
    """Return the machine's default/root Hermes home from any profile."""
    try:
        from hermes_cli.profiles import get_profile_dir

        return Path(get_profile_dir(DEFAULT_OWNER_PROFILE)).expanduser()
    except Exception:
        current = _current_home()
        if current.parent.name == "profiles":
            return current.parent.parent
        return current


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        if not separator:
            continue
        value = value.strip()
        if value[:1] in {"'", '"'}:
            quote = value[0]
            end = value.find(quote, 1)
            value = value[1:end] if end != -1 else value[1:]
        values[key.strip()] = value
    return values


def owner_profile_name() -> str:
    """Configured machine-wide owner, defaulting compatibly to ``default``.

    The root Hermes ``.env`` is checked explicitly because named profiles load
    their own environment files.  One setting in the root home must therefore
    be visible to every profile before any of them decides it may own Fetch.
    An exported process environment still takes precedence.
    """
    configured = os.environ.get(OWNER_PROFILE_ENV, "").strip()
    if not configured:
        configured = _parse_env_file(default_home() / ".env").get(OWNER_PROFILE_ENV, "").strip()
    return normalize_profile_name(configured or DEFAULT_OWNER_PROFILE)


def current_profile_name() -> str:
    """Resolve the ambient Hermes profile to its canonical id.

    ``HERMES_PROFILE`` is used by gateway/bot launchers.  Hermes CLI ``-p``
    instead scopes ``HERMES_HOME`` and exposes ``get_active_profile_name``.
    A custom single-home installation is treated as the backwards-compatible
    default profile rather than being unexpectedly made passive.
    """
    configured = os.environ.get("HERMES_PROFILE", "").strip()
    if configured:
        return normalize_profile_name(configured)
    try:
        from hermes_cli.profiles import get_active_profile_name

        active = str(get_active_profile_name() or "").strip()
        if active and active.casefold() != "custom":
            return normalize_profile_name(active)
    except Exception:
        pass
    current = _current_home()
    if current.parent.name == "profiles":
        return normalize_profile_name(current.name)
    return DEFAULT_OWNER_PROFILE


def is_owner_profile() -> bool:
    """True only in the one profile allowed to own Fetch's mobile runtime."""
    try:
        return current_profile_name() == owner_profile_name()
    except ValueError:
        # Invalid policy is safer as all-passive than as an accidental default.
        return False


def owner_home() -> Path:
    """Hermes home containing the one Fetch pairing, state DB, and run files."""
    owner = owner_profile_name()
    # Preserve custom/single-home installations. Hermes reports those as the
    # logical default profile, but get_profile_dir("default") still points at
    # the conventional ~/.hermes root. The active owner home is the authority.
    if current_profile_name() == owner:
        return _current_home()
    try:
        from hermes_cli.profiles import get_profile_dir

        return Path(get_profile_dir(owner)).expanduser()
    except Exception:
        root = default_home()
        return root if owner == DEFAULT_OWNER_PROFILE else root / "profiles" / owner


def delivery_home() -> Path:
    """Explicit legacy routing override, otherwise the configured owner home."""
    configured = str(owner_config_value(STORE_HOME_ENV, "") or "").strip()
    if configured:
        return Path(os.path.expanduser(configured))
    return owner_home()


def owner_config_value(name: str, default: str | None = None) -> str | None:
    """Read one value from the process or the owner profile's durable config."""
    value = os.environ.get(name)
    if value:
        return value
    configured = _parse_env_file(owner_home() / ".env").get(name)
    return configured if configured else default


def policy_status() -> dict[str, object]:
    """Non-secret ownership facts suitable for logs and diagnostics."""
    try:
        owner = owner_profile_name()
        error = None
    except ValueError as exc:
        owner = ""
        error = str(exc)
    try:
        current = current_profile_name()
    except ValueError as exc:
        current = ""
        error = error or str(exc)
    return {
        "owner_profile": owner,
        "current_profile": current,
        "is_owner": bool(owner and current and owner == current),
        "valid": error is None,
        "error": error,
        "setting": OWNER_PROFILE_ENV,
    }
