#!/usr/bin/env python3
"""Configure and verify Fetch computer access on the current machine."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import socket
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
TUNNEL_ENV = "HERMES_FETCH_TUNNEL_ENABLED"
DISPLAY_ENV = "DISPLAY"


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
        stripped = line.strip()
        candidate = stripped[7:].lstrip() if stripped.startswith("export ") else stripped
        key, separator, _value = candidate.partition("=")
        key = key.strip()
        if separator and key in remaining:
            updated.append(f"{key}={_quoted_env_value(remaining.pop(key))}")
        else:
            updated.append(line)
    if remaining and updated and updated[-1].strip():
        updated.append("")
    for key, value in remaining.items():
        updated.append(f"{key}={_quoted_env_value(value)}")

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            handle.write("\n".join(updated).rstrip() + "\n")
            temporary_path = Path(handle.name)
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
    except OSError as exc:
        raise SetupError(f"Could not update {path}: {exc}") from exc


def _read_rfb_banner(sock: socket.socket) -> bytes:
    chunks: list[bytes] = []
    remaining = 12
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def probe_desktop(target: str, *, wait_seconds: float) -> None:
    computer = _load_sibling("fetch_plugin_computer_setup_probe", "_computer.py")
    target = computer.validate_local_target(target)
    parsed = urlsplit(target)
    if parsed.scheme != "tcp":
        raise SetupError("Automatic readiness checks require a tcp:// loopback VNC target.")

    deadline = time.monotonic() + max(0.0, wait_seconds)
    last_error = "no RFB response"
    while True:
        try:
            with socket.create_connection((parsed.hostname, parsed.port), timeout=2.0) as connection:
                connection.settimeout(2.0)
                banner = _read_rfb_banner(connection)
            if banner.startswith(b"RFB "):
                return
            last_error = f"unexpected protocol banner {banner[:12]!r}"
        except OSError as exc:
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
    wait_seconds: float,
    check_only: bool,
) -> dict:
    probe_desktop(target, wait_seconds=wait_seconds)
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
        persist_environment(hermes_home() / ".env", values)
        os.environ.update(values)
        computer_runtime = _load_sibling(
            "fetch_plugin_computer_runtime_setup",
            "_computer_runtime.py",
        )
        if not computer_runtime.restart_computer_runtime():
            raise SetupError("Could not restart the Fetch computer bridge.")
        runtime_status = computer_runtime.ensure_computer_runtime()
        if runtime_status not in {"started", "already-running", "self"}:
            raise SetupError(f"Could not start the Fetch computer bridge: {runtime_status}")
    return wait_for_relay(credentials, wait_seconds=wait_seconds)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True)
    parser.add_argument("--kind", required=True)
    parser.add_argument("--name", default="")
    parser.add_argument("--display", default="")
    parser.add_argument("--wait-seconds", type=float, default=60.0)
    parser.add_argument("--check-only", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        status = configure(
            target=args.target,
            kind=args.kind,
            name=args.name,
            display=args.display,
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
