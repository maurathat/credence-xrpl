"""Shared X1a offline world: deterministic TEST keys, accounts, ledger state and authorizations.

NOT FOR PRODUCTION. Every XRPL and Ed25519 key is derived at runtime from a public label, so no
key material is committed and anyone can rebuild the same keys.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from nacl.signing import SigningKey
from xrpl.constants import CryptoAlgorithm
from xrpl.core.addresscodec import encode_seed
from xrpl.core.keypairs import derive_classic_address, derive_keypair

from credence.core.primitives import Timestamp
from credence.core.signatures import KeyStatus, key_id_for
from credence.signers.payment_authorization import (
    AccountingSettlementFact,
    AuthorityKey,
    PaymentAuthorization,
    SettlementAuthorityKeys,
    SignedPaymentAuthorization,
    sign_payment_authorization,
)
from credence.signers.policy_signer import PolicySigner, PolicySignerConfig, PolicySigningLog
from credence.xrpl_adapter.multisign import (
    UNSIGNED_DIGEST_SCHEME,
    AccountSnapshot,
    ExpectedPayment,
    LedgerContext,
    MasterKeyPolicy,
    MultisignPolicy,
    MultisignSignature,
    SignerEntry,
    SignerListSnapshot,
    build_unsigned_payment,
    sign_as_signer,
    unsigned_transaction_digest,
)
from credence.xrpl_adapter.preparation import TRLUSD_WIRE_CODE
from credence.xrpl_adapter.types import Asset

TEST_KEY_PREFIX = b"CREDENCE TEST KEY - NOT FOR PRODUCTION - "


@dataclass(frozen=True)
class XrplTestKey:
    public_key: str
    private_key: str
    account: str

    def __repr__(self) -> str:
        return f"XrplTestKey({self.account})"


def xrpl_test_key(label: str) -> XrplTestKey:
    entropy = hashlib.sha256(TEST_KEY_PREFIX + label.encode("ascii")).digest()[:16]
    public_key, private_key = derive_keypair(encode_seed(entropy, CryptoAlgorithm.ED25519))
    return XrplTestKey(public_key, private_key, derive_classic_address(public_key))


def ed25519_test_key(label: str) -> SigningKey:
    return SigningKey(hashlib.sha256(TEST_KEY_PREFIX + label.encode("ascii")).digest())


OWNER = xrpl_test_key("x1a-agent-payment-account-owner-master")
RESEARCHBOT = xrpl_test_key("x1a-researchbot-signer")
POLICY = xrpl_test_key("x1a-credence-policy-signer")
ISSUER = xrpl_test_key("x1a-trlusd-issuer")
PROVIDER_A = xrpl_test_key("x1a-provider-a-settlement")
PROVIDER_B = xrpl_test_key("x1a-provider-b-settlement")
ATTACKER = xrpl_test_key("x1a-attacker")
OTHER_ISSUER = xrpl_test_key("x1a-other-issuer")

AUTHORITY = ed25519_test_key("x1a-credence-settlement-authority")
AUTHORITY_RETIRED = ed25519_test_key("x1a-credence-settlement-authority-retired")
UNREGISTERED_AUTHORITY = ed25519_test_key("x1a-unregistered-authority")

NETWORK_ID = 21_337
VALIDATED_LEDGER = 100
LEDGER_HASH = "A1" * 32
BASE_FEE = 10
FEE = 30
SEQUENCE = 7
TICKET = 55
LAST_LEDGER = 110
AMOUNT = 1_500_000
NOW = Timestamp("2026-10-24T18:00:00Z")
EXPIRES = Timestamp("2026-10-24T18:04:00Z")
SETTLED_AT = Timestamp("2026-10-24T17:59:30Z")
RECEIPT = "sha256:" + "1" * 64
OTHER_RECEIPT = "sha256:" + "3" * 64
DECISION = "sha256:" + "2" * 64
OPERATION = "operation-x1a-1"
FACILITY = "facility-x1a"
RESERVATION = "reservation-x1a-1"
PROVIDER_ID = "provider-a"

ASSET = Asset(label="tRLUSD", currency_code=TRLUSD_WIRE_CODE, issuer=ISSUER.account)

RULES = MultisignPolicy(
    account=OWNER.account,
    researchbot_account=RESEARCHBOT.account,
    policy_signer_account=POLICY.account,
    network_id=NETWORK_ID,
    master_key=MasterKeyPolicy.OWNER_RETAINED,
    maximum_fee_drops=100,
    maximum_ledger_window=20,
)

SIGNER_CONFIG = PolicySignerConfig(
    policy=RULES,
    asset=ASSET,
    provider_destinations={PROVIDER_ID: PROVIDER_A.account, "provider-b": PROVIDER_B.account},
)


def signer_list(**changes: Any) -> SignerListSnapshot:
    fields: dict[str, Any] = {
        "account": OWNER.account,
        "quorum": 2,
        "entries": (SignerEntry(RESEARCHBOT.account, 1), SignerEntry(POLICY.account, 1)),
    }
    fields.update(changes)
    return SignerListSnapshot(**fields)


def account_snapshot(**changes: Any) -> AccountSnapshot:
    fields: dict[str, Any] = {
        "account": OWNER.account,
        "exists": True,
        "validated": True,
        "ledger_index": VALIDATED_LEDGER,
        "ledger_hash": LEDGER_HASH,
        "flags": 0,
        "regular_key": None,
        "sequence": SEQUENCE,
        "balance_drops": 50_000_000,
        "owner_count": 2,
        "tickets": (TICKET,),
        "signer_list": signer_list(),
    }
    fields.update(changes)
    return AccountSnapshot(**fields)


def signer_snapshot(key: XrplTestKey, **changes: Any) -> AccountSnapshot:
    fields: dict[str, Any] = {
        "account": key.account,
        "exists": False,
        "validated": True,
        "ledger_index": VALIDATED_LEDGER,
        "ledger_hash": LEDGER_HASH,
    }
    fields.update(changes)
    return AccountSnapshot(**fields)


def ledger_context(
    *,
    account: AccountSnapshot | None = None,
    signers: dict[str, AccountSnapshot] | None = None,
    **changes: Any,
) -> LedgerContext:
    fields: dict[str, Any] = {
        "validated_ledger_index": VALIDATED_LEDGER,
        "validated_ledger_hash": LEDGER_HASH,
        "base_fee_drops": BASE_FEE,
        "network_id": NETWORK_ID,
        "account": account or account_snapshot(),
        "signers": signers
        or {
            RESEARCHBOT.account: signer_snapshot(RESEARCHBOT),
            POLICY.account: signer_snapshot(POLICY),
        },
    }
    fields.update(changes)
    return LedgerContext(**fields)


def unsigned_payment(**changes: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "account": OWNER.account,
        "destination": PROVIDER_A.account,
        "asset": ASSET,
        "amount_micro_units": AMOUNT,
        "fee_drops": FEE,
        "network_id": NETWORK_ID,
        "last_ledger_sequence": LAST_LEDGER,
        "sequence": SEQUENCE,
    }
    arguments.update(changes)
    return build_unsigned_payment(**arguments)


def expected_payment(transaction: dict[str, Any], **changes: Any) -> ExpectedPayment:
    fields: dict[str, Any] = {
        "account": OWNER.account,
        "destination": PROVIDER_A.account,
        "asset": ASSET,
        "amount_micro_units": AMOUNT,
        "network_id": NETWORK_ID,
        "unsigned_transaction_digest": unsigned_transaction_digest(transaction),
    }
    fields.update(changes)
    return ExpectedPayment(**fields)


def researchbot_signature(transaction: dict[str, Any]) -> MultisignSignature:
    return sign_as_signer(
        transaction,
        account=RESEARCHBOT.account,
        public_key=RESEARCHBOT.public_key,
        private_key=RESEARCHBOT.private_key,
    )


def direct_policy_signature(transaction: dict[str, Any]) -> MultisignSignature:
    return sign_as_signer(
        transaction,
        account=POLICY.account,
        public_key=POLICY.public_key,
        private_key=POLICY.private_key,
    )


def settlement_fact(**changes: Any) -> AccountingSettlementFact:
    fields: dict[str, Any] = {
        "receipt_digest": RECEIPT,
        "reservation_id": RESERVATION,
        "facility_id": FACILITY,
        "provider_id": PROVIDER_ID,
        "verifier_decision_digest": DECISION,
        "actual_amount_micro_trlusd": AMOUNT,
        "settled_at": SETTLED_AT,
        "state": "accounting_settled",
    }
    fields.update(changes)
    return AccountingSettlementFact(**fields)


class FakeSettlementSource:
    """Stands in for Core's read-only accounting settlement view (request 0009)."""

    def __init__(
        self,
        facts: tuple[AccountingSettlementFact, ...] | None = None,
        heads: dict[str, str] | None = None,
    ) -> None:
        facts = (settlement_fact(),) if facts is None else facts
        self.facts = {fact.receipt_digest: fact for fact in facts}
        self.heads = {FACILITY: RECEIPT} if heads is None else heads

    def accounting_settlement(self, receipt_digest: str) -> AccountingSettlementFact | None:
        return self.facts.get(receipt_digest)

    def chain_head(self, facility_id: str) -> str | None:
        return self.heads.get(facility_id)


