"""Reliable offline submission, verification, and restart reconciliation."""

from __future__ import annotations

import json

from credence.xrpl_adapter.journal import PaymentJournal
from credence.xrpl_adapter.preparation import assert_signed_payment_matches
from credence.xrpl_adapter.types import (
    TERMINAL_STATES,
    HashLookup,
    JournalConflict,
    LedgerPayment,
    LookupDisposition,
    LookupResult,
    LookupUnavailable,
    PaymentRecord,
    PaymentRequest,
    PaymentStatusReporter,
    ResultVerifier,
    SignedTransactionInspector,
    SubmissionAttempt,
    SubmissionDisposition,
    SubmissionTimeout,
    SubmissionTransport,
    SubmissionTransportError,
    TransactionPreparer,
    TransactionState,
    TransactionType,
    ValidatedLedgerObservation,
    ValidatedLedgerObserver,
    VerificationResult,
)


class PaymentResultVerifier:
    """Verify final ledger evidence against the authorized signed Payment."""

    def verify(self, expected: PaymentRecord, observed: LedgerPayment) -> VerificationResult:
        if not observed.validated:
            return VerificationResult(False, "NOT_VALIDATED", "ledger result is provisional")
        if observed.transaction_hash != expected.transaction_hash:
            return VerificationResult(False, "HASH_MISMATCH", "lookup returned a different hash")
        if observed.transaction_type is not TransactionType.PAYMENT:
            return VerificationResult(
                False, "TYPE_MISMATCH", "validated transaction is not a Payment"
            )
        if observed.result != "tesSUCCESS":
            return VerificationResult(
                False,
                observed.result or "RESULT_MISSING",
                "validated transaction did not execute successfully",
            )
        checks = (
            (observed.account == expected.account, "ACCOUNT_MISMATCH"),
            (observed.destination == expected.destination, "DESTINATION_MISMATCH"),
            (observed.asset == expected.asset, "ASSET_MISMATCH"),
            (observed.amount_micro_units == expected.amount_micro_units, "AMOUNT_MISMATCH"),
            (
                observed.delivered_amount_micro_units == expected.amount_micro_units,
                "DELIVERED_AMOUNT_MISMATCH",
            ),
            (observed.fee_drops == expected.fee_drops, "FEE_MISMATCH"),
            (observed.network_id == expected.network_id, "NETWORK_MISMATCH"),
            (
                observed.last_ledger_sequence == expected.last_ledger_sequence,
                "LAST_LEDGER_SEQUENCE_MISMATCH",
            ),
            (observed.sequence == expected.sequence, "SEQUENCE_MISMATCH"),
            (observed.ticket_sequence == expected.ticket_sequence, "TICKET_MISMATCH"),
            (observed.ledger_index is not None, "LEDGER_INDEX_MISSING"),
            (
                observed.ledger_index is not None
                and expected.earliest_ledger_sequence
                <= observed.ledger_index
                <= expected.last_ledger_sequence,
                "LEDGER_INDEX_OUT_OF_RANGE",
            ),
            (bool(observed.ledger_hash), "LEDGER_HASH_MISSING"),
            (bool(observed.evidence), "FINAL_EVIDENCE_MISSING"),
        )
        for passed, code in checks:
            if not passed:
                return VerificationResult(False, code, "validated Payment details do not match")
        return VerificationResult(True, "tesSUCCESS", "validated Payment matches authorized intent")


class ReadOnlyPaymentStatus(PaymentStatusReporter):
    """Read-only reporting surface; it exposes no journal mutation methods."""

    def __init__(self, journal: PaymentJournal) -> None:
        self.__journal = journal

    def status(self, operation_id: str) -> PaymentRecord:
        return self.__journal.get(operation_id)

    def attempts(self, operation_id: str) -> tuple[SubmissionAttempt, ...]:
        return self.__journal.attempts(operation_id)


