"""Integration I1c: process interruption, recovery and reconciliation.

A crash is simulated by a hook that raises SimulatedCrash (a BaseException, so no handler in the
service catches it) at a named lifecycle point. The service is then closed, releasing ownership as
process death would, and a new service opens the same database and recovers. Recovery reconciles
the journal with Core's reservation, attempt and settlement records first, and never re-executes
a provider, charges twice, releases a hold whose delivery may have happened, or fabricates a
receipt. Offline: no network, model or XRPL.
"""

from __future__ import annotations

import socket
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from i1_world import (
    AGENT_KEY_ID,
    GENERAL_FACILITY,
    NOW,
    TTL_SECONDS,
    Clock,
    CrashDuringExecution,
    SimulatedCrash,
    Wrapped,
    agent_signer,
    count,
    crash_at,
    job,
    market_world,
    open_service,
)
from market_world import MarketWorld

from credence.core.primitives import ReservationId, ReservationStatus, Sha256Digest
from credence.core.settlement_coordinator import NoDeliveryReason, UnknownDeliveryCause
from credence.inference.provider import (
    ProviderExecutionError,
    ProviderRequest,
    ProviderResult,
    ProviderUnavailableError,
)
from credence.integration.journal import Operation
from credence.integration.journal import OperationState as S
from credence.integration.service import (
    IntegrationService,
    JournalInconsistency,
    PurchaseReport,
    ResponseUnavailable,
)

FID = GENERAL_FACILITY.facility_id
WITHIN_TTL = NOW.plus_seconds(60)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield


def crash_then_reopen(
    db: Path, world: MarketWorld, point: str, *, recover: bool = True
) -> tuple[IntegrationService, Operation]:
    clock = Clock()
    service = open_service(
        db, world, clock=clock, register=(GENERAL_FACILITY,), crash_hook=crash_at(point)
    )
    with pytest.raises(SimulatedCrash):
        service.purchase(job(1), agent_signer)
    (pending,) = service.pending_operations()
    service.close()  # the process is gone: the OS drops its ownership lock
    reopened = open_service(db, world, clock=clock, recover=recover)
    return reopened, pending


def provider_calls(world: MarketWorld) -> int:
    return len(world.nebius_transport.posts)


def held(service: IntegrationService) -> int:
    return service.coordinator.exposure(FID, WITHIN_TTL).held.value


def next_purchase_settles(service: IntegrationService) -> None:
    """After recovery the facility is not busy, and the next receipt chains to Core's head."""
    head = service.coordinator.chain_head(FID)
    report = service.purchase(job(2), agent_signer)
    assert isinstance(report, PurchaseReport) and report.state is S.SETTLED, report
    receipt = service.journal_entry(report.operation_id).receipt()
    assert receipt is not None and receipt.previous_receipt_digest == head


# --- 1. before reservation -----------------------------------------------------------------------


def test_crash_before_reservation_abandons_with_nothing_held(tmp_path: Path) -> None:
    world = market_world()
    service, pending = crash_then_reopen(tmp_path / "i.sqlite3", world, "after_intent")
    op = service.journal_entry(pending.operation_id)
    assert op.state is S.ABANDONED
    with pytest.raises(Exception, match="NOT_FOUND"):
        service.ledger.get(ReservationId(str(op.reservation_id)))
    assert held(service) == 0 and provider_calls(world) == 0
    next_purchase_settles(service)


# --- 2. after reservation, before inference ------------------------------------------------------


@pytest.mark.parametrize("point", ["after_reserve", "after_executing_recorded"])
def test_crash_after_reservation_before_any_attempt_releases_auditably(
    tmp_path: Path, point: str
) -> None:
    """No Core delivery attempt exists, so no provider was called: Core's audited release."""
    world = market_world()
    service, pending = crash_then_reopen(tmp_path / "i.sqlite3", world, point)
    op = service.journal_entry(pending.operation_id)
    assert op.state is S.RELEASED_UNDELIVERED
    (release,) = service.coordinator.no_delivery_releases()
    assert release.reservation_id.value == op.reservation_id
    assert release.reason is NoDeliveryReason.ABANDONED_BEFORE_EXECUTION
    assert held(service) == 0 and provider_calls(world) == 0
    next_purchase_settles(service)


def test_crash_after_attempt_begun_before_the_call_is_unknown_not_released(tmp_path: Path) -> None:
    """Core authorized execution; it cannot prove the provider was not called. Fail closed."""
    world = market_world()
    service, pending = crash_then_reopen(tmp_path / "i.sqlite3", world, "after_attempt_begun")
    op = service.journal_entry(pending.operation_id)
    assert op.state is S.UNKNOWN
    rid = ReservationId(str(op.reservation_id))
    attempt = service.coordinator.delivery_attempt(rid)
    assert attempt is not None and attempt.unknown_cause is UnknownDeliveryCause.INTERRUPTED
    assert service.ledger.get(rid).status is ReservationStatus.HELD
    assert service.coordinator.no_delivery_releases() == []
    assert provider_calls(world) == 0
    next_purchase_settles(service)


