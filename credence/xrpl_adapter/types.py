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


@dataclass(frozen=True)
class Asset:
    """An issued asset with both its product label and XRPL wire identity."""

    label: str
    currency_code: str
    issuer: str


@dataclass(frozen=True)
class PaymentRequest:
    """Authorized intent used to prepare one provider Payment.

    ``payment_authorization_id`` is intentionally distinct from any ordinary
    procurement authorization. ``earliest_ledger_sequence`` bounds the history
    that must be complete before non-inclusion can be proven.
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
class ValidatedLedgerObservation:
    """Validated-ledger progress plus the node's complete history range."""

    ledger_index: int
    complete_history_min: int | None
    complete_history_max: int | None
    history_complete: bool

    def proves_non_inclusion(self, *, earliest: int, last: int) -> bool:
        return bool(
            self.ledger_index > last
            and self.history_complete
            and self.complete_history_min is not None
            and self.complete_history_max is not None
            and 0 < self.complete_history_min <= self.complete_history_max
            and self.complete_history_max <= self.ledger_index
            and self.complete_history_min <= earliest
            and self.complete_history_max >= last
        )


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
    success: bool
    result_code: str
    detail: str


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
    settlement_status: str | None
    settlement_effect_claimed: bool
    created_at: str
    updated_at: str


class TransactionPreparer(Protocol):
    def prepare_payment(self, request: PaymentRequest) -> PreparedPayment: ...


class SignedTransactionInspector(Protocol):
    def inspect_payment(self, signed_transaction: bytes) -> ParsedSignedPayment: ...


class SubmissionTransport(Protocol):
    def submit(self, signed_transaction: bytes, transaction_hash: str) -> SubmissionResponse: ...


class HashLookup(Protocol):
    def lookup(self, transaction_hash: str) -> LookupResult: ...


class ValidatedLedgerObserver(Protocol):
    def validated_ledger(self) -> ValidatedLedgerObservation: ...


class ResultVerifier(Protocol):
    def verify(self, expected: PaymentRecord, observed: LedgerPayment) -> VerificationResult: ...


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
