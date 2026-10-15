"""Credence object signatures: Ed25519 over a domain-separated statement of a canonical digest.

Cryptography is PyNaCl (libsodium), already a locked dependency; nothing is implemented here.

Signed message (contracts/SIGNATURES.md), every field length-prefixed with a 4-byte big-endian
length so no concatenation is ambiguous:

    message = lp("credence/signature/v1") || lp(object_type) || lp(schema_version)
              || lp(object_digest)
    lp(x)   = uint32_be(len(x)) || x          (x is ASCII)

object_type and schema_version come from the object; object_digest is its `sha256:<hex>` digest
through the pinned Kessai serializer (JCS (RFC 8785) + NFC). The canonical bytes already contain
object_type and schema_version, so the statement binds the type twice. A signature is carried in a
SignedObject outside the body it signs; key_id is `ed25519:` + the public key, so the key_id *is*
the verifying key and is never secret.

These are Credence object signatures. They are not XRPL transaction signatures: no XRPL signing
hash, transaction blob or key derivation is involved.

This module answers only "was this object signed by a recognized key, and is that key current?".
Whether a purchase or delivery is allowed stays with the policy engine and the verifier.

Owner: Credence Core.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum

import nacl.bindings
import nacl.exceptions
import nacl.signing

from credence.core.objects import (
    SCHEMA_VERSION,
    CanonicalObject,
    DeliveryReceiptBody,
    Facility,
    ProviderAttestationBody,
    PurchaseRequest,
    SignedObject,
)
from credence.core.primitives import Ed25519Signature, KeyId
from credence.core.verifier import ProviderSignatureFact

SIGNATURE_DOMAIN = "credence/signature/v1"
_LENGTH_BYTES = 4


class KeyRole(Enum):
    """What a key may sign: the agent signs purchase requests, the gateway signs receipts,
    a provider signs its attestations."""

    AGENT = "agent"
    GATEWAY = "gateway"
    PROVIDER = "provider"


class KeyStatus(Enum):
    CURRENT = "current"
    REVOKED = "revoked"


ROLE_OBJECT_TYPES: dict[KeyRole, str] = {
    KeyRole.AGENT: PurchaseRequest.OBJECT_TYPE,
    KeyRole.GATEWAY: DeliveryReceiptBody.OBJECT_TYPE,
    KeyRole.PROVIDER: ProviderAttestationBody.OBJECT_TYPE,
}


class KeyRegistryError(ValueError):
    """A registration the registry refuses (duplicate or malformed key)."""


# --- message ---------------------------------------------------------------------------------


def _lp(text: str) -> bytes:
    data = text.encode("ascii")
    return len(data).to_bytes(_LENGTH_BYTES, "big") + data


def statement_bytes(object_type: str, schema_version: str, object_digest: str) -> bytes:
    """The signed statement for an (object type, schema version, digest) triple."""
    return _lp(SIGNATURE_DOMAIN) + _lp(object_type) + _lp(schema_version) + _lp(object_digest)


def signature_message(obj: CanonicalObject) -> bytes:
    """The exact bytes signed for `obj`: the domain-separated statement of its canonical digest."""
    return statement_bytes(obj.OBJECT_TYPE, SCHEMA_VERSION, obj.digest().value)


def key_id_for(verify_key: nacl.signing.VerifyKey) -> KeyId:
    return KeyId("ed25519:" + bytes(verify_key).hex())


def public_key_bytes(key_id: KeyId) -> bytes:
    return bytes.fromhex(key_id.value.removeprefix("ed25519:"))


def is_valid_public_key(key_id: KeyId) -> bool:
    """A 32-byte encoding of a valid, non-small-order Ed25519 point."""
    return bool(nacl.bindings.crypto_core_ed25519_is_valid_point(public_key_bytes(key_id)))


def sign(obj: CanonicalObject, signing_key: nacl.signing.SigningKey) -> SignedObject:
    """Sign `obj` (its domain-separated digest statement). The private key is never stored."""
    signature = signing_key.sign(signature_message(obj)).signature
    return SignedObject(
        object_digest=obj.digest(),
        key_id=key_id_for(signing_key.verify_key),
        signature=Ed25519Signature(signature.hex()),
    )


# --- key registry ----------------------------------------------------------------------------


@dataclass(frozen=True)
class RegisteredKey:
    """A recognized key: its role, the subject it speaks for, and whether it is current.

    subject: the borrower account (agent keys), the provider ID (provider keys), or the gateway's
    own name (gateway keys).
    """

    key_id: KeyId
    role: KeyRole
    subject: str
    status: KeyStatus


class KeyRegistry:
    """Deterministic in-memory registry of recognized keys. No database, network or XRPL lookup."""

    def __init__(self, keys: Iterable[RegisteredKey] = ()) -> None:
        self._keys: dict[KeyId, RegisteredKey] = {}
        for key in keys:
            self.register(key)

    def register(self, key: RegisteredKey) -> None:
        if not isinstance(key.key_id, KeyId) or not isinstance(key.role, KeyRole):
            raise KeyRegistryError("key_id and role must be typed")
        if not isinstance(key.status, KeyStatus) or not key.subject:
            raise KeyRegistryError("status must be typed and subject non-empty")
        if key.key_id in self._keys:
            raise KeyRegistryError(f"{key.key_id.value} is already registered")
        if not is_valid_public_key(key.key_id):
            raise KeyRegistryError(f"{key.key_id.value} is not a valid Ed25519 public key")
        self._keys[key.key_id] = key

    def revoke(self, key_id: KeyId) -> None:
        key = self._keys[key_id]
        self._keys[key_id] = RegisteredKey(key.key_id, key.role, key.subject, KeyStatus.REVOKED)

    def lookup(self, key_id: KeyId) -> RegisteredKey | None:
        return self._keys.get(key_id)


# --- verification ----------------------------------------------------------------------------


class SignatureFailure(Enum):
    """Why a signature is not accepted, checked in this order."""

    WRONG_OBJECT_TYPE = "WRONG_OBJECT_TYPE"
    DIGEST_MISMATCH = "DIGEST_MISMATCH"
    UNKNOWN_KEY = "UNKNOWN_KEY"
    WRONG_ROLE = "WRONG_ROLE"
    WRONG_SUBJECT = "WRONG_SUBJECT"
    INVALID_PUBLIC_KEY = "INVALID_PUBLIC_KEY"
    BAD_SIGNATURE = "BAD_SIGNATURE"
    REVOKED_KEY = "REVOKED_KEY"


@dataclass(frozen=True)
class Verification:
    """signature_valid: the object's statement verifies under a key registered for this role and
    subject (current or revoked). key_current: that key is also current. failure: the first
    reason either is false, or None."""

    signature_valid: bool
    key_current: bool
    failure: SignatureFailure | None

    @property
    def accepted(self) -> bool:
        return self.signature_valid and self.key_current


def _reject(failure: SignatureFailure) -> Verification:
    return Verification(signature_valid=False, key_current=False, failure=failure)


def verify_signed(
    obj: CanonicalObject,
    signed: SignedObject,
    registry: KeyRegistry,
    *,
    role: KeyRole,
    subject: str | None,
) -> Verification:
    """Verify that `signed` is a signature over `obj` by a recognized key for `role`/`subject`.

    subject None accepts any subject for the role (used for the gateway). Fails closed.
    """
    if not isinstance(signed, SignedObject) or obj.OBJECT_TYPE != ROLE_OBJECT_TYPES[role]:
        return _reject(SignatureFailure.WRONG_OBJECT_TYPE)
    if signed.object_digest != obj.digest():
        return _reject(SignatureFailure.DIGEST_MISMATCH)
    key = registry.lookup(signed.key_id)
    if key is None:
        return _reject(SignatureFailure.UNKNOWN_KEY)
    if key.role is not role:
        return _reject(SignatureFailure.WRONG_ROLE)
    if subject is not None and key.subject != subject:
        return _reject(SignatureFailure.WRONG_SUBJECT)
    if not is_valid_public_key(signed.key_id):
        return _reject(SignatureFailure.INVALID_PUBLIC_KEY)
    try:
        nacl.signing.VerifyKey(public_key_bytes(signed.key_id)).verify(
            signature_message(obj), bytes.fromhex(signed.signature.value)
        )
    except (nacl.exceptions.BadSignatureError, nacl.exceptions.CryptoError, ValueError):
        return _reject(SignatureFailure.BAD_SIGNATURE)
    current = key.status is KeyStatus.CURRENT
    return Verification(
        signature_valid=True,
        key_current=current,
        failure=None if current else SignatureFailure.REVOKED_KEY,
    )


# --- facts for the C2 engines ----------------------------------------------------------------


@dataclass(frozen=True)
class PolicySignatureFacts:
    request_signature_valid: bool
    working_key_current: bool


@dataclass(frozen=True)
class VerifierSignatureFacts:
    gateway_signature_valid: bool
    provider_signature: ProviderSignatureFact


def policy_signature_facts(
    request: PurchaseRequest,
    signed: SignedObject | None,
    facility: Facility,
    registry: KeyRegistry,
) -> PolicySignatureFacts:
    """The facts PolicyInput needs. The key must be an agent key for the facility's borrower.

    A revoked agent key that did sign the request yields (True, False), so the policy engine
    reports KEY_REVOKED; anything else that fails yields (False, False) and SIG_INVALID.
    """
    if signed is None:
        return PolicySignatureFacts(request_signature_valid=False, working_key_current=False)
    result = verify_signed(
        request, signed, registry, role=KeyRole.AGENT, subject=facility.borrower_account.value
    )
    return PolicySignatureFacts(
        request_signature_valid=result.signature_valid, working_key_current=result.key_current
    )


def verifier_signature_facts(
    receipt: DeliveryReceiptBody,
    gateway_signed: SignedObject | None,
    attestation: ProviderAttestationBody | None,
    provider_signed: SignedObject | None,
    registry: KeyRegistry,
) -> VerifierSignatureFacts:
    """The facts VerifierInput needs.

    Gateway: valid only under a current gateway key. Provider: NOT_PRESENT when no provider
    signature is supplied (Tier 1); otherwise VALID only if it verifies over the attestation under
    a current provider key whose subject is the receipt's provider_id, else INVALID. Whether the
    tier requires the provider signature is the verifier's decision.
    """
    gateway_valid = gateway_signed is not None and (
        verify_signed(
            receipt, gateway_signed, registry, role=KeyRole.GATEWAY, subject=None
        ).accepted
    )
    if provider_signed is None:
        provider = ProviderSignatureFact.NOT_PRESENT
    elif attestation is None:
        provider = ProviderSignatureFact.INVALID
    else:
        result = verify_signed(
            attestation,
            provider_signed,
            registry,
            role=KeyRole.PROVIDER,
            subject=receipt.provider_id.value,
        )
        provider = ProviderSignatureFact.VALID if result.accepted else ProviderSignatureFact.INVALID
    return VerifierSignatureFacts(
        gateway_signature_valid=gateway_valid, provider_signature=provider
    )
