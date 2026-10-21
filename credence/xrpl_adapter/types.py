"""Typed records for offline XRPL payment readiness.

These records deliberately keep inference delivery, Core accounting settlement,
submission, and validated ledger outcome as separate facts.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class TransactionType(Enum):
    """XRPL transaction types visible at the X0 integration seam."""

    PAYMENT = "Payment"
    LOAN_SET = "LoanSet"
    LOAN_PAY = "LoanPay"


class TransactionState(Enum):
    """Durable reliable-submission lifecycle."""

    PREPARED = "PREPARED"
    SUBMITTED = "SUBMITTED"
    PENDING_CONFIRMATION = "PENDING_CONFIRMATION"
    VALIDATED_SUCCESS = "VALIDATED_SUCCESS"
    VALIDATED_FAILURE = "VALIDATED_FAILURE"
    EXPIRED_NOT_INCLUDED = "EXPIRED_NOT_INCLUDED"
    UNKNOWN = "UNKNOWN"


TERMINAL_STATES = frozenset(
    {
        TransactionState.VALIDATED_SUCCESS,
        TransactionState.VALIDATED_FAILURE,
        TransactionState.EXPIRED_NOT_INCLUDED,
    }
)


class LookupDisposition(Enum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    UNAVAILABLE = "UNAVAILABLE"


class SubmissionDisposition(Enum):
    ACKNOWLEDGED = "ACKNOWLEDGED"
    REJECTED = "REJECTED"
    TIMEOUT = "TIMEOUT"
    TRANSPORT_ERROR = "TRANSPORT_ERROR"


class VerificationDisposition(Enum):
    """Classification produced from structured trusted-ledger evidence."""

    PROVISIONAL = "PROVISIONAL"
    UNRESOLVED = "UNRESOLVED"
    VALIDATED_SUCCESS = "VALIDATED_SUCCESS"
    VALIDATED_FAILURE = "VALIDATED_FAILURE"


@dataclass(frozen=True)
class Asset:
    """An issued asset with both its product label and XRPL wire identity."""

    label: str
    currency_code: str
    issuer: str


@dataclass(frozen=True)
class PaymentRequest:
    """Authorized intent used to prepare one provider Payment.

    ``payment_authorization_id`` is an identifier, not proof that Core authorized
    a payment. X1 must bind it to an immutable Core settlement and approved
    unsigned-transaction digest before live submission is enabled.

    ``earliest_ledger_sequence`` is a caller hint retained for compatibility and
    audit only. It cannot prove non-inclusion. X0s derives the authoritative
    history anchor from a trusted validated-ledger observation before broadcast.
    """

    operation_id: str
    payment_authorization_id: str
    account: str
    destination: str
    asset: Asset
    amount_micro_units: int
    fee_drops: int
    network_id: int
    last_ledger_sequence: int
    earliest_ledger_sequence: int
    sequence: int | None = None
    ticket_sequence: int | None = None


@dataclass(frozen=True)
class PreparedPayment:
    request: PaymentRequest


@dataclass(frozen=True)
class ParsedSignedPayment:
    """Security-relevant fields decoded from persisted signed bytes."""

    account: str
    destination: str
    asset: Asset
    amount_micro_units: int
    fee_drops: int
    network_id: int
    last_ledger_sequence: int
    sequence: int | None
    ticket_sequence: int | None


@dataclass(frozen=True)
class SubmissionResponse:
    """A submission acknowledgement only; never proof of settlement."""

    accepted: bool
    engine_result: str
    message: str


@dataclass(frozen=True)
class LedgerRange:
    """One continuous inclusive range of validated ledgers held by a source."""

    first: int
    last: int


@dataclass(frozen=True)
class ValidatedLedgerObservation:
    """Trusted-source ledger progress, identity, history, and sender state.

    This observation is a statement by a configured trusted ledger source. It
    is not independently verified cryptographic consensus proof.
    """

    ledger_index: int
    complete_history_min: int | None
    complete_history_max: int | None
    history_complete: bool
    ledger_hash: str | None = None
    source_id: str | None = None
    complete_history_ranges: tuple[LedgerRange, ...] = ()
    account: str | None = None
    account_sequence: int | None = None
    available_tickets: tuple[int, ...] | None = None

    def has_trusted_identity(self) -> bool:
        return bool(
            self.ledger_index > 0
            and isinstance(self.ledger_hash, str)
            and len(self.ledger_hash) == 64
            and all(character in "0123456789ABCDEFabcdef" for character in self.ledger_hash)
            and isinstance(self.source_id, str)
            and self.source_id.strip()
        )

    def account_state_matches(
        self,
        *,
        account: str,
        sequence: int | None,
        ticket_sequence: int | None,
    ) -> bool:
        if self.account != account:
            return False
        if sequence is not None:
            return self.account_sequence == sequence
        return bool(
            ticket_sequence is not None
            and self.available_tickets is not None
            and ticket_sequence in self.available_tickets
        )

    def proves_non_inclusion(self, *, earliest: int, last: int) -> bool:
        if not (
            self.has_trusted_identity()
            and self.ledger_index > last
            and self.history_complete
            and 0 < earliest <= last
            and self.complete_history_ranges
        ):
            return False
        ranges = sorted(self.complete_history_ranges, key=lambda item: (item.first, item.last))
        covered_through = earliest - 1
        for ledger_range in ranges:
            if not (0 < ledger_range.first <= ledger_range.last <= self.ledger_index):
                return False
            if ledger_range.last < earliest or ledger_range.first > last:
                continue
            if ledger_range.first > covered_through + 1:
                return False
            covered_through = max(covered_through, ledger_range.last)
            if covered_through >= last:
                return True
        return False


@dataclass(frozen=True)
class LedgerPayment:
    """Normalized hash lookup result, including final metadata evidence."""

    transaction_hash: str
    validated: bool
    result: str | None
    transaction_type: TransactionType
    account: str
    destination: str
    asset: Asset
    amount_micro_units: int
    delivered_amount_micro_units: int | None
    fee_drops: int
    network_id: int
    last_ledger_sequence: int
    sequence: int | None
    ticket_sequence: int | None
    ledger_index: int | None
    ledger_hash: str | None
    evidence: bytes


@dataclass(frozen=True)
class LookupResult:
    disposition: LookupDisposition
    payment: LedgerPayment | None = None
    detail: str | None = None


@dataclass(frozen=True)
class VerificationResult:
    disposition: VerificationDisposition
    result_code: str
    detail: str
    ledger_index: int | None = None
    ledger_hash: str | None = None
    delivered_amount_micro_units: int | None = None

    @property
    def success(self) -> bool:
        return self.disposition is VerificationDisposition.VALIDATED_SUCCESS


@dataclass(frozen=True)
class SubmissionAttempt:
    attempt_number: int
    started_at: str
    completed_at: str | None
    disposition: SubmissionDisposition | None
    provisional_status: str | None


@dataclass(frozen=True)
class PaymentRecord:
    operation_id: str
    payment_authorization_id: str
    transaction_type: TransactionType
    account: str
    destination: str
    asset: Asset
    amount_micro_units: int
    fee_drops: int
    network_id: int
    sequence: int | None
    ticket_sequence: int | None
    earliest_ledger_sequence: int
    last_ledger_sequence: int
    signed_transaction: bytes
    transaction_hash: str
    state: TransactionState
    last_provisional_status: str | None
    final_result: str | None
    final_detail: str | None
    final_evidence: bytes | None
    validated_ledger_index: int | None
    validated_ledger_hash: str | None
    verified_delivered_amount_micro_units: int | None
    evidence_verification_version: int
    submission_anchor_ledger_index: int | None
    submission_anchor_ledger_hash: str | None
    submission_anchor_source_id: str | None
    settlement_status: str | None
    settlement_effect_claimed: bool
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class SettlementEvent:
    """Durable append-only notification of one verified XRPL settlement."""

    event_id: str
    operation_id: str
    payment_authorization_id: str
    transaction_hash: str
    payload: bytes
    delivery_attempts: int
    created_at: str
    delivered_at: str | None


class TransactionPreparer(Protocol):
    def prepare_payment(self, request: PaymentRequest) -> PreparedPayment: ...


class SignedTransactionInspector(Protocol):
    def inspect_payment(self, signed_transaction: bytes) -> ParsedSignedPayment: ...


class SubmissionTransport(Protocol):
    offline_only: bool

    def submit(self, signed_transaction: bytes, transaction_hash: str) -> SubmissionResponse: ...


class HashLookup(Protocol):
    def lookup(self, transaction_hash: str) -> LookupResult: ...


class ValidatedLedgerObserver(Protocol):
    def validated_ledger(self, account: str) -> ValidatedLedgerObservation: ...


class ResultVerifier(Protocol):
    def verify(self, expected: PaymentRecord, observed: LedgerPayment) -> VerificationResult: ...


class SettlementEventConsumer(Protocol):
    """Idempotent consumer; return only after the destination durably applies the event."""

    def apply(self, event: SettlementEvent) -> None: ...


class Reconciler(Protocol):
    def reconcile(
        self, operation_id: str, *, resubmit_if_pending: bool = False
    ) -> PaymentRecord: ...


class PaymentStatusReporter(Protocol):
    def status(self, operation_id: str) -> PaymentRecord: ...

    def attempts(self, operation_id: str) -> tuple[SubmissionAttempt, ...]: ...


class SubmissionTimeout(TimeoutError):
    """The submit response was lost; ledger outcome is unknown."""


class SubmissionTransportError(ConnectionError):
    """Submission transport failed without final ledger evidence."""


class LookupUnavailable(ConnectionError):
    """Hash lookup or validated-ledger evidence is unavailable."""


class PreparationError(ValueError):
    pass


class SignedTransactionError(ValueError):
    pass


class TransactionHashMismatch(RuntimeError):
    pass


class JournalConflict(RuntimeError):
    pass


class DuplicateFinancialOperation(RuntimeError):
    pass
