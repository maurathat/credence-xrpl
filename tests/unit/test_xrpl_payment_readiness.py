"""X0 offline adversarial suite for reliable XRPL Payment submission."""

from __future__ import annotations

import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from xrpl.core.addresscodec import encode_classic_address
from xrpl.core.binarycodec import encode
from xrpl.models.amounts import IssuedCurrencyAmount
from xrpl.models.transactions import Payment, Transaction

from credence.xrpl_adapter.journal import PaymentJournal
from credence.xrpl_adapter.mock import DeterministicMockTransport
from credence.xrpl_adapter.preparation import (
    TRLUSD_WIRE_CODE,
    OfflinePaymentPreparer,
    XrplSignedPaymentInspector,
    transaction_hash,
)
from credence.xrpl_adapter.service import PaymentService
from credence.xrpl_adapter.types import (
    Asset,
    DuplicateFinancialOperation,
    LedgerPayment,
    LookupDisposition,
    LookupResult,
    PaymentRecord,
    PaymentRequest,
    PreparationError,
    SignedTransactionError,
    SubmissionResponse,
    SubmissionTimeout,
    TransactionHashMismatch,
    TransactionState,
    TransactionType,
    ValidatedLedgerObservation,
)

ACCOUNT = encode_classic_address(bytes(range(20)))
DESTINATION = encode_classic_address(bytes(range(1, 21)))
WRONG_DESTINATION = encode_classic_address(bytes(range(3, 23)))
ISSUER = encode_classic_address(bytes(range(2, 22)))
OTHER_ISSUER = encode_classic_address(bytes(range(4, 24)))
ASSET = Asset(label="tRLUSD", currency_code=TRLUSD_WIRE_CODE, issuer=ISSUER)


def payment_request(operation_id: str = "operation-1", *, sequence: int = 10) -> PaymentRequest:
    return PaymentRequest(
        operation_id=operation_id,
        payment_authorization_id=f"payment-authorization-{operation_id}",
        account=ACCOUNT,
        destination=DESTINATION,
        asset=ASSET,
        amount_micro_units=1_500_000,
        fee_drops=12,
        network_id=21_337,
        earliest_ledger_sequence=20,
        last_ledger_sequence=25,
        sequence=sequence,
    )


def xrpl_value(micro_units: int) -> str:
    whole, remainder = divmod(micro_units, 1_000_000)
    return f"{whole}.{remainder:06d}".rstrip("0").rstrip(".")


def signed_blob(request: PaymentRequest, **changes: object) -> bytes:
    fields: dict[str, object] = {
        "account": request.account,
        "destination": request.destination,
        "amount": IssuedCurrencyAmount(
            currency=request.asset.currency_code,
            issuer=request.asset.issuer,
            value=xrpl_value(request.amount_micro_units),
        ),
        "fee": str(request.fee_drops),
        "last_ledger_sequence": request.last_ledger_sequence,
        "network_id": request.network_id,
        "signing_pub_key": "02" + "11" * 32,
        "txn_signature": "AA",
    }
    if request.ticket_sequence is None:
        fields["sequence"] = request.sequence
    else:
        fields["sequence"] = 0
        fields["ticket_sequence"] = request.ticket_sequence
    fields.update(changes)
    transaction = Payment(**fields)  # type: ignore[arg-type]
    return bytes.fromhex(encode(transaction.to_xrpl()))


def ledger_before_expiry() -> ValidatedLedgerObservation:
    return ValidatedLedgerObservation(
        ledger_index=24,
        complete_history_min=1,
        complete_history_max=24,
        history_complete=True,
    )


def service_for(
    database: Path, transport: DeterministicMockTransport
) -> tuple[PaymentService, PaymentJournal]:
    journal = PaymentJournal(database)
    return (
        PaymentService(
            journal=journal,
            preparer=OfflinePaymentPreparer(maximum_fee_drops=20),
            inspector=XrplSignedPaymentInspector(),
            transport=transport,
            lookup=transport,
            ledger_observer=transport,
        ),
        journal,
    )


def prepared(
    database: Path,
    transport: DeterministicMockTransport,
    operation_id: str = "operation-1",
    *,
    sequence: int = 10,
) -> tuple[PaymentService, PaymentJournal, PaymentRecord]:
    service, journal = service_for(database, transport)
    request = payment_request(operation_id, sequence=sequence)
    record = service.prepare_and_persist(request, signed_blob(request))
    return service, journal, record