def authority_keys() -> SettlementAuthorityKeys:
    return SettlementAuthorityKeys(
        [
            AuthorityKey(key_id_for(AUTHORITY.verify_key), KeyStatus.CURRENT),
            AuthorityKey(key_id_for(AUTHORITY_RETIRED.verify_key), KeyStatus.REVOKED),
        ]
    )


def authorization(transaction: dict[str, Any], **changes: Any) -> PaymentAuthorization:
    fields: dict[str, Any] = {
        "operation_id": OPERATION,
        "facility_id": FACILITY,
        "reservation_id": RESERVATION,
        "provider_id": PROVIDER_ID,
        "receipt_digest": RECEIPT,
        "receipt_chain_head": RECEIPT,
        "verifier_decision_digest": DECISION,
        "accounting_settled_at": SETTLED_AT,
        "amount_micro_trlusd": AMOUNT,
        "currency_code": TRLUSD_WIRE_CODE,
        "asset_issuer": ISSUER.account,
        "source_account": OWNER.account,
        "destination": PROVIDER_A.account,
        "network_id": NETWORK_ID,
        "unsigned_transaction_digest": unsigned_transaction_digest(transaction),
        "unsigned_transaction_digest_scheme": UNSIGNED_DIGEST_SCHEME,
        "expires_at": EXPIRES,
        "authority_key_id": key_id_for(AUTHORITY.verify_key),
    }
    fields.update(changes)
    return PaymentAuthorization(**fields)


def signed_authorization(
    transaction: dict[str, Any], *, key: SigningKey = AUTHORITY, **changes: Any
) -> SignedPaymentAuthorization:
    changes.setdefault("authority_key_id", key_id_for(key.verify_key))
    return sign_payment_authorization(authorization(transaction, **changes), key)


def policy_signer(
    directory: Path,
    *,
    source: FakeSettlementSource | None = None,
    keys: SettlementAuthorityKeys | None = None,
    config: PolicySignerConfig = SIGNER_CONFIG,
    log_name: str = "policy-signer.sqlite3",
) -> PolicySigner:
    return PolicySigner(
        config=config,
        authority_keys=keys or authority_keys(),
        settlement_source=source or FakeSettlementSource(),
        signing_log=PolicySigningLog(directory / log_name),
        public_key=POLICY.public_key,
        private_key=POLICY.private_key,
    )


def with_policy(**changes: Any) -> PolicySignerConfig:
    return replace(SIGNER_CONFIG, policy=replace(RULES, **changes))
