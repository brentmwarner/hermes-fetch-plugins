import importlib.util
import sys
from pathlib import Path

import pytest

_p = Path(__file__).resolve().parent.parent / "_owner.py"
_spec = importlib.util.spec_from_file_location("fetch_plugin_owner_policy_test", _p)
owner = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = owner
_spec.loader.exec_module(owner)


def test_default_profile_is_backwards_compatible_owner(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv(owner.OWNER_PROFILE_ENV, raising=False)
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    monkeypatch.setattr(owner, "default_home", lambda: tmp_path)
    monkeypatch.setattr(owner, "_current_home", lambda: tmp_path)

    assert owner.owner_profile_name() == "default"
    assert owner.current_profile_name() == "default"
    assert owner.is_owner_profile() is True


def test_root_config_owner_is_visible_and_normalized_from_specialist(monkeypatch, tmp_path) -> None:
    (tmp_path / ".env").write_text(
        'HERMES_FETCH_OWNER_PROFILE="ReSeArChEr"\n', encoding="utf-8"
    )
    monkeypatch.delenv(owner.OWNER_PROFILE_ENV, raising=False)
    monkeypatch.setenv("HERMES_PROFILE", "RESEARCHER")
    monkeypatch.setattr(owner, "default_home", lambda: tmp_path)

    assert owner.owner_profile_name() == "researcher"
    assert owner.is_owner_profile() is True


def test_non_owner_delivery_routes_to_default_mobile_home(monkeypatch, tmp_path) -> None:
    specialist = tmp_path / "profiles" / "coder"
    monkeypatch.setenv(owner.OWNER_PROFILE_ENV, "default")
    monkeypatch.setenv("HERMES_PROFILE", "coder")
    monkeypatch.setattr(owner, "default_home", lambda: tmp_path)
    monkeypatch.setattr(owner, "_current_home", lambda: specialist)
    monkeypatch.delenv(owner.STORE_HOME_ENV, raising=False)

    assert owner.is_owner_profile() is False
    assert owner.delivery_home() == tmp_path


def test_explicit_store_home_remains_bounded_routing_override(monkeypatch, tmp_path) -> None:
    routed = tmp_path / "mobile-owner"
    monkeypatch.setenv(owner.STORE_HOME_ENV, str(routed))

    assert owner.delivery_home() == routed


def test_custom_single_home_remains_the_default_owner_home(monkeypatch, tmp_path) -> None:
    custom_home = tmp_path / "custom-hermes"
    monkeypatch.setenv(owner.OWNER_PROFILE_ENV, "default")
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    monkeypatch.setattr(owner, "_current_home", lambda: custom_home)

    assert owner.current_profile_name() == "default"
    assert owner.owner_home() == custom_home
    assert owner.delivery_home() == custom_home


def test_invalid_owner_setting_fails_closed(monkeypatch) -> None:
    monkeypatch.setenv(owner.OWNER_PROFILE_ENV, "not a profile")
    monkeypatch.setenv("HERMES_PROFILE", "default")

    assert owner.is_owner_profile() is False
    status = owner.policy_status()
    assert status["valid"] is False
    assert status["is_owner"] is False
    assert "invalid Fetch owner profile" in str(status["error"])


def test_worker_reads_durable_store_home_without_inherited_env(monkeypatch, tmp_path):
    paired = tmp_path / "paired-store"
    (tmp_path / ".env").write_text(f'HERMES_FETCH_STORE_HOME="{paired}"\n')
    monkeypatch.delenv(owner.STORE_HOME_ENV, raising=False)
    monkeypatch.setenv("HERMES_PROFILE", "researcher")
    monkeypatch.setattr(owner, "owner_home", lambda: tmp_path)
    assert owner.delivery_home() == paired