def final_payment(record: PaymentRecord, **changes: object) -> LedgerPayment:
    fields: dict[str, object] = {
        "transaction_hash": record.transaction_hash,
        "validated": True,
        "result": "tesSUCCESS",
        "transaction_type": TransactionType.PAYMENT,
        "account": record.account,
        "destination": record.destination,
        "asset": record.asset,
        "amount_micro_units": record.amount_micro_units,
        "delivered_amount_micro_units": record.amount_micro_units,
        "fee_drops": record.fee_drops,
        "network_id": record.network_id,
        "last_ledger_sequence": record.last_ledger_sequence,
        "sequence": record.sequence,
        "ticket_sequence": record.ticket_sequence,
        "ledger_index": 23,
        "ledger_hash": "A" * 64,
        "evidence": b'{"validated":true}',
    }
    fields.update(changes)
    return LedgerPayment(**fields)  # type: ignore[arg-type]


def acknowledge() -> SubmissionResponse:
    return SubmissionResponse(True, "tesSUCCESS", "provisional only")


def found(payment: LedgerPayment) -> LookupResult:
    return LookupResult(LookupDisposition.FOUND, payment=payment)


def test_success_requires_validated_payment_and_effect_is_exactly_once(tmp_path: Path) -> None:
    transport = DeterministicMockTransport(ledger=ledger_before_expiry())
    transport.queue_submission(acknowledge())
    service, _, record = prepared(tmp_path / "payments.db", transport)

    submitted = service.submit(record.operation_id)
    assert submitted.state is TransactionState.PENDING_CONFIRMATION
    assert submitted.last_provisional_status == "tesSUCCESS"
    assert not submitted.settlement_effect_claimed

    transport.set_lookup(record.transaction_hash, found(final_payment(record)))
    settled = service.reconcile(record.operation_id)
    assert settled.state is TransactionState.VALIDATED_SUCCESS
    assert settled.settlement_status == "XRPL_SETTLED"
    assert service.claim_xrpl_settlement(record.operation_id)
    assert not service.claim_xrpl_settlement(record.operation_id)


def test_submission_timeout_then_validated_success(tmp_path: Path) -> None:
    transport = DeterministicMockTransport(ledger=ledger_before_expiry())
    transport.queue_submission(SubmissionTimeout("response lost"))
    service, _, record = prepared(tmp_path / "payments.db", transport)

    assert service.submit(record.operation_id).state is TransactionState.UNKNOWN
    transport.set_lookup(record.transaction_hash, found(final_payment(record)))
    assert service.reconcile(record.operation_id).state is TransactionState.VALIDATED_SUCCESS
    assert len(transport.submitted) == 1


def test_crash_after_broadcast_recovers_by_original_hash(tmp_path: Path) -> None:
    database = tmp_path / "payments.db"
    transport = DeterministicMockTransport(ledger=ledger_before_expiry())
    transport.queue_submission(RuntimeError("simulated process crash"))
    service, journal, record = prepared(database, transport)

    with pytest.raises(RuntimeError, match="simulated process crash"):
        service.submit(record.operation_id)
    assert journal.get(record.operation_id).state is TransactionState.SUBMITTED

    restarted_transport = DeterministicMockTransport(ledger=ledger_before_expiry())
    restarted_transport.set_lookup(record.transaction_hash, found(final_payment(record)))
    restarted, _ = service_for(database, restarted_transport)
    assert restarted.reconcile(record.operation_id).state is TransactionState.VALIDATED_SUCCESS
    assert restarted_transport.submitted == []


def test_safe_resubmission_reuses_identical_signed_transaction(tmp_path: Path) -> None:
    transport = DeterministicMockTransport(ledger=ledger_before_expiry())
    transport.queue_submission(SubmissionTimeout("response lost"))
    transport.queue_submission(acknowledge())
    service, journal, record = prepared(tmp_path / "payments.db", transport)

    service.submit(record.operation_id)
    transport.set_lookup(record.transaction_hash, LookupResult(LookupDisposition.NOT_FOUND))
    pending = service.reconcile(record.operation_id, resubmit_if_pending=True)

    assert pending.state is TransactionState.PENDING_CONFIRMATION
    assert transport.submitted == [
        (record.signed_transaction, record.transaction_hash),
        (record.signed_transaction, record.transaction_hash),
    ]
    assert len(journal.attempts(record.operation_id)) == 2


