"""X1a end-to-end offline preflight: one Core-bound, 2-of-2 multisigned tRLUSD provider Payment.

Core settlement binding -> policy signer + ResearchBot signatures -> multisign verification ->
X0t journal -> (offline) submission -> reconciliation through the trusted-transport lookup ->
X0t outbox -> durable idempotent consumer. No network and no ledger writes.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
from x1a_rippled import FakeRippled, standard_ledger
from x1a_world import (
    AMOUNT,
    ASSET,
    FEE,
    ISSUER,
    LAST_LEDGER,
    NETWORK_ID,
    NOW,
    OPERATION,
    OWNER,
    PROVIDER_A,
    RULES,
    SEQUENCE,
    VALIDATED_LEDGER,
    expected_payment,
    policy_signer,
    researchbot_signature,
    signed_authorization,
    unsigned_payment,
)

from credence.xrpl_adapter.journal import PaymentJournal
from credence.xrpl_adapter.mock import DeterministicMockTransport
from credence.xrpl_adapter.multisign import assemble_multisigned, verify_multisigned_payment
from credence.xrpl_adapter.preparation import (
    TRLUSD_WIRE_CODE,
    OfflinePaymentPreparer,
    XrplSignedPaymentInspector,
)
from credence.xrpl_adapter.rippled import RippledTransport, RippledTransportConfig, RpcTimeout
from credence.xrpl_adapter.service import PaymentService
from credence.xrpl_adapter.settlement import (
    ConflictingSettlementEvent,
    PaymentOutcome,
    SqliteSettlementEventConsumer,
    classify_payment,
)
from credence.xrpl_adapter.types import (
    DuplicateFinancialOperation,
    PaymentRequest,
    SettlementEvent,
    SubmissionResponse,
    SubmissionTimeout,
    TransactionState,
)

CONFIG = RippledTransportConfig(
    source_id="local-rippled", expected_network_id=NETWORK_ID, expected_build_version="3.4.0"
)


@dataclass
class Flow:
    rippled: FakeRippled
    trusted: RippledTransport
    offline: DeterministicMockTransport
    service: PaymentService
    journal: PaymentJournal
    signed: bytes
    authorization_id: str


def build_flow(tmp_path: Path, *, name: str = "journal.sqlite3") -> Flow:
    rippled = standard_ledger()
    trusted = RippledTransport(rippled, CONFIG)
    transaction = unsigned_payment()

    # Credence's settlement authority binds the payment to Core's accounting settlement.
    statement = signed_authorization(transaction)
    ledger = trusted.ledger_context(RULES)
    # The policy signer reads the ledger itself through its own trusted transport (X1c, B3).
    policy_part = policy_signer(tmp_path, ledger_source=trusted).sign_payment(
        statement, transaction, ledger, now=NOW
    )
    signed = assemble_multisigned(transaction, [researchbot_signature(transaction), policy_part])
    verify_multisigned_payment(
        signed, policy=RULES, expected=expected_payment(transaction), ledger=ledger
    )

    offline = DeterministicMockTransport(ledger=trusted.validated_ledger(OWNER.account))
    journal = PaymentJournal(tmp_path / name)
    service = PaymentService(
        journal=journal,
        preparer=OfflinePaymentPreparer(maximum_fee_drops=RULES.maximum_fee_drops),
        inspector=XrplSignedPaymentInspector(),
        transport=offline,
        lookup=trusted,
        ledger_observer=trusted,
    )
    authorization_id = statement.authorization.authorization_id()
    service.prepare_and_persist(payment_request(authorization_id), signed)
    return Flow(rippled, trusted, offline, service, journal, signed, authorization_id)


def payment_request(authorization_id: str, operation_id: str = OPERATION) -> PaymentRequest:
    return PaymentRequest(
        operation_id=operation_id,
        payment_authorization_id=authorization_id,
        account=OWNER.account,
        destination=PROVIDER_A.account,
        asset=ASSET,
        amount_micro_units=AMOUNT,
        fee_drops=FEE,
        network_id=NETWORK_ID,
        last_ledger_sequence=LAST_LEDGER,
        earliest_ledger_sequence=VALIDATED_LEDGER,
        sequence=SEQUENCE,
    )


def delivered(value: str = "1.5") -> dict[str, str]:
    return {"currency": TRLUSD_WIRE_CODE, "issuer": ISSUER.account, "value": value}


def submit_accepted(flow: Flow) -> None:
    flow.offline.queue_submission(SubmissionResponse(True, "tesSUCCESS", "queued offline"))
    record = flow.service.submit(OPERATION)
    assert record.state is TransactionState.PENDING_CONFIRMATION


class EffectLog:
    """A destination effect that counts applications inside the consumer's transaction."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.applications: list[str] = []

    def __call__(self, connection: sqlite3.Connection, event: SettlementEvent) -> None:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS provider_payments_settled (event_id TEXT PRIMARY KEY)"
        )
        connection.execute("INSERT INTO provider_payments_settled VALUES (?)", (event.event_id,))
        with self.lock:
            self.applications.append(event.event_id)


