import base64
import hashlib
import os
import sys
from pathlib import Path

import cbor2
import pytest

from push_relay import attestation as att

# A real iOS key id is base64 of the raw SHA-256 of the attested public key.
KEY_ID_B64 = base64.b64encode(b"k1" * 16).decode("ascii")  # 32 raw bytes -> valid base64


def test_verify_returns_identity_on_success(monkeypatch):
    captured = {}
    def fake_verify(self):
        captured["called"] = True       # pyattest raises on failure; success = no raise
    monkeypatch.setattr(att.Attestation, "verify", fake_verify, raising=True)
    v = att.AppAttestVerifier(app_id="GCYB5LT4QQ.com.brentwarner.fetch", production=True)
    ident = v.verify(platform="ios", payload={"attestation": "AAAA", "key_id": KEY_ID_B64}, challenge="abc")
    assert captured["called"] is True
    assert ident.key_id == KEY_ID_B64


def test_verify_raises_on_library_failure(monkeypatch):
    def boom(self):
        raise ValueError("bad cert chain")
    monkeypatch.setattr(att.Attestation, "verify", boom, raising=True)
    v = att.AppAttestVerifier(app_id="GCYB5LT4QQ.com.brentwarner.fetch", production=True)
    with pytest.raises(att.AttestationError, match="bad cert chain"):
        v.verify(platform="ios", payload={"attestation": "AAAA", "key_id": KEY_ID_B64}, challenge="abc")


def test_verify_raises_on_missing_fields():
    v = att.AppAttestVerifier(app_id="x.y", production=True)
    with pytest.raises(att.AttestationError, match="missing"):
        v.verify(platform="ios", payload={"key_id": "k"}, challenge="c")


def test_verify_rejects_non_ios_platform():
    v = att.AppAttestVerifier(app_id="x.y", production=True)
    with pytest.raises(att.AttestationError):
        v.verify(platform="android", payload={"attestation": "A", "key_id": "k"}, challenge="abc")


def _build_factory_attestation(app_id: str, challenge: str):
    """Build a genuine (factory-signed) Apple attestation for `challenge`.

    Uses pyattest's bundled test factory. The factory imports `_cbor2` (the C
    extension), which isn't built for this interpreter; alias it to the pure-python
    `cbor2` so the import succeeds. Returns (attestation_b64, key_id_b64, root_ca_pem).
    """
    sys.modules.setdefault("_cbor2", cbor2)
    from asn1crypto.x509 import Certificate as ASN1Cert
    from pyattest.testutils.factories.attestation import apple

    # The verifier's contract: it passes `challenge.encode("utf-8")` as pyattest's
    # nonce, so build the factory attestation against that same raw nonce.
    nonce = challenge.encode("utf-8")
    attestation_bytes, _public_key = apple.get(app_id=app_id, nonce=nonce)

    # Device-style key id = base64 of the leaf cert public key's SHA-256, computed
    # exactly as pyattest's verifier does (asn1crypto Certificate.public_key.sha256).
    decoded = cbor2.loads(attestation_bytes)
    leaf_der = decoded["attStmt"]["x5c"][0]
    key_id_bytes = ASN1Cert.load(leaf_der).public_key.sha256
    key_id_b64 = base64.b64encode(key_id_bytes).decode("ascii")

    fixtures = Path(apple.__file__).resolve().parent.parent.parent / "fixtures"
    root_ca_pem = (fixtures / "root_cert.pem").read_bytes()

    return base64.b64encode(attestation_bytes).decode("ascii"), key_id_b64, root_ca_pem


def test_verify_accepts_real_factory_attestation():
    """End-to-end, NO monkeypatch: a genuine factory attestation verifies only when
    the raw challenge is passed as pyattest's nonce. This guards the double-hash bug:
    pre-hashing the challenge raises InvalidNonceException (see the negative test)."""
    app_id = "GCYB5LT4QQ.com.brentwarner.fetch"
    challenge = "server-challenge-xyz"
    attestation_b64, key_id_b64, root_ca = _build_factory_attestation(app_id, challenge)

    v = att.AppAttestVerifier(app_id=app_id, production=False, root_ca=root_ca)
    ident = v.verify(
        platform="ios",
        payload={"attestation": attestation_b64, "key_id": key_id_b64},
        challenge=challenge,
    )
    assert ident.platform == "ios"
    assert ident.key_id == key_id_b64


def test_real_factory_attestation_rejects_wrong_nonce():
    """Regression guard for the double-hash bug. The attestation above is built for
    nonce == raw challenge; pyattest hashes that nonce arg once internally. If the
    verifier instead pre-hashed the challenge (the old bug), the nonce would no longer
    match. We reproduce that mismatch by verifying the attestation against a different
    challenge value, which must be rejected. Combined with the success test, this proves
    the verify only passes when the raw-challenge nonce contract is honored."""
    app_id = "GCYB5LT4QQ.com.brentwarner.fetch"
    attestation_b64, key_id_b64, root_ca = _build_factory_attestation(app_id, "server-challenge-xyz")

    v = att.AppAttestVerifier(app_id=app_id, production=False, root_ca=root_ca)
    with pytest.raises(att.AttestationError):
        v.verify(
            platform="ios",
            payload={"attestation": attestation_b64, "key_id": key_id_b64},
            challenge="a-different-challenge",  # nonce no longer matches the attestation
        )