def test_validated_transaction_failure_is_final_but_not_settled(tmp_path: Path) -> None:
    transport = DeterministicMockTransport(ledger=ledger_before_expiry())
    transport.queue_submission(acknowledge())
    service, _, record = prepared(tmp_path / "payments.db", transport)
    service.submit(record.operation_id)
    failure = final_payment(
        record,
        result="tecUNFUNDED_PAYMENT",
        delivered_amount_micro_units=None,
    )
    transport.set_lookup(record.transaction_hash, found(failure))

    result = service.reconcile(record.operation_id)
    assert result.state is TransactionState.VALIDATED_FAILURE
    assert result.settlement_status is None
    assert result.final_result == "tecUNFUNDED_PAYMENT"
    assert not service.claim_xrpl_settlement(record.operation_id)


def test_expiry_requires_complete_validated_history(tmp_path: Path) -> None:
    transport = DeterministicMockTransport(ledger=ValidatedLedgerObservation(30, 1, 30, True))
    transport.queue_submission(SubmissionTimeout("response lost"))
    service, _, record = prepared(tmp_path / "payments.db", transport)
    service.submit(record.operation_id)
    transport.set_lookup(record.transaction_hash, LookupResult(LookupDisposition.NOT_FOUND))

    result = service.reconcile(record.operation_id)
    assert result.state is TransactionState.EXPIRED_NOT_INCLUDED
    assert result.final_evidence is not None


def test_missing_ledger_history_cannot_prove_expiry(tmp_path: Path) -> None:
    transport = DeterministicMockTransport(ledger=ValidatedLedgerObservation(30, None, None, False))
    transport.queue_submission(SubmissionTimeout("response lost"))
    service, _, record = prepared(tmp_path / "payments.db", transport)
    service.submit(record.operation_id)
    transport.set_lookup(record.transaction_hash, LookupResult(LookupDisposition.NOT_FOUND))

    result = service.reconcile(record.operation_id)
    assert result.state is TransactionState.UNKNOWN
    assert result.last_provisional_status == "HISTORY_INCOMPLETE_NON_INCLUSION_UNPROVEN"


@pytest.mark.parametrize(
    "changes, reason",
    [
        ({"destination": WRONG_DESTINATION}, "DESTINATION_MISMATCH"),
        (
            {"asset": Asset("tRLUSD", TRLUSD_WIRE_CODE, OTHER_ISSUER)},
            "ASSET_MISMATCH",
        ),
        ({"delivered_amount_micro_units": 1_499_999}, "DELIVERED_AMOUNT_MISMATCH"),
    ],
)
def test_wrong_destination_asset_or_delivered_amount_fails_closed(
    tmp_path: Path, changes: dict[str, object], reason: str
) -> None:
    transport = DeterministicMockTransport(ledger=ledger_before_expiry())
    transport.queue_submission(acknowledge())
    service, _, record = prepared(tmp_path / f"{reason}.db", transport)
    service.submit(record.operation_id)
    transport.set_lookup(record.transaction_hash, found(final_payment(record, **changes)))

    result = service.reconcile(record.operation_id)
    assert result.state is TransactionState.VALIDATED_FAILURE
    assert result.final_result == reason
    assert not service.claim_xrpl_settlement(record.operation_id)


def test_repeated_reconciliation_after_restart_is_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "payments.db"
    transport = DeterministicMockTransport(ledger=ledger_before_expiry())
    transport.queue_submission(acknowledge())
    service, _, record = prepared(database, transport)
    service.submit(record.operation_id)
    payment = final_payment(record)
    transport.set_lookup(record.transaction_hash, found(payment))
    assert service.reconcile(record.operation_id).state is TransactionState.VALIDATED_SUCCESS
    assert service.claim_xrpl_settlement(record.operation_id)

    restarted_transport = DeterministicMockTransport(ledger=ledger_before_expiry())
    restarted_transport.set_lookup(record.transaction_hash, found(payment))
    restarted, _ = service_for(database, restarted_transport)
    assert restarted.reconcile(record.operation_id).state is TransactionState.VALIDATED_SUCCESS
    assert not restarted.claim_xrpl_settlement(record.operation_id)


def test_competing_reconciliation_workers_commit_one_outcome(tmp_path: Path) -> None:
    database = tmp_path / "payments.db"
    transport = DeterministicMockTransport(ledger=ledger_before_expiry())
    transport.queue_submission(acknowledge())
    service, _, record = prepared(database, transport)
    service.submit(record.operation_id)
    transport.set_lookup(record.transaction_hash, found(final_payment(record)))
    worker_a, _ = service_for(database, transport)
    worker_b, _ = service_for(database, transport)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(
            pool.map(lambda worker: worker.reconcile(record.operation_id), (worker_a, worker_b))
        )
    assert {result.state for result in results} == {TransactionState.VALIDATED_SUCCESS}
    assert (
        sum(service.claim_xrpl_settlement(record.operation_id) for service in (worker_a, worker_b))
        == 1
    )


