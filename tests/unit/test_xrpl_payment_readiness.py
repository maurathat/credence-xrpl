"""X0 offline adversarial suite for reliable XRPL Payment submission."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from xrpl.core.addresscodec import encode_classic_address
from xrpl.core.binarycodec import encode
from xrpl.models.amounts import IssuedCurrencyAmount
from xrpl.models.transactions import Payment, Signer, Transaction

from credence.xrpl_adapter.journal import PaymentJournal
from credence.xrpl_adapter.mock import (
    DeterministicMockTransport,
    DeterministicSettlementConsumer,
)
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
    LedgerRange,
    LegacyJournalUnsupported,
    LegacySettlementClaimRetired,
    LookupDisposition,
    LookupResult,
    LookupUnavailable,
    NonInclusionProof,
    PaymentRecord,
    PaymentRequest,
    PreparationError,
    PreparedPayment,
    SignedTransactionError,
    SubmissionDisposition,
    SubmissionResponse,
    SubmissionTimeout,
    SupersededJournalSchemaV3,
    TransactionHashMismatch,
    TransactionState,
    TransactionType,
    UnsupportedJournalSchema,
    ValidatedLedgerObservation,
)

ACCOUNT = encode_classic_address(bytes(range(20)))
DESTINATION = encode_classic_address(bytes(range(1, 21)))
WRONG_DESTINATION = encode_classic_address(bytes(range(3, 23)))
ISSUER = encode_classic_address(bytes(range(2, 22)))
OTHER_ISSUER = encode_classic_address(bytes(range(4, 24)))
ASSET = Asset(label="tRLUSD", currency_code=TRLUSD_WIRE_CODE, issuer=ISSUER)
LEGACY_FIXTURES = Path(__file__).parents[1] / "fixtures" / "xrpl"


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
        # Deliberately noncryptographic X0s fixture: structural inspection only.
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


def trusted_ledger(
    ledger_index: int = 19,
    *,
    history_ranges: tuple[LedgerRange, ...] = (LedgerRange(1, 19),),
    source_id: str = "offline-mock",
    account_sequence: int = 10,
    available_tickets: tuple[int, ...] = (77,),
) -> ValidatedLedgerObservation:
    return ValidatedLedgerObservation(
        ledger_index=ledger_index,
        complete_history_min=1,
        complete_history_max=ledger_index,
        history_complete=True,
        ledger_hash=(f"{ledger_index:064X}"),
        source_id=source_id,
        complete_history_ranges=history_ranges,
        account=ACCOUNT,
        account_sequence=account_sequence,
        available_tickets=available_tickets,
        network_id=21_337,
        build_version="deterministic-mock",
        node_public_key="n9DeterministicMockNode",
    )


def ledger_before_expiry() -> ValidatedLedgerObservation:
    return trusted_ledger()


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
        "ledger_hash": f"{23:064X}",
        "evidence": b"",
    }
    fields.update(changes)
    if "evidence" not in changes:
        delivered = fields["delivered_amount_micro_units"]
        delivered_asset = fields["asset"]
        assert isinstance(delivered_asset, Asset)
        evidence = {
            "schema": "credence.xrpl.validated-payment-evidence.v1",
            "validated": True,
            "ledger_index": fields["ledger_index"],
            "ledger_hash": fields["ledger_hash"],
            "transaction_hash": record.transaction_hash,
            "signed_transaction": record.signed_transaction.hex().upper(),
            "meta": {
                "TransactionResult": fields["result"],
                "delivered_amount": (
                    None
                    if delivered is None
                    else {
                        "currency": delivered_asset.currency_code,
                        "issuer": delivered_asset.issuer,
                        "value": xrpl_value(int(delivered)),
                    }
                ),
            },
        }
        fields["evidence"] = json.dumps(evidence, separators=(",", ":"), sort_keys=True).encode()
    return LedgerPayment(**fields)  # type: ignore[arg-type]


def acknowledge() -> SubmissionResponse:
    return SubmissionResponse(True, "tesSUCCESS", "provisional only")


def journal_events(database: Path) -> list[tuple[str, str]]:
    with sqlite3.connect(database) as connection:
        return [
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                "SELECT event_id, operation_id FROM xrpl_settlement_outbox"
            )
        ]


def found(payment: LedgerPayment) -> LookupResult:
    return LookupResult(LookupDisposition.FOUND, payment=payment)


def not_found_with_proof(
    record: PaymentRecord, observation: ValidatedLedgerObservation
) -> LookupResult:
    first = 20
    identity = {
        "network_id": observation.network_id,
        "build_version": observation.build_version,
        "node_public_key": observation.node_public_key,
        "ledger_index": observation.ledger_index,
        "ledger_hash": observation.ledger_hash,
    }
    evidence = json.dumps(
        {
            "schema": "credence.xrpl.non-inclusion-evidence.v1",
            "transaction_hash": record.transaction_hash,
            "searched_all": True,
            "min_ledger": first,
            "max_ledger": record.last_ledger_sequence,
            "source_id": observation.source_id,
            "validated_identity": identity,
            "validated_identity_after": identity,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return LookupResult(
        LookupDisposition.NOT_FOUND,
        non_inclusion_proof=NonInclusionProof(
            transaction_hash=record.transaction_hash,
            first_ledger_sequence=first,
            last_ledger_sequence=record.last_ledger_sequence,
            searched_all=True,
            source_id=observation.source_id or "",
            network_id=observation.network_id or 0,
            build_version=observation.build_version or "",
            node_public_key=observation.node_public_key or "",
            validated_ledger_index=observation.ledger_index,
            validated_ledger_hash=observation.ledger_hash or "",
            complete_history_ranges=observation.complete_history_ranges,
            evidence=evidence,
        ),
    )


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
    # X1c B5: the effect is the single outbox event; the legacy claim is retired and refused.
    assert len(journal_events(tmp_path / "payments.db")) == 1
    for _ in range(2):
        with pytest.raises(LegacySettlementClaimRetired):
            service.claim_xrpl_settlement(record.operation_id)
    assert not service.status.status(record.operation_id).settlement_effect_claimed


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
    database = tmp_path / "payments.db"
    transport = DeterministicMockTransport(ledger=ledger_before_expiry())
    transport.queue_submission(SubmissionTimeout("response lost"))
    service, _, record = prepared(database, transport)
    service.submit(record.operation_id)
    observation = trusted_ledger(30, history_ranges=(LedgerRange(1, 30),), account_sequence=10)
    transport.set_lookup(record.transaction_hash, not_found_with_proof(record, observation))
    transport.set_ledger(observation)

    restarted, _ = service_for(database, transport)
    result = restarted.reconcile(record.operation_id)
    assert result.state is TransactionState.EXPIRED_NOT_INCLUDED
    assert result.final_evidence is not None


def test_missing_ledger_history_cannot_prove_expiry(tmp_path: Path) -> None:
    transport = DeterministicMockTransport(ledger=ledger_before_expiry())
    transport.queue_submission(SubmissionTimeout("response lost"))
    service, _, record = prepared(tmp_path / "payments.db", transport)
    service.submit(record.operation_id)
    transport.set_lookup(record.transaction_hash, LookupResult(LookupDisposition.NOT_FOUND))
    transport.set_ledger(
        ValidatedLedgerObservation(
            30,
            None,
            None,
            False,
            f"{30:064X}",
            "offline-mock",
            (),
            ACCOUNT,
            10,
            (77,),
            21_337,
            "deterministic-mock",
            "n9DeterministicMockNode",
        )
    )

    result = service.reconcile(record.operation_id)
    assert result.state is TransactionState.UNKNOWN
    assert result.last_provisional_status == "BOUNDED_NON_INCLUSION_PROOF_MISSING_OR_INCONSISTENT"


@pytest.mark.parametrize(
    "changes, state, reason",
    [
        (
            {"destination": WRONG_DESTINATION},
            TransactionState.UNKNOWN,
            "NORMALIZED_DESTINATION_MISMATCH",
        ),
        (
            {"asset": Asset("tRLUSD", TRLUSD_WIRE_CODE, OTHER_ISSUER)},
            TransactionState.UNKNOWN,
            "NORMALIZED_ASSET_MISMATCH",
        ),
        (
            {"delivered_amount_micro_units": 1_499_999},
            TransactionState.VALIDATED_UNEXPECTED_DELIVERY,  # X1c C1: not a ledger failure
            "DELIVERED_AMOUNT_MISMATCH",
        ),
    ],
)
def test_wrong_destination_asset_or_delivered_amount_fails_closed(
    tmp_path: Path,
    changes: dict[str, object],
    state: TransactionState,
    reason: str,
) -> None:
    transport = DeterministicMockTransport(ledger=ledger_before_expiry())
    transport.queue_submission(acknowledge())
    service, _, record = prepared(tmp_path / f"{reason}.db", transport)
    service.submit(record.operation_id)
    transport.set_lookup(record.transaction_hash, found(final_payment(record, **changes)))

    result = service.reconcile(record.operation_id)
    assert result.state is state
    if state is TransactionState.UNKNOWN:
        assert result.last_provisional_status == reason
    else:
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
    with pytest.raises(LegacySettlementClaimRetired):
        service.claim_xrpl_settlement(record.operation_id)

    restarted_transport = DeterministicMockTransport(ledger=ledger_before_expiry())
    restarted_transport.set_lookup(record.transaction_hash, found(payment))
    restarted, _ = service_for(database, restarted_transport)
    assert restarted.reconcile(record.operation_id).state is TransactionState.VALIDATED_SUCCESS
    with pytest.raises(LegacySettlementClaimRetired):
        restarted.claim_xrpl_settlement(record.operation_id)
    assert len(journal_events(database)) == 1


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
    assert len(journal_events(database)) == 1
    for worker in (worker_a, worker_b):
        with pytest.raises(LegacySettlementClaimRetired):
            worker.claim_xrpl_settlement(record.operation_id)


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


def test_contradictory_raw_evidence_cannot_create_false_success(tmp_path: Path) -> None:
    transport = DeterministicMockTransport(ledger=ledger_before_expiry())
    transport.queue_submission(acknowledge())
    service, journal, record = prepared(tmp_path / "payments.db", transport)
    service.submit(record.operation_id)
    normalized_success = final_payment(record)
    contradictory = json.loads(normalized_success.evidence)
    contradictory["validated"] = False
    contradictory["meta"] = {
        "TransactionResult": "tecFAILURE",
        "delivered_amount": {
            "currency": record.asset.currency_code,
            "issuer": record.asset.issuer,
            "value": "0",
        },
    }
    transport.set_lookup(
        record.transaction_hash,
        found(
            replace(
                normalized_success,
                evidence=json.dumps(contradictory, separators=(",", ":"), sort_keys=True).encode(),
            )
        ),
    )

    unresolved = service.reconcile(record.operation_id)
    assert unresolved.state is TransactionState.UNKNOWN
    assert unresolved.last_provisional_status == "FINAL_EVIDENCE_INVALID"
    assert unresolved.settlement_status is None
    assert not service.claim_xrpl_settlement(record.operation_id)
    assert journal.pending_settlement_events() == ()


@pytest.mark.parametrize("result", ["tesSUCCESS", "tecUNFUNDED_PAYMENT"])
def test_missing_evidence_can_later_resolve_after_restart(tmp_path: Path, result: str) -> None:
    database = tmp_path / f"{result}.db"
    transport = DeterministicMockTransport(ledger=ledger_before_expiry())
    transport.queue_submission(acknowledge())
    service, _, record = prepared(database, transport)
    service.submit(record.operation_id)
    missing = final_payment(
        record,
        result=result,
        delivered_amount_micro_units=(
            record.amount_micro_units if result == "tesSUCCESS" else None
        ),
        evidence=b"",
    )
    transport.set_lookup(record.transaction_hash, found(missing))
    assert service.reconcile(record.operation_id).state is TransactionState.UNKNOWN

    valid = final_payment(
        record,
        result=result,
        delivered_amount_micro_units=(
            record.amount_micro_units if result == "tesSUCCESS" else None
        ),
    )
    restarted_transport = DeterministicMockTransport(ledger=ledger_before_expiry())
    restarted_transport.set_lookup(record.transaction_hash, found(valid))
    restarted, journal = service_for(database, restarted_transport)
    resolved = restarted.reconcile(record.operation_id)
    expected = (
        TransactionState.VALIDATED_SUCCESS
        if result == "tesSUCCESS"
        else TransactionState.VALIDATED_FAILURE
    )
    assert resolved.state is expected
    assert len(journal.pending_settlement_events()) == (1 if result == "tesSUCCESS" else 0)


def test_contradictory_evidence_can_later_resolve_and_stale_provisional_is_ignored(
    tmp_path: Path,
) -> None:
    transport = DeterministicMockTransport(ledger=ledger_before_expiry())
    transport.queue_submission(acknowledge())
    service, _, record = prepared(tmp_path / "payments.db", transport)
    service.submit(record.operation_id)
    good = final_payment(record)
    document = json.loads(good.evidence)
    document["meta"]["TransactionResult"] = "tecFAILURE"
    transport.set_lookup(
        record.transaction_hash,
        found(replace(good, evidence=json.dumps(document).encode())),
    )
    assert service.reconcile(record.operation_id).state is TransactionState.UNKNOWN

    transport.set_lookup(record.transaction_hash, found(good))
    assert service.reconcile(record.operation_id).state is TransactionState.VALIDATED_SUCCESS
    transport.set_lookup(
        record.transaction_hash,
        found(replace(good, validated=False, result="tesQUEUED", evidence=b"")),
    )
    assert service.reconcile(record.operation_id).state is TransactionState.VALIDATED_SUCCESS


def test_forged_caller_history_start_cannot_prove_expiry(tmp_path: Path) -> None:
    request = replace(payment_request(), earliest_ledger_sequence=24)
    transport = DeterministicMockTransport(ledger=ledger_before_expiry())
    transport.queue_submission(SubmissionTimeout("response lost"))
    service, _ = service_for(tmp_path / "payments.db", transport)
    record = service.prepare_and_persist(request, signed_blob(request))
    service.submit(record.operation_id)
    transport.set_lookup(record.transaction_hash, LookupResult(LookupDisposition.NOT_FOUND))
    transport.set_ledger(
        trusted_ledger(30, history_ranges=(LedgerRange(24, 30),), account_sequence=10)
    )

    result = service.reconcile(record.operation_id)
    assert result.state is TransactionState.UNKNOWN
    assert result.last_provisional_status == "BOUNDED_NON_INCLUSION_PROOF_MISSING_OR_INCONSISTENT"


@pytest.mark.parametrize(
    ("observation", "reason"),
    [
        (
            trusted_ledger(
                30,
                history_ranges=(LedgerRange(20, 22), LedgerRange(22, 24)),
                account_sequence=10,
            ),
            "BOUNDED_NON_INCLUSION_PROOF_MISSING_OR_INCONSISTENT",
        ),
        (
            trusted_ledger(
                30,
                history_ranges=(LedgerRange(20, 30),),
                source_id="disagreeing-server",
                account_sequence=10,
            ),
            "LEDGER_SOURCE_IDENTITY_DISAGREES_WITH_SUBMISSION_ANCHOR",
        ),
        (
            trusted_ledger(
                30,
                history_ranges=(LedgerRange(20, 30),),
                account_sequence=11,
            ),
            "SENDER_SEQUENCE_OR_TICKET_CONSUMPTION_UNRESOLVED",
        ),
    ],
)
def test_expiry_requires_continuous_same_source_history_and_unconsumed_sequence(
    tmp_path: Path,
    observation: ValidatedLedgerObservation,
    reason: str,
) -> None:
    transport = DeterministicMockTransport(ledger=ledger_before_expiry())
    transport.queue_submission(SubmissionTimeout("response lost"))
    service, _, record = prepared(tmp_path / f"{reason}.db", transport)
    service.submit(record.operation_id)
    transport.set_lookup(record.transaction_hash, LookupResult(LookupDisposition.NOT_FOUND))
    transport.set_ledger(observation)
    result = service.reconcile(record.operation_id)
    assert result.state is TransactionState.UNKNOWN
    assert result.last_provisional_status == reason


def test_missing_first_broadcast_anchor_never_manufactures_expiry(tmp_path: Path) -> None:
    database = tmp_path / "payments.db"
    transport = DeterministicMockTransport(ledger=ledger_before_expiry())
    service, journal, record = prepared(database, transport)
    attempt = journal.begin_submission(record.operation_id, resubmission=False)
    assert attempt is not None
    journal.finish_submission(
        record.operation_id,
        attempt,
        disposition=SubmissionDisposition.TIMEOUT,
        next_state=TransactionState.UNKNOWN,
        provisional_status="SIMULATED_LEGACY_SUBMISSION_WITHOUT_ANCHOR",
    )
    transport.set_lookup(record.transaction_hash, LookupResult(LookupDisposition.NOT_FOUND))
    transport.set_ledger(
        trusted_ledger(30, history_ranges=(LedgerRange(1, 30),), account_sequence=10)
    )
    result = service.reconcile(record.operation_id)
    assert result.state is TransactionState.UNKNOWN
    assert result.last_provisional_status == "TRUSTED_SUBMISSION_ANCHOR_IDENTITY_MISSING"


def test_missing_first_broadcast_provenance_prevents_submission(tmp_path: Path) -> None:
    transport = DeterministicMockTransport(ledger=LookupUnavailable("ledger unavailable"))
    transport.queue_submission(acknowledge())
    service, journal, record = prepared(tmp_path / "payments.db", transport)
    with pytest.raises(LookupUnavailable, match="ledger unavailable"):
        service.submit(record.operation_id)
    assert journal.get(record.operation_id).state is TransactionState.PREPARED
    assert transport.submitted == []


def test_consumed_ticket_prevents_expiry_classification(tmp_path: Path) -> None:
    request = replace(payment_request(), sequence=None, ticket_sequence=77)
    transport = DeterministicMockTransport(ledger=ledger_before_expiry())
    transport.queue_submission(SubmissionTimeout("response lost"))
    service, _ = service_for(tmp_path / "payments.db", transport)
    record = service.prepare_and_persist(request, signed_blob(request))
    service.submit(record.operation_id)
    transport.set_lookup(record.transaction_hash, LookupResult(LookupDisposition.NOT_FOUND))
    transport.set_ledger(
        trusted_ledger(
            30,
            history_ranges=(LedgerRange(20, 30),),
            available_tickets=(),
        )
    )
    result = service.reconcile(record.operation_id)
    assert result.state is TransactionState.UNKNOWN
    assert result.last_provisional_status == "SENDER_SEQUENCE_OR_TICKET_CONSUMPTION_UNRESOLVED"


def test_success_creates_outbox_event_in_finalization_transaction(tmp_path: Path) -> None:
    transport = DeterministicMockTransport(ledger=ledger_before_expiry())
    transport.queue_submission(acknowledge())
    service, journal, record = prepared(tmp_path / "payments.db", transport)
    service.submit(record.operation_id)
    transport.set_lookup(record.transaction_hash, found(final_payment(record)))
    settled = service.reconcile(record.operation_id)

    events = journal.pending_settlement_events()
    assert settled.state is TransactionState.VALIDATED_SUCCESS
    assert len(events) == 1
    event = events[0]
    assert event.operation_id == record.operation_id
    assert event.transaction_hash == record.transaction_hash
    assert event.delivery_attempts == 0
    assert event.delivered_at is None
    assert json.loads(event.payload)["result"] == "tesSUCCESS"


def test_outbox_retries_crash_before_consumer_application(tmp_path: Path) -> None:
    database = tmp_path / "payments.db"
    transport = DeterministicMockTransport(ledger=ledger_before_expiry())
    transport.queue_submission(acknowledge())
    service, _, record = prepared(database, transport)
    service.submit(record.operation_id)
    transport.set_lookup(record.transaction_hash, found(final_payment(record)))
    service.reconcile(record.operation_id)
    consumer = DeterministicSettlementConsumer()
    consumer.fail_before_apply()
    assert service.deliver_settlement_events(consumer) == ()

    restarted, journal = service_for(
        database, DeterministicMockTransport(ledger=ledger_before_expiry())
    )
    delivered = restarted.deliver_settlement_events(consumer)
    assert len(delivered) == 1
    assert delivered[0].delivery_attempts == 2
    assert delivered[0].delivered_at is not None
    assert len(consumer.applied) == 1
    assert journal.pending_settlement_events() == ()


def test_outbox_replay_after_consumer_apply_is_idempotent(tmp_path: Path) -> None:
    transport = DeterministicMockTransport(ledger=ledger_before_expiry())
    transport.queue_submission(acknowledge())
    service, journal, record = prepared(tmp_path / "payments.db", transport)
    service.submit(record.operation_id)
    transport.set_lookup(record.transaction_hash, found(final_payment(record)))
    service.reconcile(record.operation_id)
    consumer = DeterministicSettlementConsumer()
    consumer.fail_after_apply()

    assert service.deliver_settlement_events(consumer) == ()
    assert len(consumer.applied) == 1
    assert len(journal.pending_settlement_events()) == 1
    delivered = service.deliver_settlement_events(consumer)
    assert len(delivered) == 1
    assert delivered[0].delivery_attempts == 2
    assert len(consumer.applied) == 1
    assert service.deliver_settlement_events(consumer) == ()


def test_competing_outbox_workers_require_one_idempotent_effect(tmp_path: Path) -> None:
    database = tmp_path / "payments.db"
    transport = DeterministicMockTransport(ledger=ledger_before_expiry())
    transport.queue_submission(acknowledge())
    service, _, record = prepared(database, transport)
    service.submit(record.operation_id)
    transport.set_lookup(record.transaction_hash, found(final_payment(record)))
    service.reconcile(record.operation_id)
    worker_a, _ = service_for(database, transport)
    worker_b, journal = service_for(database, transport)
    consumer = DeterministicSettlementConsumer()
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = tuple(
            pool.map(
                lambda worker: worker.deliver_settlement_events(consumer), (worker_a, worker_b)
            )
        )
    assert sum(len(outcome) for outcome in outcomes) == 1
    assert len(consumer.applied) == 1
    assert journal.pending_settlement_events() == ()


def test_no_outbox_event_before_verified_success(tmp_path: Path) -> None:
    transport = DeterministicMockTransport(ledger=ledger_before_expiry())
    transport.queue_submission(SubmissionTimeout("response lost"))
    service, journal, record = prepared(tmp_path / "payments.db", transport)
    service.submit(record.operation_id)
    assert journal.pending_settlement_events() == ()
    transport.set_lookup(
        record.transaction_hash,
        found(
            final_payment(record, result="tecUNFUNDED_PAYMENT", delivered_amount_micro_units=None)
        ),
    )
    assert service.reconcile(record.operation_id).state is TransactionState.VALIDATED_FAILURE
    assert journal.pending_settlement_events() == ()


def test_structurally_contradictory_signing_forms_are_rejected() -> None:
    request = payment_request()
    signer = Signer(
        account=WRONG_DESTINATION,
        signing_pub_key="02" + "22" * 32,
        txn_signature="AA",
    )
    with pytest.raises(SignedTransactionError, match="cannot be mixed"):
        XrplSignedPaymentInspector().inspect_payment(signed_blob(request, signers=[signer]))
    with pytest.raises(SignedTransactionError, match="empty signer fields"):
        XrplSignedPaymentInspector().inspect_payment(
            signed_blob(
                request,
                signing_pub_key="",
                txn_signature=None,
                signers=[replace(signer, txn_signature="")],
            )
        )


def test_one_signer_mock_is_structural_input_not_quorum_assurance() -> None:
    request = payment_request()
    signer = Signer(
        account=WRONG_DESTINATION,
        signing_pub_key="02" + "22" * 32,
        txn_signature="AA",
    )
    parsed = XrplSignedPaymentInspector().inspect_payment(
        signed_blob(
            request,
            signing_pub_key="",
            txn_signature=None,
            signers=[signer],
        )
    )
    assert parsed.account == request.account


def test_live_transport_is_explicitly_disabled(tmp_path: Path) -> None:
    class LiveTransport(DeterministicMockTransport):
        offline_only = False

    transport = LiveTransport(ledger=ledger_before_expiry())
    with pytest.raises(ValueError, match="offline submission"):
        service_for(tmp_path / "payments.db", transport)


def journal_snapshot(database: Path) -> tuple[str, tuple[str, ...]]:
    digest = hashlib.sha256(database.read_bytes()).hexdigest()
    uri = f"{database.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        logical_contents = tuple(connection.iterdump())
    return digest, logical_contents


def test_genuine_x0_journal_is_refused_without_any_state_change(tmp_path: Path) -> None:
    database = tmp_path / "legacy.db"
    shutil.copyfile(LEGACY_FIXTURES / "x0_v1_journal.fixture", database)
    os.chmod(database, 0o640)
    before = journal_snapshot(database)
    before_mode = database.stat().st_mode & 0o777
    before_files = tuple(tmp_path.iterdir())

    for _ in range(3):
        with pytest.raises(
            LegacyJournalUnsupported, match=r"^LEGACY_JOURNAL_UNSUPPORTED:"
        ) as refusal:
            PaymentJournal(database)
        assert refusal.value.code == "LEGACY_JOURNAL_UNSUPPORTED"
        assert journal_snapshot(database) == before
        assert database.stat().st_mode & 0o777 == before_mode
        assert tuple(tmp_path.iterdir()) == before_files

    with sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True) as connection:
        rows = connection.execute(
            """SELECT operation_id, state, settlement_status, settlement_effect_claimed,
                      signed_transaction, transaction_hash, final_evidence
               FROM xrpl_payment_transactions ORDER BY operation_id"""
        ).fetchall()
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }

    assert [(row[0], row[1]) for row in rows] == [
        ("legacy-expired", "EXPIRED_NOT_INCLUDED"),
        ("legacy-failure", "VALIDATED_FAILURE"),
        ("legacy-pending", "PENDING_CONFIRMATION"),
        ("legacy-success", "VALIDATED_SUCCESS"),
    ]
    claimed = next(row for row in rows if row[0] == "legacy-success")
    assert claimed[2:4] == ("XRPL_SETTLED", 1)
    assert all(row[4] and row[5] for row in rows)
    assert all(row[6] for row in rows if row[1] != "PENDING_CONFIRMATION")
    assert "xrpl_settlement_outbox" not in tables


def test_empty_genuine_x0_journal_is_still_refused(tmp_path: Path) -> None:
    database = tmp_path / "legacy-empty.db"
    shutil.copyfile(LEGACY_FIXTURES / "x0_v1_empty_journal.fixture", database)
    before = journal_snapshot(database)

    with pytest.raises(LegacyJournalUnsupported):
        PaymentJournal(database)

    assert journal_snapshot(database) == before


def test_x0_schema_explicitly_marked_version_one_is_refused(tmp_path: Path) -> None:
    database = tmp_path / "legacy-version-one.db"
    shutil.copyfile(LEGACY_FIXTURES / "x0_v1_empty_journal.fixture", database)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version = 1")
    before = journal_snapshot(database)

    with pytest.raises(LegacyJournalUnsupported):
        PaymentJournal(database)

    assert journal_snapshot(database) == before


@pytest.mark.parametrize("precreate_empty_file", [False, True])
def test_genuinely_empty_database_initializes_directly_at_version_four(
    tmp_path: Path, precreate_empty_file: bool
) -> None:
    database = tmp_path / "new.db"
    if precreate_empty_file:
        database.touch()

    journal = PaymentJournal(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        } == {
            "xrpl_payment_transactions",
            "xrpl_submission_attempts",
            "xrpl_settlement_outbox",
        }
    assert journal.list_nonterminal() == ()


def test_valid_version_four_journal_reopens_normally(tmp_path: Path) -> None:
    database = tmp_path / "current.db"
    first = PaymentJournal(database)
    request = payment_request()
    expected = first.persist_signed(PreparedPayment(request), signed_blob(request))

    reopened = PaymentJournal(database)

    assert reopened.get(expected.operation_id) == expected


def test_version_three_journal_is_preserved_and_refused(tmp_path: Path) -> None:
    database = tmp_path / "version-three.db"
    PaymentJournal(database)
    with sqlite3.connect(database) as connection:
        for column in (
            "submission_anchor_network_id",
            "submission_anchor_build_version",
            "submission_anchor_node_public_key",
        ):
            connection.execute(f"ALTER TABLE xrpl_payment_transactions DROP COLUMN {column}")
        for column in ("lease_owner_token", "lease_expires_at"):
            connection.execute(f"ALTER TABLE xrpl_submission_attempts DROP COLUMN {column}")
        connection.execute("PRAGMA user_version = 3")
    before = journal_snapshot(database)

    with pytest.raises(SupersededJournalSchemaV3, match=r"^JOURNAL_VERSION_3_UNSUPPORTED:"):
        PaymentJournal(database)

    assert journal_snapshot(database) == before


def test_unknown_schema_version_fails_closed_without_changes(tmp_path: Path) -> None:
    database = tmp_path / "unknown.db"
    PaymentJournal(database)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version = 99")
    before = journal_snapshot(database)

    with pytest.raises(UnsupportedJournalSchema, match=r"^UNSUPPORTED_JOURNAL_SCHEMA:"):
        PaymentJournal(database)

    assert journal_snapshot(database) == before


def test_partially_migrated_x0_schema_fails_closed_without_changes(tmp_path: Path) -> None:
    database = tmp_path / "partial.db"
    shutil.copyfile(LEGACY_FIXTURES / "x0_v1_empty_journal.fixture", database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "ALTER TABLE xrpl_payment_transactions ADD COLUMN validated_ledger_hash TEXT"
        )
    before = journal_snapshot(database)

    with pytest.raises(UnsupportedJournalSchema):
        PaymentJournal(database)

    assert journal_snapshot(database) == before