# --- the one payment ------------------------------------------------------------------------


def test_one_bound_multisigned_payment_settles_once(tmp_path: Path) -> None:
    flow = build_flow(tmp_path)
    assert classify_payment(flow.service.status.status(OPERATION)) is PaymentOutcome.NOT_SUBMITTED

    submit_accepted(flow)
    assert flow.offline.submitted == [
        (flow.signed, flow.service.status.status(OPERATION).transaction_hash)
    ]
    record = flow.service.status.status(OPERATION)
    assert record.submission_anchor_ledger_hash == flow.rippled.ledger_hash
    assert record.submission_anchor_source_id == CONFIG.source_id
    assert classify_payment(record) is PaymentOutcome.SUBMISSION_PENDING

    flow.rippled.include(flow.signed, ledger_index=VALIDATED_LEDGER + 3, delivered=delivered())
    flow.rippled.advance(VALIDATED_LEDGER + 3)
    record = flow.service.reconcile(OPERATION)
    assert record.state is TransactionState.VALIDATED_SUCCESS
    assert record.payment_authorization_id == flow.authorization_id
    assert classify_payment(record) is PaymentOutcome.VALIDATED_SUCCESS

    effect = EffectLog()
    consumer = SqliteSettlementEventConsumer(tmp_path / "destination.sqlite3", effect)
    delivered_events = flow.service.deliver_settlement_events(consumer)
    assert len(delivered_events) == 1
    assert delivered_events[0].payment_authorization_id == flow.authorization_id
    # Redelivery and repeated reconciliation change nothing.
    assert flow.service.deliver_settlement_events(consumer) == ()
    consumer.apply(delivered_events[0])
    assert flow.service.reconcile(OPERATION).state is TransactionState.VALIDATED_SUCCESS
    assert effect.applications == [delivered_events[0].event_id]


def test_duplicate_journal_entry_for_the_same_authorization_is_refused(tmp_path: Path) -> None:
    flow = build_flow(tmp_path)
    with pytest.raises(DuplicateFinancialOperation):
        flow.service.prepare_and_persist(payment_request(flow.authorization_id), flow.signed)
    with pytest.raises(DuplicateFinancialOperation):
        flow.service.prepare_and_persist(
            payment_request(flow.authorization_id, "operation-x1a-again"), flow.signed
        )


# --- the seven outcomes ---------------------------------------------------------------------


def test_submission_timeout_is_unknown_then_resolved_by_hash(tmp_path: Path) -> None:
    flow = build_flow(tmp_path)
    flow.offline.queue_submission(SubmissionTimeout("lost response"))
    record = flow.service.submit(OPERATION)
    assert classify_payment(record) is PaymentOutcome.UNKNOWN_RECONCILIATION_REQUIRED

    def timeout(request: dict[str, Any]) -> dict[str, Any]:
        raise RpcTimeout("tx")

    flow.rippled.faults["tx"] = timeout
    record = flow.service.reconcile(OPERATION)
    assert classify_payment(record) is PaymentOutcome.UNKNOWN_RECONCILIATION_REQUIRED
    del flow.rippled.faults["tx"]
    flow.rippled.include(flow.signed, ledger_index=VALIDATED_LEDGER + 1, delivered=delivered())
    flow.rippled.advance(VALIDATED_LEDGER + 1)
    assert classify_payment(flow.service.reconcile(OPERATION)) is PaymentOutcome.VALIDATED_SUCCESS