def test_recorded_hash_inconsistent_with_signed_bytes_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "payments.db"
    transport = DeterministicMockTransport(ledger=ledger_before_expiry())
    _, journal, record = prepared(database, transport)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE xrpl_payment_transactions SET transaction_hash = ? WHERE operation_id = ?",
            ("F" * 64, record.operation_id),
        )
    with pytest.raises(TransactionHashMismatch):
        journal.get(record.operation_id)


def test_failed_submission_keeps_outcome_unknown(tmp_path: Path) -> None:
    transport = DeterministicMockTransport(ledger=ledger_before_expiry())
    transport.queue_submission(SubmissionResponse(False, "temMALFORMED", "initial server rejected"))
    service, _, record = prepared(tmp_path / "payments.db", transport)

    result = service.submit(record.operation_id)
    assert result.state is TransactionState.UNKNOWN
    assert result.final_result is None
    assert not service.claim_xrpl_settlement(record.operation_id)


def test_signed_blob_is_derived_and_matched_before_persistence(tmp_path: Path) -> None:
    request = payment_request()
    transport = DeterministicMockTransport(ledger=ledger_before_expiry())
    service, _ = service_for(tmp_path / "payments.db", transport)
    blob = signed_blob(request, destination=WRONG_DESTINATION)

    with pytest.raises(SignedTransactionError, match="differ"):
        service.prepare_and_persist(request, blob)
    assert transaction_hash(blob) != "0" * 64
    assert transaction_hash(blob) == Transaction.from_blob(blob.hex().upper()).get_hash()


def test_signed_payment_rejects_unauthorized_extra_fields(tmp_path: Path) -> None:
    request = payment_request()
    transport = DeterministicMockTransport(ledger=ledger_before_expiry())
    service, _ = service_for(tmp_path / "payments.db", transport)

    with pytest.raises(SignedTransactionError, match="unauthorized fields"):
        service.prepare_and_persist(request, signed_blob(request, destination_tag=7))


def test_uniqueness_prevents_duplicate_financial_operation(tmp_path: Path) -> None:
    database = tmp_path / "payments.db"
    transport = DeterministicMockTransport(ledger=ledger_before_expiry())
    service, _, _ = prepared(database, transport)
    duplicate = payment_request("operation-2", sequence=10)

    with pytest.raises(DuplicateFinancialOperation):
        service.prepare_and_persist(duplicate, signed_blob(duplicate))


@pytest.mark.parametrize(
    "changes",
    [
        {"payment_authorization_id": ""},
        {"amount_micro_units": 0},
        {"fee_drops": 21},
        {"destination": ACCOUNT},
        {"asset": Asset("other", TRLUSD_WIRE_CODE, ISSUER)},
    ],
)
def test_preparation_rejects_unauthorized_or_unsafe_payment_fields(
    changes: dict[str, object],
) -> None:
    request = replace(payment_request(), **changes)
    with pytest.raises(PreparationError):
        OfflinePaymentPreparer(maximum_fee_drops=20).prepare_payment(request)


def test_journal_file_is_owner_only_and_status_surface_is_read_only(tmp_path: Path) -> None:
    database = tmp_path / "payments.db"
    transport = DeterministicMockTransport(ledger=ledger_before_expiry())
    service, _, record = prepared(database, transport)
    assert os.stat(database).st_mode & 0o777 == 0o600
    assert service.status.status(record.operation_id).state is TransactionState.PREPARED
    assert not hasattr(service.status, "claim_settlement_effect")


def test_no_sqlite_write_transaction_spans_submission(tmp_path: Path) -> None:
    database = tmp_path / "payments.db"

    class LockProbeTransport(DeterministicMockTransport):
        def submit(self, signed_transaction: bytes, tx_hash: str) -> SubmissionResponse:
            with sqlite3.connect(database, timeout=0.1) as connection:
                connection.execute("CREATE TABLE transport_probe (value INTEGER)")
                connection.execute("INSERT INTO transport_probe VALUES (1)")
            self.submitted.append((signed_transaction, tx_hash))
            return acknowledge()

    transport = LockProbeTransport(ledger=ledger_before_expiry())
    service, _, record = prepared(database, transport)
    assert service.submit(record.operation_id).state is TransactionState.PENDING_CONFIRMATION
