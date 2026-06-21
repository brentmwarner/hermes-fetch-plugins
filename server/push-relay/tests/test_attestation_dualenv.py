"""AppAttestVerifier must accept either Apple App Attest environment when
`try_both_environments` is set, so one relay serves Xcode/dev builds and
TestFlight/App Store builds. We stub the per-environment crypto check and
assert only the environment-selection logic.
"""

import base64

import pytest

from push_relay.attestation import AppAttestVerifier, AttestationError

_KEY_ID = base64.b64encode(b"\x11" * 32).decode("ascii")
_ATTESTATION = base64.b64encode(b"fake-attestation-bytes").decode("ascii")
_PAYLOAD = {"key_id": _KEY_ID, "attestation": _ATTESTATION}
_CHALLENGE = "deadbeef" * 8


def _verifier(*, production, try_both):
    return AppAttestVerifier(
        app_id="GCYB5LT4QQ.com.brentwarner.fetch",
        production=production,
        try_both_environments=try_both,
    )


def test_falls_back_to_development_when_both_enabled(monkeypatch):
    # Simulate a dev build: production verify fails, development verify succeeds.
    attempted = []

    def fake_verify(self, attestation_obj, nonce, key_id_bytes, production):
        attempted.append(production)
        if production:
            raise ValueError("aaguid mismatch (prod)")
        return None  # development succeeds

    monkeypatch.setattr(AppAttestVerifier, "_verify_in_environment", fake_verify)

    ident = _verifier(production=True, try_both=True).verify(
        platform="ios", payload=_PAYLOAD, challenge=_CHALLENGE
    )
    assert attempted == [True, False]  # primary first, then fallback
    assert ident.platform == "ios"
    assert ident.key_id == _KEY_ID


def test_no_fallback_when_disabled(monkeypatch):
    attempted = []

    def fake_verify(self, attestation_obj, nonce, key_id_bytes, production):
        attempted.append(production)
        if production:
            raise ValueError("aaguid mismatch (prod)")

    monkeypatch.setattr(AppAttestVerifier, "_verify_in_environment", fake_verify)

    with pytest.raises(AttestationError):
        _verifier(production=True, try_both=False).verify(
            platform="ios", payload=_PAYLOAD, challenge=_CHALLENGE
        )
    assert attempted == [True]  # only the configured environment, no fallback


def test_primary_success_short_circuits(monkeypatch):
    attempted = []

    def fake_verify(self, attestation_obj, nonce, key_id_bytes, production):
        attempted.append(production)
        return None  # production succeeds immediately

    monkeypatch.setattr(AppAttestVerifier, "_verify_in_environment", fake_verify)

    ident = _verifier(production=True, try_both=True).verify(
        platform="ios", payload=_PAYLOAD, challenge=_CHALLENGE
    )
    assert attempted == [True]  # second env not attempted once the first passes
    assert ident.key_id == _KEY_ID


def test_malformed_payload_raises_attestation_error():
    with pytest.raises(AttestationError):
        _verifier(production=True, try_both=True).verify(
            platform="ios",
            payload={"key_id": "!!notbase64!!", "attestation": _ATTESTATION},
            challenge=_CHALLENGE,
        )
