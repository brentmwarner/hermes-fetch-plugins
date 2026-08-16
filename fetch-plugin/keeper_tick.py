"""One supervised keeper tick: respawn missing Fetch runtimes.

Run by the ``fetch-runtime-keeper`` systemd user timer (see
``_runtime.ensure_keeper_units``). In-process keepers die with their host
process; this tick survives them all, so a relay/computer runtime lost to an
interrupted reconfigure heals within a minute. Both ensures are pid-file
guarded and idempotent, so overlapping with in-process keepers is safe.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent

try:
    from hermes_cli.env_loader import load_hermes_dotenv

    load_hermes_dotenv()
except Exception:
    pass


def _load(module_name: str, filename: str):
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(module_name, PLUGIN_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    runtime = _load("fetch_plugin_runtime", "_runtime.py")
    if not runtime.keeper_should_run():
        print("gate closed (tunnel disabled or unpaired); nothing to keep")
        return 0
    relay_status = runtime.ensure_relay_runtime()
    computer = _load("fetch_plugin_computer_runtime", "_computer_runtime.py")
    # Keeper-scoped variant: never resurrect a bridge whose persisted computer
    # settings were removed by a disable/reconfigure elsewhere.
    computer_status = computer.keeper_ensure_computer_runtime()
    print(f"relay={relay_status} computer={computer_status}")
    if relay_status == "started" or computer_status == "started":
        print("keeper tick respawned a missing runtime")
    return 0


if __name__ == "__main__":
    sys.exit(main())