# --- 3 and 4. provider outstanding / returned but not captured -----------------------------------


def test_crash_while_provider_inference_is_outstanding_is_unknown(tmp_path: Path) -> None:
    world = market_world()
    crashing = CrashDuringExecution(world.nebius)
    db = tmp_path / "i.sqlite3"
    clock = Clock()
    service = open_service(
        db, world, providers=(crashing, world.local), clock=clock, register=(GENERAL_FACILITY,)
    )
    with pytest.raises(SimulatedCrash):
        service.purchase(job(1), agent_signer)
    (pending,) = service.pending_operations()
    assert pending.state is S.EXECUTING
    service.close()

    reopened = open_service(db, world, clock=clock)
    op = reopened.journal_entry(pending.operation_id)
    assert op.state is S.UNKNOWN and op.receipt_digest is None and op.response is None
    rid = ReservationId(str(op.reservation_id))
    assert reopened.ledger.get(rid).status is ReservationStatus.HELD  # not released as failed
    assert reopened.coordinator.exposure(FID, clock()).drawn.value == 0  # not charged
    assert count(db, "SELECT COUNT(*) FROM accounting_settlements") == 0  # no fabricated receipt
    assert crashing.calls == 1  # never re-executed
    reopened.recover()
    assert crashing.calls == 1
    # the hold lapses only by the 600-second expiry
    clock.advance(TTL_SECONDS + 5)
    assert reopened.coordinator.exposure(FID, clock()).held.value == 0
    assert reopened.journal_entry(pending.operation_id).state is S.UNKNOWN


def test_crash_after_provider_returned_before_evidence_is_stored_is_unknown(tmp_path: Path) -> None:
    world = market_world()
    service, pending = crash_then_reopen(tmp_path / "i.sqlite3", world, "after_provider_returned")
    op = service.journal_entry(pending.operation_id)
    assert op.state is S.UNKNOWN and op.response is None
    assert provider_calls(world) == 1
    assert (
        service.ledger.get(ReservationId(str(op.reservation_id))).status is ReservationStatus.HELD
    )
    assert count(tmp_path / "i.sqlite3", "SELECT COUNT(*) FROM accounting_settlements") == 0


# --- 5. after evidence storage, before settlement ------------------------------------------------


def test_crash_after_evidence_is_stored_settles_on_recovery_and_recovers_the_response(
    tmp_path: Path,
) -> None:
    world = market_world()
    service, pending = crash_then_reopen(tmp_path / "i.sqlite3", world, "after_evidence_stored")
    op = service.journal_entry(pending.operation_id)
    assert op.state is S.SETTLED
    assert service.coordinator.receipt_settled(Sha256Digest(str(op.receipt_digest)))
    assert service.retrieve(op.operation_id, AGENT_KEY_ID) == pending.response
    assert provider_calls(world) == 1


# --- 6. after settlement, before retrieval -------------------------------------------------------


def test_crash_after_core_settled_before_the_journal_reconciles_without_settling_twice(
    tmp_path: Path,
) -> None:
    world = market_world()
    db = tmp_path / "i.sqlite3"
    service, pending = crash_then_reopen(db, world, "after_settle_committed", recover=False)
    assert service.journal_entry(pending.operation_id).state is S.DELIVERED
    assert service.coordinator.receipt_settled(Sha256Digest(str(pending.receipt_digest)))
    (rec,) = service.recover()
    assert (rec.before, rec.after) == (S.DELIVERED, S.SETTLED)
    assert count(db, "SELECT COUNT(*) FROM accounting_settlements") == 1
    assert count(db, "SELECT COUNT(*) FROM settlement_disputes") == 0
    assert service.retrieve(pending.operation_id, AGENT_KEY_ID) == pending.response


def test_restart_after_settlement_before_retrieval_keeps_the_response(tmp_path: Path) -> None:
    world = market_world()
    db = tmp_path / "i.sqlite3"
    clock = Clock()
    service = open_service(db, world, clock=clock, register=(GENERAL_FACILITY,))
    report = service.purchase(job(1), agent_signer)
    assert isinstance(report, PurchaseReport) and report.state is S.SETTLED
    service.close()
    reopened = open_service(db, world, clock=clock)
    response = reopened.retrieve(report.operation_id, AGENT_KEY_ID)
    assert response and response == reopened.journal_entry(report.operation_id).response


# --- 7. after retrieval, before acknowledgment ---------------------------------------------------


