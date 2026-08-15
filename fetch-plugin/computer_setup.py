#!/usr/bin/env python3
"""Configure and verify Fetch computer access on the current machine."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

TARGET_ENV = "HERMES_FETCH_COMPUTER_TARGET"
NAME_ENV = "HERMES_FETCH_COMPUTER_NAME"
KIND_ENV = "HERMES_FETCH_COMPUTER_KIND"
LEGACY_TARGET_ENV = "HERMES_FETCH_COMPUTER_WS_URL"
TUNNEL_ENV = "HERMES_FETCH_TUNNEL_ENABLED"
DISPLAY_ENV = "DISPLAY"
XAUTHORITY_ENV = "XAUTHORITY"
BROWSER_HEADED_ENV = "AGENT_BROWSER_HEADED"
VNC_PASSWORD_ENV = "HERMES_FETCH_COMPUTER_VNC_PASSWORD"
COMPUTER_ENV_KEYS = (
    TARGET_ENV,
    NAME_ENV,
    KIND_ENV,
    LEGACY_TARGET_ENV,
    DISPLAY_ENV,
    XAUTHORITY_ENV,
    BROWSER_HEADED_ENV,
    VNC_PASSWORD_ENV,
)
_GATEWAY_RESTART_STATE_FILE = "fetch-computer-gateway-restart.json"


class SetupError(RuntimeError):
    pass


def _load_sibling(module_name: str, filename: str):
    path = Path(__file__).resolve().parent / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def hermes_home() -> Path:
    store_home = os.environ.get("HERMES_FETCH_STORE_HOME", "").strip()
    if store_home:
        return Path(os.path.expanduser(store_home))
    try:
        from hermes_cli.config import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


def _quoted_env_value(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _env_line_key(line: str) -> str | None:
    stripped = line.strip()
    candidate = stripped[7:].lstrip() if stripped.startswith("export ") else stripped
    key, separator, _value = candidate.partition("=")
    if not separator:
        return None
    return key.strip()


def _write_env_file(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            handle.write("\n".join(lines).rstrip() + "\n")
            temporary_path = Path(handle.name)
        try:
            os.chmod(temporary_path, 0o600)
        except OSError:
            pass
        os.replace(temporary_path, path)
    except OSError as exc:
        raise SetupError(f"Could not update {path}: {exc}") from exc


def persist_environment(path: Path, values: dict[str, str]) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    except OSError as exc:
        raise SetupError(f"Could not read {path}: {exc}") from exc

    remaining = dict(values)
    updated: list[str] = []
    for line in lines:
        key = _env_line_key(line)
        if key is not None and key in remaining:
            updated.append(f"{key}={_quoted_env_value(remaining.pop(key))}")
        else:
            updated.append(line)
    if remaining and updated and updated[-1].strip():
        updated.append("")
    for key, value in remaining.items():
        updated.append(f"{key}={_quoted_env_value(value)}")
    _write_env_file(path, updated)


def remove_environment_keys(path: Path, keys: tuple[str, ...] | list[str] | set[str]) -> None:
    drop = set(keys)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise SetupError(f"Could not read {path}: {exc}") from exc
    updated = [line for line in lines if _env_line_key(line) not in drop]
    _write_env_file(path, updated)


def _computer_runtime_module():
    return _load_sibling("fetch_plugin_computer_runtime_setup", "_computer_runtime.py")


def _relay_runtime_module():
    return _load_sibling("fetch_plugin_runtime_setup", "_runtime.py")


def _gateway_restart_state_path() -> Path:
    return hermes_home() / "run" / _GATEWAY_RESTART_STATE_FILE


def _configuration_fingerprint(values: dict[str, str]) -> str:
    payload = json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _read_gateway_restart_state() -> tuple[str, set[int]]:
    try:
        data = json.loads(_gateway_restart_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "", set()
    if not isinstance(data, dict):
        return "", set()
    fingerprint = str(data.get("fingerprint") or "")
    pids: set[int] = set()
    for raw_pid in data.get("pids") or []:
        try:
            pid = int(raw_pid)
        except (TypeError, ValueError):
            continue
        if pid > 0:
            pids.add(pid)
    return fingerprint, pids


def _write_gateway_restart_state(fingerprint: str, pids: set[int]) -> None:
    path = _gateway_restart_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"fingerprint": fingerprint, "pids": sorted(pids)},
        separators=(",", ":"),
    )
    _write_env_file(path, [payload])


def _clear_gateway_restart_state() -> None:
    try:
        _gateway_restart_state_path().unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise SetupError(f"Could not clear the Hermes restart checkpoint: {exc}") from exc


def _manual_gateway_adopted_configuration(fingerprint: str, pids: list[int]) -> bool:
    current_pids = {pid for pid in pids if pid > 0}
    previous_fingerprint, previous_pids = _read_gateway_restart_state()
    if (
        current_pids
        and previous_fingerprint == fingerprint
        and previous_pids
        and current_pids.isdisjoint(previous_pids)
    ):
        _clear_gateway_restart_state()
        return True
    _write_gateway_restart_state(fingerprint, current_pids)
    return False


def disable_computer() -> None:
    computer_runtime = _computer_runtime_module()
    if not computer_runtime.restart_computer_runtime():
        raise SetupError("Could not stop the Fetch computer bridge.")
    remove_environment_keys(hermes_home() / ".env", COMPUTER_ENV_KEYS)
    for key in COMPUTER_ENV_KEYS:
        os.environ.pop(key, None)

    relay_runtime = _relay_runtime_module()
    relay_handoff = relay_runtime.restart_relay_runtime_for_reconfigure()
    left_running = relay_handoff.get("left_running") or []
    if left_running:
        disabled_fingerprint = _configuration_fingerprint({"computer": "disabled"})
        if not _manual_gateway_adopted_configuration(disabled_fingerprint, left_running):
            raise SetupError(
                "Computer access is disabled, but a manually managed Hermes gateway still has the "
                "old desktop environment. Restart that gateway, then rerun this cleanup."
            )
    else:
        _clear_gateway_restart_state()
        relay_runtime_status = relay_runtime.ensure_relay_runtime()
        if relay_runtime_status not in {"started", "already-running", "self", "disabled"}:
            raise SetupError(
                f"Could not restart the Hermes runtime after cleanup: {relay_runtime_status}"
            )


def _persisted_environment_value(path: Path, key: str) -> str:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        if _env_line_key(line) != key:
            continue
        raw = line.strip()
        if raw.startswith("export "):
            raw = raw[7:].lstrip()
        _name, _separator, value = raw.partition("=")
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, str) else ""
        except (TypeError, ValueError):
            return value.strip().strip("'\"")
    return ""


def configured_vnc_password() -> str:
    current = os.environ.get(VNC_PASSWORD_ENV, "")
    if current:
        return current
    return _persisted_environment_value(hermes_home() / ".env", VNC_PASSWORD_ENV)


def probe_desktop(target: str, *, password: str, wait_seconds: float) -> None:
    computer = _load_sibling("fetch_plugin_computer_setup_probe", "_computer.py")
    target = computer.validate_local_target(target)
    parsed = urlsplit(target)
    if parsed.scheme != "tcp":
        raise SetupError("Automatic readiness checks require a tcp:// loopback VNC target.")

    deadline = time.monotonic() + max(0.0, wait_seconds)
    last_error = "no RFB response"
    while True:
        try:
            async def open_and_close() -> None:
                connection = await computer.open_local_vnc(target, password=password)
                await connection.close()

            asyncio.run(asyncio.wait_for(open_and_close(), timeout=2.0))
            return
        except computer.VNCSetupError as exc:
            raise SetupError(str(exc)) from exc
        except (asyncio.IncompleteReadError, OSError, TimeoutError) as exc:
            last_error = str(exc)
        if time.monotonic() >= deadline:
            break
        time.sleep(0.25)
    raise SetupError(f"The local desktop did not become ready at {target}: {last_error}")


def _credentials() -> dict[str, str]:
    path = hermes_home() / "push" / "fetch-relay.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SetupError("Fetch is not paired yet. Run `hermes setup`, choose Fetch, then rerun this setup.") from exc
    if not isinstance(data, dict):
        raise SetupError("Fetch relay credentials are invalid. Run `hermes setup` again.")
    credentials = {
        "relay_url": str(data.get("relay_url") or "").rstrip("/"),
        "agent_id": str(data.get("agent_id") or ""),
        "agent_secret": str(data.get("agent_secret") or ""),
    }
    if not all(credentials.values()):
        raise SetupError("Fetch relay credentials are incomplete. Run `hermes setup` again.")
    return credentials


def computer_status(credentials: dict[str, str]) -> tuple[int, dict]:
    request = Request(
        credentials["relay_url"] + "/v1/agents/computer/status",
        headers={
            "Accept": "application/json",
            "X-Hermes-Agent-Id": credentials["agent_id"],
            "Authorization": f"Bearer {credentials['agent_secret']}",
        },
    )
    try:
        with urlopen(request, timeout=5.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return response.status, payload if isinstance(payload, dict) else {}
    except HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            payload = {}
        return exc.code, payload if isinstance(payload, dict) else {}
    except URLError as exc:
        raise SetupError(f"Could not reach the Fetch relay: {exc.reason}") from exc


def wait_for_relay(credentials: dict[str, str], *, wait_seconds: float) -> dict:
    deadline = time.monotonic() + max(0.0, wait_seconds)
    last_reason = "agent_offline"
    while True:
        status, payload = computer_status(credentials)
        if status == 200 and payload.get("ok") is True:
            return payload
        reason = str(payload.get("reason") or f"http_{status}")
        last_reason = reason
        if status == 404:
            raise SetupError("The Fetch relay does not support computer readiness yet. Deploy the matching relay release.")
        if status == 401:
            raise SetupError("The Fetch relay rejected this agent. Run `hermes setup` again.")
        if reason == "computer_disabled":
            raise SetupError("Computer access is disabled on the Fetch relay.")
        if time.monotonic() >= deadline:
            break
        time.sleep(0.5)
    raise SetupError(f"The Fetch computer bridge did not come online: {last_reason}")


def configure(
    *,
    target: str,
    kind: str,
    name: str,
    display: str,
    xauthority: str,
    headed_browser: bool,
    vnc_password: str,
    wait_seconds: float,
    check_only: bool,
) -> dict:
    probe_desktop(target, password=vnc_password, wait_seconds=wait_seconds)
    credentials = _credentials()
    if not check_only:
        values = {
            TARGET_ENV: target,
            KIND_ENV: kind,
            TUNNEL_ENV: "1",
        }
        if name:
            values[NAME_ENV] = name
        if display:
            values[DISPLAY_ENV] = display
        if xauthority:
            values[XAUTHORITY_ENV] = xauthority
        if headed_browser:
            values[BROWSER_HEADED_ENV] = "1"
        if vnc_password:
            values[VNC_PASSWORD_ENV] = vnc_password
        persist_environment(hermes_home() / ".env", values)
        os.environ.update(values)
        if not vnc_password:
            remove_environment_keys(hermes_home() / ".env", (VNC_PASSWORD_ENV,))
            os.environ.pop(VNC_PASSWORD_ENV, None)
        computer_runtime = _computer_runtime_module()
        if not computer_runtime.restart_computer_runtime():
            raise SetupError("Could not restart the Fetch computer bridge.")
        runtime_status = computer_runtime.ensure_computer_runtime()
        if runtime_status not in {"started", "already-running", "self"}:
            raise SetupError(f"Could not start the Fetch computer bridge: {runtime_status}")

        relay_runtime = _relay_runtime_module()
        relay_handoff = relay_runtime.restart_relay_runtime_for_reconfigure()
        left_running = relay_handoff.get("left_running") or []
        fingerprint = _configuration_fingerprint(values)
        if left_running:
            if not _manual_gateway_adopted_configuration(fingerprint, left_running):
                raise SetupError(
                    "The desktop is configured, but a manually managed Hermes gateway is still "
                    "running with the old display environment. Restart that gateway, then rerun "
                    "this check."
                )
        else:
            _clear_gateway_restart_state()
            relay_runtime_status = relay_runtime.ensure_relay_runtime()
            if relay_runtime_status not in {"started", "already-running", "self"}:
                raise SetupError(
                    f"Could not restart the Hermes runtime on the visible desktop: "
                    f"{relay_runtime_status}"
                )
    return wait_for_relay(credentials, wait_seconds=wait_seconds)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--disable",
        action="store_true",
        help="Stop the computer bridge and remove persisted computer settings.",
    )
    parser.add_argument("--target")
    parser.add_argument("--kind")
    parser.add_argument("--name", default="")
    parser.add_argument("--display", default="")
    parser.add_argument("--xauthority", default="")
    parser.add_argument(
        "--headed-browser",
        action="store_true",
        help="Run Hermes' local browser visibly on the streamed desktop.",
    )
    parser.add_argument(
        "--ask-vnc-password",
        action="store_true",
        help="Securely prompt on this host for its dedicated VNC password.",
    )
    parser.add_argument("--wait-seconds", type=float, default=60.0)
    parser.add_argument("--check-only", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.disable:
            disable_computer()
            print("Fetch computer access is disabled on this machine.")
            return 0
        if not args.target or not args.kind:
            parser.error("--target and --kind are required unless --disable is set")
        vnc_password = configured_vnc_password()
        if args.ask_vnc_password:
            vnc_password = getpass.getpass("Dedicated Screen Sharing/VNC password: ")
            if not vnc_password:
                raise SetupError("A dedicated Screen Sharing/VNC password is required.")
        status = configure(
            target=args.target,
            kind=args.kind,
            name=args.name,
            display=args.display,
            xauthority=args.xauthority,
            headed_browser=args.headed_browser,
            vnc_password=vnc_password,
            wait_seconds=args.wait_seconds,
            check_only=args.check_only,
        )
    except (SetupError, ValueError) as exc:
        print(f"Fetch computer setup failed: {exc}", file=sys.stderr)
        return 1
    computer = status.get("status", {}).get("computer", {})
    label = computer.get("name") or args.name or "this computer"
    print(f"Fetch computer access is ready: {label} ({args.target})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
