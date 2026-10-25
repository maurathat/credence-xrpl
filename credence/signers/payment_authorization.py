"""X1a: immutable signed payment-authorization statement binding one provider Payment to Core.

A ``PaymentAuthorization`` names one Credence operation, the Core accounting settlement it pays
for, and the exact unsigned XRPL transaction that may pay it:

- the operation, facility, reservation and provider;
- the accounting-settled receipt digest, the facility's receipt-chain head and the verifier
  decision digest;
- the settled amount (integer micro-tRLUSD; tRLUSD is a test token), its XRPL wire currency and
  issuer, the source account and the destination account;
- the authorized unsigned-transaction digest and its scheme, the network ID and an expiry.

It is operational, not canonical: no frozen C1/C3A object, C3A key role or signature definition is
changed. ``credence.decision_record.authorized_transaction_digest`` stays null; populating it needs
separate approval. The statement's domain string differs from C3A's ``credence/signature/v1`` and
from the integration response-authorization domain, so it can never be confused with either.

The exact signed bytes are ``lp(domain) || Kessai canonical form of the statement`` where the
Kessai serializer is JCS (RFC 8785) + NFC and ``lp`` is a 4-byte big-endian length prefix. The
signature is Ed25519 (PyNaCl), by a Credence settlement-authority key held in its own key ring.

``authorization_id`` is the SHA-256 of the signed statement bytes. It is the X0t journal's
``payment_authorization_id``, so the outbox event links back to exactly this statement.

The statement is evidence that Credence's settlement authority asked for this payment. It is not
by itself sufficient: the policy signer re-checks every bound fact against Core's own records
before it signs (``credence.signers.policy_signer``).

Owner: Credence Core signers (X1a, authorized by Maura).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, fields
from typing import Protocol

import nacl.exceptions
import nacl.signing

from credence.core.kessai import JsonValue, canonicalize
from credence.core.primitives import Ed25519Signature, KeyId, Timestamp
from credence.core.signatures import KeyStatus, is_valid_public_key, key_id_for, public_key_bytes

AUTHORIZATION_DOMAIN = "credence/xrpl/payment-authorization/v1"
PROTOCOL = "credence.xrpl.payment_authorization"
VERSION = "1"
MAX_LIFETIME_SECONDS = 300
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_XRPL_DIGEST = re.compile(r"^[0-9A-F]{64}$")


class PaymentAuthorizationRefused(RuntimeError):
    """A payment authorization is not accepted. ``code`` is stable."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class PaymentAuthorization:
    operation_id: str
    facility_id: str
    reservation_id: str
    provider_id: str
    receipt_digest: str
    receipt_chain_head: str
    verifier_decision_digest: str
    accounting_settled_at: Timestamp
    amount_micro_trlusd: int
    currency_code: str
    asset_issuer: str
    source_account: str
    destination: str
    network_id: int
    unsigned_transaction_digest: str
    unsigned_transaction_digest_scheme: str
    expires_at: Timestamp
    authority_key_id: KeyId

    def __post_init__(self) -> None:
        for name in (
            "operation_id",
            "facility_id",
            "reservation_id",
            "provider_id",
            "currency_code",
            "asset_issuer",
            "source_account",
            "destination",
            "unsigned_transaction_digest_scheme",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or not value.isascii():
                raise PaymentAuthorizationRefused("MALFORMED_AUTHORIZATION", name)
        for name in ("receipt_digest", "receipt_chain_head", "verifier_decision_digest"):
            if not _SHA256.match(getattr(self, name)):
                raise PaymentAuthorizationRefused("MALFORMED_AUTHORIZATION", name)
        if not _XRPL_DIGEST.match(self.unsigned_transaction_digest):
            raise PaymentAuthorizationRefused(
                "MALFORMED_AUTHORIZATION", "unsigned_transaction_digest"
            )
        for name in ("amount_micro_trlusd", "network_id"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise PaymentAuthorizationRefused("MALFORMED_AUTHORIZATION", name)
        if not isinstance(self.authority_key_id, KeyId):
            raise PaymentAuthorizationRefused("MALFORMED_AUTHORIZATION", "authority_key_id")
        for name in ("accounting_settled_at", "expires_at"):
            if not isinstance(getattr(self, name), Timestamp):
                raise PaymentAuthorizationRefused("MALFORMED_AUTHORIZATION", name)

    def statement(self) -> dict[str, JsonValue]:
        body: dict[str, JsonValue] = {"protocol": PROTOCOL, "version": VERSION}
        for item in fields(self):
            value = getattr(self, item.name)
            body[item.name] = value.value if isinstance(value, (Timestamp, KeyId)) else value
        return body

    def statement_bytes(self) -> bytes:
        """The exact bytes signed: the length-prefixed domain, then the Kessai canonical form."""
        domain = AUTHORIZATION_DOMAIN.encode("ascii")
        return len(domain).to_bytes(4, "big") + domain + canonicalize(self.statement()).data

    def authorization_id(self) -> str:
        return hashlib.sha256(self.statement_bytes()).hexdigest()


@dataclass(frozen=True)
class SignedPaymentAuthorization:
    authorization: PaymentAuthorization
    signature: Ed25519Signature


@dataclass(frozen=True)
class AuthorityKey:
    key_id: KeyId
    status: KeyStatus


class SettlementAuthorityKeys:
    """Deterministic in-memory ring of Credence settlement-authority keys. No C3A role is added."""

    def __init__(self, keys: Iterable[AuthorityKey] = ()) -> None:
        self._keys: dict[KeyId, AuthorityKey] = {}
        for key in keys:
            if key.key_id in self._keys:
                raise ValueError(f"{key.key_id.value} is already registered")
            if not is_valid_public_key(key.key_id):
                raise ValueError(f"{key.key_id.value} is not a valid Ed25519 public key")
            self._keys[key.key_id] = key

    def revoke(self, key_id: KeyId) -> None:
        self._keys[key_id] = AuthorityKey(key_id, KeyStatus.REVOKED)

    def lookup(self, key_id: KeyId) -> AuthorityKey | None:
        return self._keys.get(key_id)


def sign_payment_authorization(
    authorization: PaymentAuthorization, signing_key: nacl.signing.SigningKey
) -> SignedPaymentAuthorization:
    """For the settlement authority. The key must be the one the statement names."""
    if key_id_for(signing_key.verify_key) != authorization.authority_key_id:
        raise PaymentAuthorizationRefused("WRONG_AUTHORITY_KEY")
    signature = signing_key.sign(authorization.statement_bytes()).signature
    return SignedPaymentAuthorization(authorization, Ed25519Signature(signature.hex()))


def verify_payment_authorization(
    signed: SignedPaymentAuthorization, keys: SettlementAuthorityKeys, *, now: Timestamp
) -> PaymentAuthorization:
    """Signature, key status and lifetime. Binding to Core and to the transaction is the signer's
    separate check."""
    if not isinstance(signed, SignedPaymentAuthorization):
        raise PaymentAuthorizationRefused("MALFORMED_AUTHORIZATION")
    authorization = signed.authorization
    key = keys.lookup(authorization.authority_key_id)
    if key is None or not is_valid_public_key(authorization.authority_key_id):
        raise PaymentAuthorizationRefused("UNKNOWN_AUTHORITY_KEY")
    try:
        nacl.signing.VerifyKey(public_key_bytes(authorization.authority_key_id)).verify(
            authorization.statement_bytes(), bytes.fromhex(signed.signature.value)
        )
    except (nacl.exceptions.BadSignatureError, nacl.exceptions.CryptoError, ValueError):
        raise PaymentAuthorizationRefused("BAD_AUTHORIZATION_SIGNATURE") from None
    if key.status is not KeyStatus.CURRENT:
        raise PaymentAuthorizationRefused("AUTHORITY_KEY_REVOKED")
    if not now.value < authorization.expires_at.value:
        raise PaymentAuthorizationRefused("AUTHORIZATION_EXPIRED")
    if now.plus_seconds(MAX_LIFETIME_SECONDS).value < authorization.expires_at.value:
        raise PaymentAuthorizationRefused("AUTHORIZATION_TOO_LONG")
    return authorization


# --- Core's accounting settlement, as the signer reads it ------------------------------------


@dataclass(frozen=True)
class AccountingSettlementFact:
    """One row of Core's ``accounting_settlements``, plus the receipt's provider.

    Core does not yet expose this read; see contracts/requests/0009-core-accounting-settlement-read.md.
    """

    receipt_digest: str
    reservation_id: str
    facility_id: str
    provider_id: str
    verifier_decision_digest: str
    actual_amount_micro_trlusd: int
    settled_at: Timestamp
    state: str


class AccountingSettlementSource(Protocol):
    """Read-only view of Core's accounting settlement and receipt chain."""

    def accounting_settlement(self, receipt_digest: str) -> AccountingSettlementFact | None: ...

    def chain_head(self, facility_id: str) -> str | None: ...