def test_validated_ledger_failure(tmp_path: Path) -> None:
    flow = build_flow(tmp_path)
    submit_accepted(flow)
    flow.rippled.include(flow.signed, ledger_index=VALIDATED_LEDGER + 2, result="tecPATH_DRY")
    flow.rippled.advance(VALIDATED_LEDGER + 2)
    record = flow.service.reconcile(OPERATION)
    assert record.state is TransactionState.VALIDATED_FAILURE
    assert classify_payment(record) is PaymentOutcome.VALIDATED_FAILURE
    assert flow.journal.pending_settlement_events() == ()


def test_validated_success_with_unexpected_delivery_is_a_funds_exception(tmp_path: Path) -> None:
    flow = build_flow(tmp_path)
    submit_accepted(flow)
    flow.rippled.include(flow.signed, ledger_index=VALIDATED_LEDGER + 2, delivered=delivered("1.4"))
    flow.rippled.advance(VALIDATED_LEDGER + 2)
    record = flow.service.reconcile(OPERATION)
    assert record.state is TransactionState.VALIDATED_UNEXPECTED_DELIVERY
    assert record.final_result == "DELIVERED_AMOUNT_MISMATCH"
    assert classify_payment(record) is PaymentOutcome.VALIDATED_UNEXPECTED_DELIVERY
    assert flow.journal.pending_settlement_events() == ()


def test_expiry_with_proven_non_inclusion(tmp_path: Path) -> None:
    flow = build_flow(tmp_path)
    submit_accepted(flow)
    flow.rippled.advance(LAST_LEDGER + 1)
    record = flow.service.reconcile(OPERATION)
    assert record.state is TransactionState.EXPIRED_NOT_INCLUDED
    assert classify_payment(record) is PaymentOutcome.EXPIRED_NOT_INCLUDED


def test_missing_history_leaves_expiry_unknown(tmp_path: Path) -> None:
    flow = build_flow(tmp_path)
    submit_accepted(flow)
    flow.rippled.advance(
        LAST_LEDGER + 1,
        complete=f"1-{VALIDATED_LEDGER + 2},{VALIDATED_LEDGER + 5}-{LAST_LEDGER + 1}",
    )
    record = flow.service.reconcile(OPERATION)
    assert record.state is TransactionState.UNKNOWN
    assert record.last_provisional_status == "HISTORY_INCOMPLETE_NON_INCLUSION_UNPROVEN"
    assert classify_payment(record) is PaymentOutcome.UNKNOWN_RECONCILIATION_REQUIRED


def test_incorrect_validated_evidence_stays_unknown(tmp_path: Path) -> None:
    flow = build_flow(tmp_path)
    submit_accepted(flow)
    flow.rippled.include(flow.signed, ledger_index=VALIDATED_LEDGER + 2, delivered=delivered())
    flow.rippled.advance(VALIDATED_LEDGER + 3)
    included = VALIDATED_LEDGER + 2
    flow.rippled.faults["ledger"] = lambda request: (
        {"ledger_hash": "B2" * 32, "ledger_index": included, "validated": True}
        if request["ledger_index"] == included
        else None
    )
    record = flow.service.reconcile(OPERATION)
    assert record.state is TransactionState.UNKNOWN
    assert record.last_provisional_status == "LEDGER_HASH_MISMATCH"
    assert classify_payment(record) is PaymentOutcome.UNKNOWN_RECONCILIATION_REQUIRED


def test_classification_fails_closed_on_inconsistent_records(tmp_path: Path) -> None:
    flow = build_flow(tmp_path)
    submit_accepted(flow)
    flow.rippled.include(flow.signed, ledger_index=VALIDATED_LEDGER + 2, result="tecPATH_DRY")
    flow.rippled.advance(VALIDATED_LEDGER + 2)
    record = flow.service.reconcile(OPERATION)

    assert (
        classify_payment(replace(record, final_result="DELIVERED_AMOUNT_MISMATCH"))
        is PaymentOutcome.UNKNOWN_RECONCILIATION_REQUIRED
    )
    assert (
        classify_payment(replace(record, final_evidence=b"not json"))
        is PaymentOutcome.UNKNOWN_RECONCILIATION_REQUIRED
    )


# --- consumer idempotency and concurrency ---------------------------------------------------


