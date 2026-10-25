"""Credence policy signer.

Tech: Credence policy key

Responsibility (XRPL integration > Accounts and keys; Security and key management > Rules, build plan):
Signs only after the policy engine passes. Second signature on the borrower's spends.

The policy service signs only an XRPL transaction whose full unsigned form (destination, amount, fee, sequence, network ID, last ledger sequence) hashes to the digest in the engine's decision record, checked at signing time.

X1a implementation (offline; authorized by Maura). The frozen decision record's
``authorized_transaction_digest`` is still null, so the digest is carried by a signed
``PaymentAuthorization`` statement (``credence.signers.payment_authorization``) instead. Before it
produces its one XRPL multisignature, the policy signer independently checks, in order:

1. the statement's Ed25519 signature, under a current settlement-authority key, and its expiry;
2. the network ID;
3. Core's own accounting settlement for the receipt, read through a read-only source: it exists,
   is ``accounting_settled``, and matches the statement's facility, reservation, provider, verifier
   decision, amount and settlement time; and the facility's receipt-chain head is the bound head;
4. the source account, the tRLUSD wire currency and issuer, and that the destination is the
   configured payout account of that provider;
5. the unsigned transaction: it hashes to the authorized digest, and rebuilding the canonical
   Payment from the statement and the transaction's fee, sequence and LastLedgerSequence gives
   the same digest, so no field outside the statement (Memos, Paths, SendMax, flags) can ride along;
6. validated ledger state: the policy signer is in the SignerList, the SignerList, RegularKey and
   master-key state are as configured, the multisign fee is covered, and Sequence or
   TicketSequence and LastLedgerSequence are usable.

It then durably records, before signing, that this receipt and this operation are authorized for
exactly this unsigned transaction. A second, different transaction for the same receipt or
operation is refused, even after a restart. Re-signing the identical transaction is idempotent.
This includes a replacement after the first transaction is proven expired and not included: X1a
has no replacement procedure, so such a receipt stays unpaid until one is reviewed and approved.

The private key is held in memory for the life of the object and is never logged, stored or
returned. Tests use deterministic TEST keys derived at runtime.

Owner: Credence Core (kept separate from the engine to isolate keys).
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xrpl.core.keypairs import derive_classic_address

from credence.core.primitives import Timestamp
from credence.signers.payment_authorization import (
    AccountingSettlementSource,
    PaymentAuthorization,
    PaymentAuthorizationRefused,
    SettlementAuthorityKeys,
    SignedPaymentAuthorization,
    verify_payment_authorization,
)
from credence.xrpl_adapter.multisign import (
    UNSIGNED_DIGEST_SCHEME,
    LedgerContext,
    MultisignPolicy,
    MultisignRejected,
    MultisignSignature,
    build_unsigned_payment,
    sign_as_signer,
    unsigned_transaction_digest,
    verify_ledger_context,
    verify_submission_window,
)
from credence.xrpl_adapter.types import Asset

ACCOUNTING_SETTLED = "accounting_settled"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS policy_signatures (
    receipt_digest TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL UNIQUE,
    authorization_id TEXT NOT NULL UNIQUE,
    unsigned_transaction_digest TEXT NOT NULL,
    signed_at TEXT NOT NULL
)
"""