def test_restart_after_retrieval_before_acknowledgment_keeps_the_response(tmp_path: Path) -> None:
    world = market_world()
    db = tmp_path / "i.sqlite3"
    clock = Clock()
    service = open_service(db, world, clock=clock, register=(GENERAL_FACILITY,))
    report = service.purchase(job(1), agent_signer)
    assert isinstance(report, PurchaseReport)
    first = service.retrieve(report.operation_id, AGENT_KEY_ID)
    service.close()  # crash before acknowledging

    reopened = open_service(db, world, clock=clock)
    assert reopened.operation(report.operation_id).state is S.RETRIEVED
    assert reopened.retrieve(report.operation_id, AGENT_KEY_ID) == first
    reopened.acknowledge(report.operation_id, AGENT_KEY_ID)
    op = reopened.journal_entry(report.operation_id)
    assert op.state is S.ACKNOWLEDGED and op.response is None and op.response_digest
    with pytest.raises(ResponseUnavailable):
        reopened.retrieve(report.operation_id, AGENT_KEY_ID)


# --- unknown provider outcome --------------------------------------------------------------------


class FailsAfterExecuting(Wrapped):
    def __init__(self, inner: object, error: Exception) -> None:
        super().__init__(inner)  # type: ignore[arg-type]
        self._error = error

    def execute(self, request: ProviderRequest) -> ProviderResult:
        super().execute(request)
        raise self._error


@pytest.mark.parametrize(
    ("error", "cause"),
    [
        (ProviderExecutionError("timeout"), UnknownDeliveryCause.PROVIDER_EXECUTION_ERROR),
        (ProviderUnavailableError("503"), UnknownDeliveryCause.PROVIDER_UNAVAILABLE),
    ],
)
def test_provider_failure_after_the_attempt_is_unknown_and_never_retried(
    tmp_path: Path, error: Exception, cause: UnknownDeliveryCause
) -> None:
    world = market_world()
    failing = FailsAfterExecuting(world.nebius, error)
    db = tmp_path / "i.sqlite3"
    service = open_service(
        db, world, providers=(failing, world.local), register=(GENERAL_FACILITY,)
    )
    report = service.purchase(job(1), agent_signer)
    assert isinstance(report, PurchaseReport) and report.state is S.UNKNOWN
    rid = ReservationId(str(report.reservation_id))
    attempt = service.coordinator.delivery_attempt(rid)
    assert attempt is not None and attempt.unknown_cause is cause
    assert service.ledger.get(rid).status is ReservationStatus.HELD
    assert count(db, "SELECT COUNT(*) FROM accounting_settlements") == 0
    again = service.purchase(job(1), agent_signer)  # same idempotency key
    assert again == report and failing.calls == 1
    with pytest.raises(ResponseUnavailable):
        service.retrieve(report.operation_id, AGENT_KEY_ID)


# --- replay after restart ------------------------------------------------------------------------


def test_replay_after_restart_never_buys_twice(tmp_path: Path) -> None:
    world = market_world()
    db = tmp_path / "i.sqlite3"
    clock = Clock()
    service = open_service(db, world, clock=clock, register=(GENERAL_FACILITY,))
    first = service.purchase(job(1), agent_signer)
    assert isinstance(first, PurchaseReport) and first.state is S.SETTLED
    drawn = service.coordinator.exposure(FID, clock()).drawn
    service.close()

    reopened = open_service(db, world, clock=clock)
    replay = reopened.purchase(job(1), agent_signer)
    assert replay == first
    assert provider_calls(world) == 1
    assert reopened.coordinator.exposure(FID, clock()).drawn == drawn
    assert count(db, "SELECT COUNT(*) FROM accounting_settlements") == 1
    assert count(db, "SELECT COUNT(*) FROM reservations") == 1


# --- journal / accounting reconciliation ---------------------------------------------------------


def test_journal_claiming_settlement_core_lacks_fails_closed(tmp_path: Path) -> None:
    world = market_world()
    db = tmp_path / "i.sqlite3"
    service, pending = crash_then_reopen(db, world, "after_evidence_stored", recover=False)
    service.close()
    with sqlite3.connect(db) as conn:  # tamper: the journal claims a settlement Core never made
        conn.execute(
            "UPDATE integration_operations SET state = 'settled' WHERE operation_id = ?",
            (pending.operation_id,),
        )
    with pytest.raises(JournalInconsistency):
        open_service(db, world)
    # the failed open released ownership and changed nothing in Core
    service = open_service(db, world, recover=False)
    assert not service.coordinator.receipt_settled(Sha256Digest(str(pending.receipt_digest)))
    with pytest.raises(JournalInconsistency):
        service.retrieve(pending.operation_id, AGENT_KEY_ID)


def test_facility_with_an_unreconciled_operation_refuses_new_purchases(tmp_path: Path) -> None:
    world = market_world()
    service, _ = crash_then_reopen(
        tmp_path / "i.sqlite3", world, "after_evidence_stored", recover=False
    )
    refused = service.purchase(job(2), agent_signer)
    assert not isinstance(refused, PurchaseReport)
    assert refused.refusal.value == "FACILITY_BUSY"
    service.recover()
    next_purchase_settles(service)