def settled_flow(tmp_path: Path) -> Flow:
    flow = build_flow(tmp_path)
    submit_accepted(flow)
    flow.rippled.include(flow.signed, ledger_index=VALIDATED_LEDGER + 1, delivered=delivered())
    flow.rippled.advance(VALIDATED_LEDGER + 1)
    flow.service.reconcile(OPERATION)
    return flow


def test_concurrent_settlement_event_consumption_applies_once(tmp_path: Path) -> None:
    flow = settled_flow(tmp_path)
    (event,) = flow.journal.pending_settlement_events()
    effect = EffectLog()
    consumers = [
        SqliteSettlementEventConsumer(tmp_path / "destination.sqlite3", effect) for _ in range(8)
    ]
    barrier = threading.Barrier(len(consumers))
    errors: list[BaseException] = []

    def consume(consumer: SqliteSettlementEventConsumer) -> None:
        barrier.wait()
        try:
            consumer.apply(event)
        except BaseException as exc:  # pragma: no cover - surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=consume, args=(c,)) for c in consumers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert effect.applications == [event.event_id]


def test_concurrent_outbox_delivery_from_two_services_applies_once(tmp_path: Path) -> None:
    flow = settled_flow(tmp_path)
    second = PaymentService(
        journal=PaymentJournal(tmp_path / "journal.sqlite3"),
        preparer=OfflinePaymentPreparer(maximum_fee_drops=RULES.maximum_fee_drops),
        inspector=XrplSignedPaymentInspector(),
        transport=flow.offline,
        lookup=flow.trusted,
        ledger_observer=flow.trusted,
    )
    effect = EffectLog()
    consumer = SqliteSettlementEventConsumer(tmp_path / "destination.sqlite3", effect)
    barrier = threading.Barrier(2)

    def deliver(service: PaymentService) -> None:
        barrier.wait()
        service.deliver_settlement_events(consumer)

    threads = [threading.Thread(target=deliver, args=(s,)) for s in (flow.service, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(effect.applications) == 1
    assert flow.journal.pending_settlement_events() == ()


def test_failed_effect_rolls_back_and_is_retried(tmp_path: Path) -> None:
    flow = settled_flow(tmp_path)
    (event,) = flow.journal.pending_settlement_events()
    attempts: list[int] = []

    def flaky(connection: sqlite3.Connection, settled: SettlementEvent) -> None:
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("destination crashed mid-effect")

    consumer = SqliteSettlementEventConsumer(tmp_path / "destination.sqlite3", flaky)
    assert flow.service.deliver_settlement_events(consumer) == ()
    assert not consumer.applied(event.event_id)
    assert len(flow.service.deliver_settlement_events(consumer)) == 1
    assert consumer.applied(event.event_id) and len(attempts) == 2


def test_contradictory_events_are_refused(tmp_path: Path) -> None:
    flow = settled_flow(tmp_path)
    (event,) = flow.journal.pending_settlement_events()
    consumer = SqliteSettlementEventConsumer(tmp_path / "destination.sqlite3", EffectLog())
    consumer.apply(event)

    with pytest.raises(ConflictingSettlementEvent):
        consumer.apply(replace(event, payload=event.payload.replace(b"tesSUCCESS", b"tecXXXXXXX")))
    # A second event ID for the same operation and hash is a second settlement channel.
    with pytest.raises(ConflictingSettlementEvent):
        consumer.apply(replace(event, event_id="0" * 64))
    with pytest.raises(ConflictingSettlementEvent):
        consumer.apply(replace(event, operation_id="operation-forged"))


def test_success_without_usable_delivered_amount_stays_unknown(tmp_path: Path) -> None:
    flow = build_flow(tmp_path)
    submit_accepted(flow)
    # tesSUCCESS but the node reports delivered_amount "unavailable": never XRPL_SETTLED.
    flow.rippled.include(flow.signed, ledger_index=VALIDATED_LEDGER + 2, delivered=None)
    flow.rippled.advance(VALIDATED_LEDGER + 2)
    record = flow.service.reconcile(OPERATION)
    assert record.state is TransactionState.UNKNOWN
    # X1c B1: binary metadata falls back to the signed Amount, which JSON does not confirm.
    assert record.last_provisional_status == "EVIDENCE_INCONSISTENT"
    assert flow.journal.pending_settlement_events() == ()
