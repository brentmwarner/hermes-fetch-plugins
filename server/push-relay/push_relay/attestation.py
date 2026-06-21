"""Pluggable app-attestation verification.

iOS uses Apple App Attest (verified here via pyattest). The Verifier protocol +
AttestedIdentity keep the relay's enrollment gate platform-agnostic so an Android
PlayIntegrityVerifier can be added later without touching the endpoint.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Protocol

from pyattest.attestation import Attestation
from pyattest.configs.apple import AppleConfig


class AttestationError(Exception):
    """Raised when an attestation payload fails verification."""


@dataclass(frozen=True)
class AttestedIdentity:
    platform: str
    key_id: str


class Verifier(Protocol):
    def verify(self, *, platform: str, payload: dict, challenge: str) -> AttestedIdentity: ...


class AppAttestVerifier:
    """Verifies Apple App Attest attestations. `payload` carries base64 `attestation`
    and the `key_id`; the raw server challenge is passed to pyattest as the nonce.

    Nonce handling: do NOT pre-hash the challenge. pyattest's Apple verifier (and a
    genuine iOS device) reconstruct the nonce as SHA256(authData + SHA256(nonce_arg)),
    where the device's clientDataHash == SHA256(challenge). So pyattest hashes the
    nonce arg itself once; passing the RAW challenge bytes makes the server's nonce
    match the device's. Pre-hashing here would double-hash and raise InvalidNonceException
    on every genuine attestation. `root_ca` is injectable for tests (a test root); None
    keeps Apple's real App Attest root in production."""

    def __init__(
        self,
        *,
        app_id: str,
        production: bool,
        root_ca: bytes | None = None,
        try_both_environments: bool = False,
    ) -> None:
        self._app_id = app_id
        self._production = production
        self._root_ca = root_ca
        self._try_both_environments = try_both_environments

    def verify(self, *, platform: str, payload: dict, challenge: str) -> AttestedIdentity:
        if platform != "ios":
            raise AttestationError(f"unsupported attestation platform: {platform}")
        key_id = str(payload.get("key_id") or "")
        attestation_b64 = str(payload.get("attestation") or "")
        if not key_id or not attestation_b64:
            raise AttestationError("missing key_id or attestation")
        try:
            attestation_obj = base64.b64decode(attestation_b64, validate=True)
            # The iOS DCAppAttestService returns the key id as base64 of the raw
            # SHA-256 of the attested public key. pyattest compares the config
            # key_id against that raw 32-byte digest, so decode it back to bytes.
            key_id_bytes = base64.b64decode(key_id, validate=True)
        except Exception as exc:
            raise AttestationError(f"malformed attestation payload: {exc}") from exc
        # Pass the RAW challenge as the nonce; pyattest hashes it internally to
        # reconstruct the device's clientDataHash (= SHA256(challenge)).
        nonce = challenge.encode("utf-8")
        # A genuine build attests in exactly one Apple environment: `development`
        # for Xcode / dev-provisioned builds, `production` for TestFlight & the App
        # Store. The two differ only by the App Attest aaguid pyattest checks. When
        # `try_both_environments` is set, attempt the configured environment first
        # then the other, so a single relay serves both build types. The Apple-rooted
        # cert chain and the app_id binding are enforced on BOTH attempts, so accepting
        # either environment does not weaken the enrollment gate.
        environments = [self._production]
        if self._try_both_environments:
            environments.append(not self._production)
        last_exc: Exception | None = None
        for production in environments:
            try:
                self._verify_in_environment(attestation_obj, nonce, key_id_bytes, production)
                return AttestedIdentity(
                    platform="ios", key_id=base64.b64encode(key_id_bytes).decode("ascii")
                )
            except Exception as exc:  # pyattest raises various exceptions on failure
                last_exc = exc
        assert last_exc is not None
        raise AttestationError(str(last_exc)) from last_exc

    def _verify_in_environment(
        self, attestation_obj: bytes, nonce: bytes, key_id_bytes: bytes, production: bool
    ) -> None:
        config = AppleConfig(
            key_id=key_id_bytes,
            app_id=self._app_id,
            production=production,
            root_ca=self._root_ca,
        )
        Attestation(attestation_obj, nonce, config).verify()
