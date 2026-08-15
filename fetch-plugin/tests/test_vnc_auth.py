from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_path = Path(__file__).resolve().parent.parent / "_vnc_auth.py"
_spec = importlib.util.spec_from_file_location("fetch_plugin_vnc_auth_test", _path)
vnc_auth = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = vnc_auth
_spec.loader.exec_module(vnc_auth)


def test_vnc_challenge_response_matches_des_reference_vector() -> None:
    assert vnc_auth.challenge_response("secret", bytes(range(16))) == bytes.fromhex(
        "ee22539f33a5983ec12f9c2edbc995dd"
    )


def test_vnc_password_uses_only_first_eight_bytes() -> None:
    challenge = bytes(range(16))
    assert vnc_auth.challenge_response("12345678ignored", challenge) == (
        vnc_auth.challenge_response("12345678", challenge)
    )


def test_vnc_challenge_must_be_exactly_two_blocks() -> None:
    with pytest.raises(ValueError, match="16 bytes"):
        vnc_auth.challenge_response("secret", b"short")