class PolicySignerRefused(RuntimeError):
    """The policy signer refuses to sign. ``code`` is stable."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class PolicySigningLog:
    """Durable signer-side record: at most one transaction per receipt and per operation."""

    def __init__(self, database: str | Path) -> None:
        self._database = Path(database)
        self._database.parent.mkdir(parents=True, exist_ok=True)
        with self._write() as connection:
            connection.execute(_SCHEMA)
        os.chmod(self._database, 0o600)

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._database, timeout=10, isolation_level=None)
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def record(self, authorization: PaymentAuthorization, now: Timestamp) -> None:
        """Record the authorization before signing; idempotent for the identical transaction."""
        authorization_id = authorization.authorization_id()
        with self._write() as connection:
            rows = connection.execute(
                "SELECT receipt_digest, operation_id, authorization_id, "
                "unsigned_transaction_digest FROM policy_signatures "
                "WHERE receipt_digest = ? OR operation_id = ? OR authorization_id = ?",
                (authorization.receipt_digest, authorization.operation_id, authorization_id),
            ).fetchall()
            expected = (
                authorization.receipt_digest,
                authorization.operation_id,
                authorization_id,
                authorization.unsigned_transaction_digest,
            )
            for row in rows:
                if tuple(row) != expected:
                    raise PolicySignerRefused(
                        "ALREADY_AUTHORIZED",
                        "this receipt or operation is already authorized for another transaction",
                    )
            if not rows:
                connection.execute(
                    "INSERT INTO policy_signatures (receipt_digest, operation_id, "
                    "authorization_id, unsigned_transaction_digest, signed_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (*expected, now.value),
                )


@dataclass(frozen=True)
class PolicySignerConfig:
    policy: MultisignPolicy
    asset: Asset
    provider_destinations: Mapping[str, str]


class PolicySigner:
    """The Credence policy signer for provider Payments from the agent payment account."""

    def __init__(
        self,
        *,
        config: PolicySignerConfig,
        authority_keys: SettlementAuthorityKeys,
        settlement_source: AccountingSettlementSource,
        signing_log: PolicySigningLog,
        public_key: str,
        private_key: str,
    ) -> None:
        if derive_classic_address(public_key) != config.policy.policy_signer_account:
            raise ValueError("policy signer key is not the configured signer account's master key")
        self._config = config
        self._authority_keys = authority_keys
        self._settlement_source = settlement_source
        self._signing_log = signing_log
        self._public_key = public_key
        self.__private_key = private_key

    def __repr__(self) -> str:
        return f"PolicySigner(account={self._config.policy.policy_signer_account!r})"

    @property
    def account(self) -> str:
        return self._config.policy.policy_signer_account

    def sign_payment(
        self,
        signed_authorization: SignedPaymentAuthorization,
        unsigned_transaction: Mapping[str, Any],
        ledger: LedgerContext,
        *,
        now: Timestamp,
    ) -> MultisignSignature:
        """Return this signer's multisignature, or raise ``PolicySignerRefused``."""
        try:
            authorization = verify_payment_authorization(
                signed_authorization, self._authority_keys, now=now
            )
        except PaymentAuthorizationRefused as exc:
            raise PolicySignerRefused(exc.code, exc.detail) from exc
        policy = self._config.policy
        if authorization.network_id != policy.network_id:
            raise PolicySignerRefused("NETWORK_MISMATCH")
        self._check_core_settlement(authorization)
        self._check_parties(authorization)
        self._check_transaction(authorization, unsigned_transaction)
        self._check_ledger(unsigned_transaction, ledger)
        self._signing_log.record(authorization, now)
        try:
            return sign_as_signer(
                unsigned_transaction,
                account=policy.policy_signer_account,
                public_key=self._public_key,
                private_key=self.__private_key,
            )
        except MultisignRejected as exc:
            raise PolicySignerRefused(exc.code, exc.detail) from exc

    def _check_core_settlement(self, authorization: PaymentAuthorization) -> None:
        fact = self._settlement_source.accounting_settlement(authorization.receipt_digest)
        if fact is None:
            raise PolicySignerRefused("SETTLEMENT_NOT_FOUND")
        if fact.state != ACCOUNTING_SETTLED:
            raise PolicySignerRefused("SETTLEMENT_NOT_ACCOUNTING_SETTLED", fact.state)
        bound = (
            ("receipt_digest", fact.receipt_digest, authorization.receipt_digest),
            ("facility_id", fact.facility_id, authorization.facility_id),
            ("reservation_id", fact.reservation_id, authorization.reservation_id),
            ("provider_id", fact.provider_id, authorization.provider_id),
            (
                "verifier_decision_digest",
                fact.verifier_decision_digest,
                authorization.verifier_decision_digest,
            ),
            ("amount", fact.actual_amount_micro_trlusd, authorization.amount_micro_trlusd),
            ("settled_at", fact.settled_at, authorization.accounting_settled_at),
        )
        for name, core_value, stated_value in bound:
            if core_value != stated_value:
                raise PolicySignerRefused("SETTLEMENT_MISMATCH", name)
        if self._settlement_source.chain_head(fact.facility_id) != authorization.receipt_chain_head:
            raise PolicySignerRefused("CHAIN_HEAD_MISMATCH")

    def _check_parties(self, authorization: PaymentAuthorization) -> None:
        config = self._config
        if authorization.source_account != config.policy.account:
            raise PolicySignerRefused("SOURCE_ACCOUNT_MISMATCH")
        if authorization.currency_code != config.asset.currency_code:
            raise PolicySignerRefused("CURRENCY_MISMATCH")
        if authorization.asset_issuer != config.asset.issuer:
            raise PolicySignerRefused("ISSUER_MISMATCH")
        payout = config.provider_destinations.get(authorization.provider_id)
        if payout is None or payout != authorization.destination:
            raise PolicySignerRefused("DESTINATION_NOT_PROVIDER_PAYOUT")

    def _check_transaction(
        self, authorization: PaymentAuthorization, transaction: Mapping[str, Any]
    ) -> None:
        if authorization.unsigned_transaction_digest_scheme != UNSIGNED_DIGEST_SCHEME:
            raise PolicySignerRefused("UNSUPPORTED_DIGEST_SCHEME")
        try:
            digest = unsigned_transaction_digest(transaction)
            ticket = transaction.get("TicketSequence")
            rebuilt = build_unsigned_payment(
                account=authorization.source_account,
                destination=authorization.destination,
                asset=self._config.asset,
                amount_micro_units=authorization.amount_micro_trlusd,
                fee_drops=int(transaction["Fee"]),
                network_id=authorization.network_id,
                last_ledger_sequence=int(transaction["LastLedgerSequence"]),
                sequence=None if ticket is not None else int(transaction["Sequence"]),
                ticket_sequence=None if ticket is None else int(ticket),
            )
            rebuilt_digest = unsigned_transaction_digest(rebuilt)
        except MultisignRejected as exc:
            raise PolicySignerRefused(exc.code, exc.detail) from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise PolicySignerRefused("MALFORMED_TRANSACTION", str(exc)) from exc
        if digest != authorization.unsigned_transaction_digest:
            raise PolicySignerRefused("TRANSACTION_DIGEST_MISMATCH")
        if rebuilt_digest != digest:
            raise PolicySignerRefused(
                "TRANSACTION_FIELDS_MISMATCH", "transaction differs from the authorized payment"
            )

    def _check_ledger(self, transaction: Mapping[str, Any], ledger: LedgerContext) -> None:
        policy = self._config.policy
        signer_list = ledger.account.signer_list
        if signer_list is None or policy.policy_signer_account not in {
            entry.account for entry in signer_list.entries
        }:
            raise PolicySignerRefused(
                "POLICY_SIGNER_NOT_AUTHORIZED", "the policy signer is not in the SignerList"
            )
        ticket = transaction.get("TicketSequence")
        try:
            verify_ledger_context(policy, ledger)
            verify_submission_window(
                policy,
                ledger,
                fee_drops=int(transaction["Fee"]),
                sequence=None if ticket is not None else int(transaction["Sequence"]),
                ticket_sequence=None if ticket is None else int(ticket),
                last_ledger_sequence=int(transaction["LastLedgerSequence"]),
            )
        except MultisignRejected as exc:
            raise PolicySignerRefused(exc.code, exc.detail) from exc
