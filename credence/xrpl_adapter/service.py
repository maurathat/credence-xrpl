"""Reliable offline submission, verification, and restart reconciliation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from credence.xrpl_adapter.journal import PaymentJournal
from credence.xrpl_adapter.preparation import (
    MICRO_UNITS_PER_TRLUSD,
    XrplSignedPaymentInspector,
    assert_signed_payment_matches,
    transaction_hash,
)
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
    SettlementEvent,
    SettlementEventConsumer,
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
    VerificationDisposition,
    VerificationResult,
)

_EVIDENCE_SCHEMA = "credence.xrpl.validated-payment-evidence.v1"


@dataclass(frozen=True)
class _VerifiedLedgerEvidence:
    transaction_hash: str
    signed_transaction: bytes
    result: str
    delivered_amount_micro_units: int | None
    delivered_currency: str | None
    delivered_issuer: str | None
    ledger_index: int
    ledger_hash: str


class PaymentResultVerifier:
    """Cross-check trusted-source evidence; it is not independent consensus proof."""

    def verify(self, expected: PaymentRecord, observed: LedgerPayment) -> VerificationResult:
        if not observed.validated:
            return VerificationResult(
                VerificationDisposition.PROVISIONAL,
                "NOT_VALIDATED",
                "ledger result is provisional",
            )
        try:
            evidence = _parse_validated_evidence(observed.evidence)
            parsed = XrplSignedPaymentInspector().inspect_payment(evidence.signed_transaction)
        except (TypeError, ValueError) as exc:
            return VerificationResult(
                VerificationDisposition.UNRESOLVED,
                "FINAL_EVIDENCE_INVALID",
                str(exc),
            )

        normalized_checks = (
            (evidence.signed_transaction == expected.signed_transaction, "SIGNED_BYTES_MISMATCH"),
            (evidence.transaction_hash == expected.transaction_hash, "HASH_MISMATCH"),
            (observed.transaction_hash == evidence.transaction_hash, "NORMALIZED_HASH_MISMATCH"),
            (observed.transaction_type is TransactionType.PAYMENT, "TYPE_MISMATCH"),
            (observed.result == evidence.result, "NORMALIZED_RESULT_MISMATCH"),
            (observed.ledger_index == evidence.ledger_index, "NORMALIZED_LEDGER_INDEX_MISMATCH"),
            (observed.ledger_hash == evidence.ledger_hash, "NORMALIZED_LEDGER_HASH_MISMATCH"),
            (
                observed.delivered_amount_micro_units == evidence.delivered_amount_micro_units,
                "NORMALIZED_DELIVERED_AMOUNT_MISMATCH",
            ),
            (observed.account == parsed.account, "NORMALIZED_ACCOUNT_MISMATCH"),
            (observed.destination == parsed.destination, "NORMALIZED_DESTINATION_MISMATCH"),
            (observed.asset == parsed.asset, "NORMALIZED_ASSET_MISMATCH"),
            (
                observed.amount_micro_units == parsed.amount_micro_units,
                "NORMALIZED_AMOUNT_MISMATCH",
            ),
            (observed.fee_drops == parsed.fee_drops, "NORMALIZED_FEE_MISMATCH"),
            (observed.network_id == parsed.network_id, "NORMALIZED_NETWORK_MISMATCH"),
            (
                observed.last_ledger_sequence == parsed.last_ledger_sequence,
                "NORMALIZED_LAST_LEDGER_SEQUENCE_MISMATCH",
            ),
            (observed.sequence == parsed.sequence, "NORMALIZED_SEQUENCE_MISMATCH"),
            (observed.ticket_sequence == parsed.ticket_sequence, "NORMALIZED_TICKET_MISMATCH"),
        )
        for passed, code in normalized_checks:
            if not passed:
                return VerificationResult(
                    VerificationDisposition.UNRESOLVED,
                    code,
                    "structured evidence contradicts normalized ledger fields",
                )

        expected_checks = (
            (parsed.account == expected.account, "ACCOUNT_MISMATCH"),
            (parsed.destination == expected.destination, "DESTINATION_MISMATCH"),
            (parsed.asset == expected.asset, "ASSET_MISMATCH"),
            (parsed.amount_micro_units == expected.amount_micro_units, "AMOUNT_MISMATCH"),
            (parsed.fee_drops == expected.fee_drops, "FEE_MISMATCH"),
            (parsed.network_id == expected.network_id, "NETWORK_MISMATCH"),
            (
                parsed.last_ledger_sequence == expected.last_ledger_sequence,
                "LAST_LEDGER_SEQUENCE_MISMATCH",
            ),
            (parsed.sequence == expected.sequence, "SEQUENCE_MISMATCH"),
            (parsed.ticket_sequence == expected.ticket_sequence, "TICKET_MISMATCH"),
            (
                expected.submission_anchor_ledger_index is not None
                and expected.submission_anchor_ledger_index
                < evidence.ledger_index
                <= expected.last_ledger_sequence,
                "LEDGER_INDEX_OUT_OF_RANGE",
            ),
        )
        for passed, code in expected_checks:
            if not passed:
                return VerificationResult(
                    VerificationDisposition.UNRESOLVED,
                    code,
                    "verified transaction contradicts the persisted payment intent",
                )

        if evidence.result != "tesSUCCESS":
            return VerificationResult(
                VerificationDisposition.VALIDATED_FAILURE,
                evidence.result,
                "trusted evidence establishes a validated transaction failure",
                evidence.ledger_index,
                evidence.ledger_hash,
                evidence.delivered_amount_micro_units,
            )
        if (
            evidence.delivered_currency != expected.asset.currency_code
            or evidence.delivered_issuer != expected.asset.issuer
        ):
            return VerificationResult(
                VerificationDisposition.VALIDATED_FAILURE,
                "DELIVERED_ASSET_MISMATCH",
                "validated Payment delivered an unexpected asset",
                evidence.ledger_index,
                evidence.ledger_hash,
                evidence.delivered_amount_micro_units,
            )
        if evidence.delivered_amount_micro_units != expected.amount_micro_units:
            return VerificationResult(
                VerificationDisposition.VALIDATED_FAILURE,
                "DELIVERED_AMOUNT_MISMATCH",
                "validated Payment delivered an unexpected amount",
                evidence.ledger_index,
                evidence.ledger_hash,
                evidence.delivered_amount_micro_units,
            )
        return VerificationResult(
            VerificationDisposition.VALIDATED_SUCCESS,
            "tesSUCCESS",
            "trusted evidence establishes the authorized validated Payment",
            evidence.ledger_index,
            evidence.ledger_hash,
            evidence.delivered_amount_micro_units,
        )


def _parse_validated_evidence(raw: bytes) -> _VerifiedLedgerEvidence:
    if not raw:
        raise ValueError("validated ledger evidence is missing")
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("validated ledger evidence is not valid JSON") from exc
    if not isinstance(document, dict) or document.get("schema") != _EVIDENCE_SCHEMA:
        raise ValueError("validated ledger evidence has an unsupported schema")
    if document.get("validated") is not True:
        raise ValueError("validated ledger evidence does not establish validation")

    ledger_index = document.get("ledger_index")
    if isinstance(ledger_index, bool) or not isinstance(ledger_index, int) or ledger_index < 1:
        raise ValueError("validated ledger evidence has an invalid ledger index")
    ledger_hash = _hash_text(document.get("ledger_hash"), "ledger hash")
    evidence_hash = _hash_text(document.get("transaction_hash"), "transaction hash")

    signed_hex = document.get("signed_transaction")
    if not isinstance(signed_hex, str) or not signed_hex or len(signed_hex) % 2:
        raise ValueError("validated ledger evidence has invalid signed transaction bytes")
    try:
        signed_transaction = bytes.fromhex(signed_hex)
    except ValueError as exc:
        raise ValueError("validated ledger evidence has invalid signed transaction bytes") from exc
    if transaction_hash(signed_transaction) != evidence_hash:
        raise ValueError("validated ledger evidence hash does not match its signed bytes")

    meta = document.get("meta")
    if not isinstance(meta, dict):
        raise ValueError("validated ledger evidence has no transaction metadata")
    result = meta.get("TransactionResult")
    if not isinstance(result, str) or not result:
        raise ValueError("validated ledger evidence has no final transaction result")
    delivered = meta.get("delivered_amount")
    delivered_amount: int | None = None
    delivered_currency: str | None = None
    delivered_issuer: str | None = None
    if delivered is not None:
        if not isinstance(delivered, dict):
            raise ValueError("validated ledger evidence has malformed delivered amount")
        delivered_currency = _required_text(delivered, "currency")
        delivered_issuer = _required_text(delivered, "issuer")
        delivered_amount = _evidence_micro_units(delivered.get("value"))
    if result == "tesSUCCESS" and delivered_amount is None:
        raise ValueError("successful Payment evidence has no delivered amount")
    return _VerifiedLedgerEvidence(
        transaction_hash=evidence_hash,
        signed_transaction=signed_transaction,
        result=result,
        delivered_amount_micro_units=delivered_amount,
        delivered_currency=delivered_currency,
        delivered_issuer=delivered_issuer,
        ledger_index=ledger_index,
        ledger_hash=ledger_hash,
    )


def _hash_text(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(f"validated ledger evidence has an invalid {label}")
    normalized = value.upper()
    if any(character not in "0123456789ABCDEF" for character in normalized):
        raise ValueError(f"validated ledger evidence has an invalid {label}")
    return normalized


def _required_text(document: dict[object, object], name: str) -> str:
    value = document.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"validated ledger evidence has no delivered {name}")
    return value


def _evidence_micro_units(value: object) -> int:
    if not isinstance(value, str):
        raise ValueError("validated ledger evidence amount is not a decimal string")
    try:
        scaled = Decimal(value) * MICRO_UNITS_PER_TRLUSD
    except InvalidOperation as exc:
        raise ValueError("validated ledger evidence amount is not a decimal") from exc
    if not scaled.is_finite() or scaled != scaled.to_integral_value() or scaled <= 0:
        raise ValueError("validated ledger evidence amount is not positive integer micro-units")
    return int(scaled)


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
        if getattr(transport, "offline_only", False) is not True:
            raise ValueError("X0s permits deterministic offline submission transports only")
        if verifier is not None and type(verifier) is not PaymentResultVerifier:
            raise ValueError("X0s finality verification cannot be replaced")
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
        if record.submission_anchor_ledger_index is None:
            observation = self._ledger_observer.validated_ledger(record.account)
            record = self._journal.record_submission_anchor(operation_id, observation)
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
            observation = self._ledger_observer.validated_ledger(record.account)
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
        verification = self._verifier.verify(record, observed)
        if verification.disposition is VerificationDisposition.PROVISIONAL:
            return self._journal.record_reconciliation_state(
                record.operation_id,
                TransactionState.PENDING_CONFIRMATION,
                verification.result_code,
            )
        if verification.disposition is VerificationDisposition.UNRESOLVED:
            return self._journal.record_reconciliation_state(
                record.operation_id,
                TransactionState.UNKNOWN,
                verification.result_code,
            )
        state = (
            TransactionState.VALIDATED_SUCCESS
            if verification.disposition is VerificationDisposition.VALIDATED_SUCCESS
            else TransactionState.VALIDATED_FAILURE
        )
        return self._journal.finalize(
            record.operation_id,
            state=state,
            result=verification.result_code,
            detail=verification.detail,
            evidence=observed.evidence,
            validated_ledger_index=verification.ledger_index,
            validated_ledger_hash=verification.ledger_hash,
            verified_delivered_amount_micro_units=verification.delivered_amount_micro_units,
            evidence_verification_version=1,
        )

    def _reconcile_not_found(
        self,
        record: PaymentRecord,
        observation: ValidatedLedgerObservation,
        *,
        resubmit_if_pending: bool,
    ) -> PaymentRecord:
        anchor = record.submission_anchor_ledger_index
        history_start = None if anchor is None else anchor + 1
        trusted_anchor = bool(
            anchor is not None
            and record.submission_anchor_ledger_hash
            and record.submission_anchor_source_id
        )
        source_matches = observation.source_id == record.submission_anchor_source_id
        sender_state_matches = observation.account_state_matches(
            account=record.account,
            sequence=record.sequence,
            ticket_sequence=record.ticket_sequence,
        )
        if (
            trusted_anchor
            and source_matches
            and sender_state_matches
            and history_start is not None
            and observation.proves_non_inclusion(
                earliest=history_start,
                last=record.last_ledger_sequence,
            )
        ):
            evidence = json.dumps(
                {
                    "anchor_ledger_hash": record.submission_anchor_ledger_hash,
                    "anchor_ledger_index": anchor,
                    "complete_history_max": observation.complete_history_max,
                    "complete_history_min": observation.complete_history_min,
                    "complete_history_ranges": [
                        [ledger_range.first, ledger_range.last]
                        for ledger_range in observation.complete_history_ranges
                    ],
                    "history_complete": observation.history_complete,
                    "last_ledger_sequence": record.last_ledger_sequence,
                    "source_id": observation.source_id,
                    "validated_ledger_hash": observation.ledger_hash,
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
                validated_ledger_hash=observation.ledger_hash,
                verified_delivered_amount_micro_units=None,
                evidence_verification_version=1,
            )

        if observation.ledger_index > record.last_ledger_sequence:
            if not trusted_anchor:
                detail = "TRUSTED_SUBMISSION_ANCHOR_MISSING"
            elif not source_matches:
                detail = "LEDGER_SOURCE_DISAGREES_WITH_SUBMISSION_ANCHOR"
            elif not sender_state_matches:
                detail = "SENDER_SEQUENCE_OR_TICKET_CONSUMPTION_UNRESOLVED"
            else:
                detail = "HISTORY_INCOMPLETE_NON_INCLUSION_UNPROVEN"
            return self._journal.record_reconciliation_state(
                record.operation_id,
                TransactionState.UNKNOWN,
                detail,
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

    def deliver_settlement_events(
        self,
        consumer: SettlementEventConsumer,
        *,
        limit: int = 100,
    ) -> tuple[SettlementEvent, ...]:
        """Retry undelivered events; consumers must deduplicate by deterministic event ID."""
        delivered: list[SettlementEvent] = []
        for event in self._journal.pending_settlement_events(limit=limit):
            attempt = self._journal.begin_settlement_event_delivery(event.event_id)
            if attempt.delivered_at is not None:
                continue
            try:
                consumer.apply(attempt)
            except Exception:
                continue
            if self._journal.mark_settlement_event_delivered(attempt.event_id):
                delivered.append(self._journal.get_settlement_event(attempt.event_id))
        return tuple(delivered)


def require_state(record: PaymentRecord, expected: TransactionState) -> None:
    """Small integration assertion used by callers at the accounting seam."""
    if record.state is not expected:
        raise JournalConflict(
            f"{record.operation_id}: expected {expected.value}, got {record.state.value}"
        )