class PaymentService:
    """Coordinate reliable Payment submission without changing Core accounting."""

    def __init__(
        self,
        *,
        journal: PaymentJournal,
        preparer: TransactionPreparer,
        inspector: SignedTransactionInspector,
        transport: SubmissionTransport,
        lookup: HashLookup,
        ledger_observer: ValidatedLedgerObserver,
        verifier: ResultVerifier | None = None,
    ) -> None:
        self._journal = journal
        self._preparer = preparer
        self._inspector = inspector
        self._transport = transport
        self._lookup = lookup
        self._ledger_observer = ledger_observer
        self._verifier = verifier or PaymentResultVerifier()
        self.status = ReadOnlyPaymentStatus(journal)

    def prepare_and_persist(
        self, request: PaymentRequest, signed_transaction: bytes
    ) -> PaymentRecord:
        prepared = self._preparer.prepare_payment(request)
        parsed = self._inspector.inspect_payment(signed_transaction)
        assert_signed_payment_matches(prepared, parsed)
        return self._journal.persist_signed(prepared, signed_transaction)

    def submit(self, operation_id: str) -> PaymentRecord:
        """Submit a PREPARED payment once; reconciliation controls any resubmission."""
        record = self._journal.get(operation_id)
        return self._submit(record, resubmission=False)

    def _submit(self, record: PaymentRecord, *, resubmission: bool) -> PaymentRecord:
        attempt = self._journal.begin_submission(record.operation_id, resubmission=resubmission)
        # The database transaction is closed before this network boundary.
        try:
            response = self._transport.submit(record.signed_transaction, record.transaction_hash)
        except SubmissionTimeout:
            self._journal.finish_submission(
                record.operation_id,
                attempt,
                disposition=SubmissionDisposition.TIMEOUT,
                next_state=TransactionState.UNKNOWN,
                provisional_status="SUBMISSION_TIMEOUT_OUTCOME_UNKNOWN",
            )
            return self._journal.get(record.operation_id)
        except SubmissionTransportError:
            self._journal.finish_submission(
                record.operation_id,
                attempt,
                disposition=SubmissionDisposition.TRANSPORT_ERROR,
                next_state=TransactionState.UNKNOWN,
                provisional_status="SUBMISSION_TRANSPORT_ERROR_OUTCOME_UNKNOWN",
            )
            return self._journal.get(record.operation_id)

        if not response.engine_result:
            self._journal.finish_submission(
                record.operation_id,
                attempt,
                disposition=SubmissionDisposition.TRANSPORT_ERROR,
                next_state=TransactionState.UNKNOWN,
                provisional_status="UNEXPECTED_SUBMISSION_RESPONSE",
            )
            return self._journal.get(record.operation_id)

        self._journal.finish_submission(
            record.operation_id,
            attempt,
            disposition=(
                SubmissionDisposition.ACKNOWLEDGED
                if response.accepted
                else SubmissionDisposition.REJECTED
            ),
            next_state=(
                TransactionState.PENDING_CONFIRMATION
                if response.accepted
                else TransactionState.UNKNOWN
            ),
            provisional_status=response.engine_result,
        )
        return self._journal.get(record.operation_id)

    def reconcile(self, operation_id: str, *, resubmit_if_pending: bool = False) -> PaymentRecord:
        record = self._journal.get(operation_id)
        if record.state in TERMINAL_STATES or record.state is TransactionState.PREPARED:
            return record
        try:
            lookup = self._lookup.lookup(record.transaction_hash)
        except LookupUnavailable:
            return self._journal.record_reconciliation_state(
                operation_id, TransactionState.UNKNOWN, "TRANSACTION_LOOKUP_UNAVAILABLE"
            )

        if lookup.disposition is LookupDisposition.FOUND:
            return self._reconcile_found(record, lookup)
        if lookup.disposition is LookupDisposition.UNAVAILABLE:
            return self._journal.record_reconciliation_state(
                operation_id,
                TransactionState.UNKNOWN,
                lookup.detail or "TRANSACTION_LOOKUP_UNAVAILABLE",
            )
        if lookup.disposition is not LookupDisposition.NOT_FOUND:
            return self._journal.record_reconciliation_state(
                operation_id, TransactionState.UNKNOWN, "UNEXPECTED_LOOKUP_RESULT"
            )

        try:
            observation = self._ledger_observer.validated_ledger()
        except LookupUnavailable:
            return self._journal.record_reconciliation_state(
                operation_id, TransactionState.UNKNOWN, "VALIDATED_LEDGER_UNAVAILABLE"
            )
        return self._reconcile_not_found(
            record,
            observation,
            resubmit_if_pending=resubmit_if_pending,
        )

    def _reconcile_found(self, record: PaymentRecord, lookup: LookupResult) -> PaymentRecord:
        observed = lookup.payment
        if observed is None:
            return self._journal.record_reconciliation_state(
                record.operation_id,
                TransactionState.UNKNOWN,
                "LOOKUP_FOUND_WITHOUT_TRANSACTION",
            )
        if not observed.validated:
            return self._journal.record_reconciliation_state(
                record.operation_id,
                TransactionState.PENDING_CONFIRMATION,
                observed.result or "TRANSACTION_FOUND_NOT_VALIDATED",
            )
        verification = self._verifier.verify(record, observed)
        state = (
            TransactionState.VALIDATED_SUCCESS
            if verification.success
            else TransactionState.VALIDATED_FAILURE
        )
        return self._journal.finalize(
            record.operation_id,
            state=state,
            result=verification.result_code,
            detail=verification.detail,
            evidence=observed.evidence,
            validated_ledger_index=observed.ledger_index,
        )

    def _reconcile_not_found(
        self,
        record: PaymentRecord,
        observation: ValidatedLedgerObservation,
        *,
        resubmit_if_pending: bool,
    ) -> PaymentRecord:
        if observation.proves_non_inclusion(
            earliest=record.earliest_ledger_sequence,
            last=record.last_ledger_sequence,
        ):
            evidence = json.dumps(
                {
                    "complete_history_max": observation.complete_history_max,
                    "complete_history_min": observation.complete_history_min,
                    "history_complete": observation.history_complete,
                    "last_ledger_sequence": record.last_ledger_sequence,
                    "validated_ledger_index": observation.ledger_index,
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            return self._journal.finalize(
                record.operation_id,
                state=TransactionState.EXPIRED_NOT_INCLUDED,
                result="txnNotFound",
                detail="complete validated history proves non-inclusion after expiry",
                evidence=evidence,
                validated_ledger_index=observation.ledger_index,
            )

        if observation.ledger_index > record.last_ledger_sequence:
            return self._journal.record_reconciliation_state(
                record.operation_id,
                TransactionState.UNKNOWN,
                "HISTORY_INCOMPLETE_NON_INCLUSION_UNPROVEN",
            )

        pending = self._journal.record_reconciliation_state(
            record.operation_id,
            TransactionState.PENDING_CONFIRMATION,
            "TRANSACTION_NOT_FOUND_BEFORE_EXPIRY",
        )
        if not resubmit_if_pending:
            return pending
        # Resubmission reuses the exact persisted bytes and therefore the same hash.
        return self._submit(pending, resubmission=True)

    def reconcile_after_restart(self) -> tuple[PaymentRecord, ...]:
        return tuple(
            self.reconcile(record.operation_id) for record in self._journal.list_nonterminal()
        )

    def claim_xrpl_settlement(self, operation_id: str) -> bool:
        """Return true exactly once, and only after verified validated success."""
        return self._journal.claim_settlement_effect(operation_id)


def require_state(record: PaymentRecord, expected: TransactionState) -> None:
    """Small integration assertion used by callers at the accounting seam."""
    if record.state is not expected:
        raise JournalConflict(
            f"{record.operation_id}: expected {expected.value}, got {record.state.value}"
        )
