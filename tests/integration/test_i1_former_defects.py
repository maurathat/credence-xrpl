"""Integration I1c: positive regressions for the 11 defects characterized in I1b.

One test per I1b characterization, in the same order and with the same ID, now asserting the
approved behaviour through the integrated path (IntegrationService) and Core:

  P1  stale facility accounting             -> refreshed from Core before signing
  P2a verify advanced chain, released reply -> chain moves only on Core settlement; reply after it
  P2b CHAIN_BROKEN released, kept no proof  -> hold kept, dispute and exact evidence kept
  chain divergence persisted                -> receipts always chain to Core's persisted head
  gateway restart forgot the chain          -> restart chains to Core's persisted head
  two gateways served one facility          -> second owner refused
  concurrent purchases disagreed            -> one in-flight purchase per facility; both settle
  concurrent ReceiptChainError              -> no gateway chain movement, no error
  P5 unregistered facility overspent        -> refused before any reservation
  P6 provider failure, no audited release   -> Core's audited no-delivery release
  late delivery, no dispute or evidence     -> late-delivery dispute with exact evidence

Offline: fake Nebius transport and fake signing provider; no network, model or XRPL. Amounts are
integer micro-tRLUSD (tRLUSD is a test token).
"""

from __future__ import annotations

import json
import socket
import sqlite3
import threading
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from i1_world import (
    CONTROLLED,
    CONTROLLED_FACILITY,
    GENERAL_FACILITY,
    NOW,
    TTL_SECONDS,
    Clock,
    ClockJump,
    ConcurrencyMeter,
    SimulatedCrash,
    agent_signer,
    count,
    crash_at,
    database_contains,
    job,
    market_world,
    open_service,
    retrieve,
    stale,
)

from credence.core.objects import DecisionRecord
from credence.core.primitives import MicroTrlusd, ReservationId, ReservationStatus, Sha256Digest
from credence.core.reservation_ledger import ReservationLedger, ReservationStateError
from credence.core.settlement_coordinator import (
    NoDeliveryReason,
    PurchaseReference,
    SettlementCoordinator,
    SettlementRefused,
)
from credence.inference.receipt_state import ReceiptChain
from credence.integration.journal import OperationState as S
from credence.integration.ownership import OwnershipError
from credence.integration.service import (
    IntegrationService,
    PurchaseRefused,
    PurchaseReport,
    Refusal,
    ResponseUnavailable,
    operation_id_for,
)

FID = GENERAL_FACILITY.facility_id


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield


@pytest.fixture(autouse=True)
def gateway_chain_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    """The integrated path never reads or advances the gateway's in-memory receipt chain."""

    def boom(*args: object) -> None:
        raise AssertionError("integrated path touched the gateway's in-memory receipt chain")

    monkeypatch.setattr(ReceiptChain, "advance", boom)
    monkeypatch.setattr(ReceiptChain, "head", boom)


def settled_report(report: object) -> PurchaseReport:
    assert isinstance(report, PurchaseReport), report
    assert report.state is S.SETTLED, report
    return report


def receipt_digest(service: IntegrationService, report: PurchaseReport) -> Sha256Digest:
    return Sha256Digest(str(service.journal_entry(report.operation_id).receipt_digest))


def previous_of(service: IntegrationService, report: PurchaseReport) -> Sha256Digest | None:
    receipt = service.journal_entry(report.operation_id).receipt()
    assert receipt is not None
    return receipt.previous_receipt_digest


# --- P1 -------------------------------------------------------------------------------------------


def test_fixed_p1_stale_facility_is_refreshed_from_core_before_signing(tmp_path: Path) -> None:
    service = open_service(tmp_path / "i.sqlite3", market_world(), register=(GENERAL_FACILITY,))
    settled_report(service.purchase(job(1), agent_signer))
    drawn = service.accounting.exposure(FID, NOW).drawn
    assert drawn.value > 0
    second = settled_report(service.purchase(job(2, stale(GENERAL_FACILITY)), agent_signer))
    # the policy decision behind the reservation was made against Core's stored drawn amount
    with sqlite3.connect(tmp_path / "i.sqlite3") as conn:
        (policy_digest,) = conn.execute(
            "SELECT decision_record_digest FROM reservations WHERE reservation_id = ?",
            (second.reservation_id,),
        ).fetchone()
        (policy_bytes,) = conn.execute(
            "SELECT canonical FROM decision_records WHERE digest = ?", (policy_digest,)
        ).fetchone()
    policy = DecisionRecord.from_canonical(json.loads(policy_bytes))
    assert policy.input_digests[1] == replace(GENERAL_FACILITY, drawn_micro_trlusd=drawn).digest()
    assert service.accounting.exposure(FID, NOW).drawn.value > drawn.value
    # a facility whose terms disagree with Core's stored limit is refused, never signed
    wrong = replace(GENERAL_FACILITY, limit_micro_trlusd=MicroTrlusd(1))
    refused = service.purchase(job(3, wrong), agent_signer)
    assert isinstance(refused, PurchaseRefused)
    assert refused.refusal is Refusal.FACILITY_LIMIT_MISMATCH


# --- P2 -------------------------------------------------------------------------------------------


def test_fixed_p2_chain_moves_only_on_core_settlement_and_reply_follows_it(tmp_path: Path) -> None:
    world = market_world()
    db = tmp_path / "i.sqlite3"
    service = open_service(
        db, world, register=(GENERAL_FACILITY,), crash_hook=crash_at("after_evidence_stored")
    )
    with pytest.raises(SimulatedCrash):
        service.purchase(job(1), agent_signer)
    (pending,) = service.pending_operations()
    service.close()
    reopened = open_service(db, world, recover=False)
    # delivered and verified-able, but not settled: no chain movement, no reply
    assert reopened.accounting.chain_head(FID) is None
    with pytest.raises(ResponseUnavailable):
        retrieve(reopened, pending.operation_id)
    reopened.recover()
    assert reopened.accounting.chain_head(FID) == Sha256Digest(str(pending.receipt_digest))
    assert retrieve(reopened, pending.operation_id) == pending.response


def test_fixed_p2_chain_broken_keeps_the_hold_the_dispute_and_the_evidence(tmp_path: Path) -> None:
    """CHAIN_BROKEN cannot arise in the integrated path; force it by moving Core's head out of
    band between evidence capture and settlement."""
    world = market_world()
    db = tmp_path / "i.sqlite3"
    service = open_service(
        db, world, register=(GENERAL_FACILITY,), crash_hook=crash_at("after_evidence_stored")
    )
    with pytest.raises(SimulatedCrash):
        service.purchase(job(1), agent_signer)
    (pending,) = service.pending_operations()
    service.close()
    foreign = "sha256:" + "ee" * 32
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO facility_chain_heads (facility_id, receipt_digest) VALUES (?, ?)",
            (FID.value, foreign),
        )

    reopened = open_service(db, world)
    op = reopened.journal_entry(pending.operation_id)
    assert op.state is S.DISPUTED and op.reason_code == "CHAIN_BROKEN"
    rid = ReservationId(str(op.reservation_id))
    assert reopened.accounting.reservation(rid).status is ReservationStatus.HELD  # not released
    assert reopened.accounting.exposure(FID, NOW).drawn.value == 0  # not charged
    assert reopened.accounting.chain_head(FID) == Sha256Digest(foreign)  # not advanced
    digest = Sha256Digest(str(op.receipt_digest))
    ((kind, reason, evidence),) = reopened.accounting.dispute_evidence(digest)
    assert (kind, reason) == ("rejected_submission", "CHAIN_BROKEN")
    receipt = op.receipt()
    assert receipt is not None and evidence.receipt == receipt.canonical_bytes()
    assert op.response == pending.response  # the delivered reply is retained, not released
    with pytest.raises(ResponseUnavailable):
        retrieve(reopened, op.operation_id)


# --- chain divergence -----------------------------------------------------------------------------


def test_fixed_chain_divergence_receipts_chain_to_core_after_a_dispute(tmp_path: Path) -> None:
    """After an unknown outcome, Core's head is unchanged and the next receipt chains to it."""
    world = market_world()
    db = tmp_path / "i.sqlite3"
    service = open_service(db, world, register=(GENERAL_FACILITY,))
    first = settled_report(service.purchase(job(1), agent_signer))
    service.close()
    crashing = open_service(db, world, crash_hook=crash_at("after_provider_returned"))
    with pytest.raises(SimulatedCrash):
        crashing.purchase(job(2), agent_signer)
    crashing.close()
    service = open_service(db, world)  # job 2 reconciles to UNKNOWN
    assert service.operation(operation_id_for("job-facility-market-general-2")).state is S.UNKNOWN
    third = settled_report(service.purchase(job(3), agent_signer))
    assert previous_of(service, third) == receipt_digest(service, first)
    assert service.accounting.chain_head(FID) == receipt_digest(service, third)


# --- gateway restart ------------------------------------------------------------------------------


def test_fixed_gateway_restart_chains_to_cores_persisted_head(tmp_path: Path) -> None:
    world = market_world()
    db = tmp_path / "i.sqlite3"
    clock = Clock()
    service = open_service(db, world, clock=clock, register=(GENERAL_FACILITY,))
    first = settled_report(service.purchase(job(1), agent_signer))
    service.close()
    restarted = open_service(db, world, clock=clock)
    second = settled_report(restarted.purchase(job(2), agent_signer))
    assert previous_of(restarted, second) == receipt_digest(restarted, first)


def test_fixed_two_gateway_processes_cannot_serve_one_database(tmp_path: Path) -> None:
    db = tmp_path / "i.sqlite3"
    service = open_service(db, market_world(), register=(GENERAL_FACILITY,))
    with pytest.raises(OwnershipError):
        open_service(db, market_world())
    service.close()


# --- concurrent purchases -------------------------------------------------------------------------


def _concurrently(service: IntegrationService, jobs: list[int], facility, procurement):  # type: ignore[no-untyped-def]
    results: dict[int, object] = {}

    def run(index: int) -> None:
        try:
            results[index] = service.purchase(job(index, facility, procurement), agent_signer)
        except Exception as exc:  # recorded for the assertion
            results[index] = exc

    threads = [threading.Thread(target=run, args=(index,)) for index in jobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return results


def test_fixed_concurrent_same_facility_purchases_are_serialized_and_both_settle(
    tmp_path: Path,
) -> None:
    world = market_world()
    meter = ConcurrencyMeter(world.local)
    service = open_service(
        tmp_path / "i.sqlite3",
        world,
        providers=(world.nebius, meter),
        register=(CONTROLLED_FACILITY,),
    )
    results = _concurrently(service, [1, 2, 3, 4], CONTROLLED_FACILITY, CONTROLLED)
    reports = [settled_report(r) for r in results.values()]
    assert meter.max_active == 1 and meter.calls == 4
    previous = sorted(
        (previous_of(service, r) for r in reports), key=lambda d: "" if d is None else d.value
    )
    digests = {receipt_digest(service, r) for r in reports}
    assert previous.count(None) == 1  # exactly one first receipt; the rest form one chain
    assert {p for p in previous if p is not None} <= digests
    assert service.accounting.disputes() == []
    exposure = service.accounting.exposure(CONTROLLED_FACILITY.facility_id, NOW)
    assert exposure.held.value == 0


def test_fixed_concurrent_purchases_never_touch_the_gateway_chain(tmp_path: Path) -> None:
    """The autouse guard makes any read or advance of the gateway's chain fail."""
    world = market_world()
    service = open_service(tmp_path / "i.sqlite3", world, register=(CONTROLLED_FACILITY,))
    results = _concurrently(service, [1, 2], CONTROLLED_FACILITY, CONTROLLED)
    for result in results.values():
        settled_report(result)


# --- P5 -------------------------------------------------------------------------------------------


def test_fixed_p5_unregistered_facility_is_refused_before_any_reservation(tmp_path: Path) -> None:
    world = market_world()
    db = tmp_path / "i.sqlite3"
    service = open_service(db, world)  # nothing registered
    refused = service.purchase(job(1), agent_signer)
    assert isinstance(refused, PurchaseRefused)
    assert refused.refusal is Refusal.UNREGISTERED_FACILITY
    assert count(db, "SELECT COUNT(*) FROM reservations") == 0
    assert count(db, "SELECT COUNT(*) FROM integration_operations") == 0
    assert world.nebius_transport.posts == [] and world.local.calls == 0


# --- P6 -------------------------------------------------------------------------------------------


def test_fixed_p6_provider_not_connected_is_released_through_core_with_audit(
    tmp_path: Path,
) -> None:
    world = market_world()
    service = open_service(
        tmp_path / "i.sqlite3",
        world,
        providers=(world.nebius,),  # the local provider is not connected
        register=(CONTROLLED_FACILITY,),
    )
    report = service.purchase(job(1, CONTROLLED_FACILITY, CONTROLLED), agent_signer)
    assert isinstance(report, PurchaseReport) and report.state is S.RELEASED_UNDELIVERED
    (audit,) = service.accounting.no_delivery_releases()
    assert audit.reservation_id.value == report.reservation_id
    assert audit.reason is NoDeliveryReason.PROVIDER_NOT_CONNECTED
    assert service.accounting.exposure(CONTROLLED_FACILITY.facility_id, NOW).held.value == 0
    assert service.accounting.delivery_attempt(ReservationId(str(report.reservation_id))) is None
    # the bare ledger release is no longer an unaudited bypass
    service2 = open_service(tmp_path / "j.sqlite3", market_world(), register=(CONTROLLED_FACILITY,))
    blocked = service2.purchase(job(2, CONTROLLED_FACILITY, CONTROLLED), agent_signer)
    assert isinstance(blocked, PurchaseReport)
    with pytest.raises(ReservationStateError, match="USE_SETTLEMENT_COORDINATOR"):
        ReservationLedger(tmp_path / "j.sqlite3").release(
            ReservationId(str(blocked.reservation_id)), NOW
        )


# --- late delivery --------------------------------------------------------------------------------


def test_fixed_late_delivery_is_disputed_with_evidence_never_settled(tmp_path: Path) -> None:
    world = market_world()
    clock = Clock()
    slow = ClockJump(world.nebius, clock, TTL_SECONDS + 30)
    db = tmp_path / "i.sqlite3"
    service = open_service(
        db, world, providers=(slow, world.local), clock=clock, register=(GENERAL_FACILITY,)
    )
    report = service.purchase(job(1), agent_signer)
    assert isinstance(report, PurchaseReport) and report.state is S.LATE
    op = service.journal_entry(report.operation_id)
    rid = ReservationId(str(op.reservation_id))
    assert service.accounting.release_reason(rid) == "expired"
    assert service.accounting.exposure(FID, clock()).drawn.value == 0
    assert count(db, "SELECT COUNT(*) FROM accounting_settlements") == 0
    (late,) = service.accounting.late_delivery_disputes()
    receipt = op.receipt()
    assert receipt is not None
    assert late.receipt_digest == receipt.digest()
    assert late.evidence.receipt == receipt.canonical_bytes()
    assert op.response is not None and database_contains(db, op.response)  # retained, not released
    with pytest.raises(ResponseUnavailable):
        retrieve(service, report.operation_id)
    assert slow.calls == 1


def test_invalid_release_attempts_through_core_are_refused_after_delivery_began(
    tmp_path: Path,
) -> None:
    """A delivery attempt exists, so a claim of non-delivery cannot release the hold."""
    world = market_world()
    db = tmp_path / "i.sqlite3"
    service = open_service(
        db, world, register=(GENERAL_FACILITY,), crash_hook=crash_at("after_provider_returned")
    )
    with pytest.raises(SimulatedCrash):
        service.purchase(job(1), agent_signer)
    service.close()
    service = open_service(db, world)
    op = service.journal_entry(operation_id_for("job-facility-market-general-1"))
    assert op.state is S.UNKNOWN
    request, quote = op.request(), op.quote()
    service_class, profile = op.service_class(), op.execution_profile()
    assert request and quote and service_class and profile
    ref = PurchaseReference(
        ReservationId(str(op.reservation_id)), request, quote, service_class, profile
    )
    with pytest.raises(SettlementRefused, match="DELIVERY_ATTEMPTED"):
        SettlementCoordinator(ReservationLedger(db), world.registry()).release_undelivered(
            ref, NoDeliveryReason.ABANDONED_BEFORE_EXECUTION, NOW.plus_seconds(30)
        )
    assert service.accounting.reservation(ref.reservation_id).status is ReservationStatus.HELD


# --- I1e: R3, the standalone gateway bypass (Inference finding I-2) -------------------------------


def test_r3_service_exposes_no_purchase_reserve_or_release_bypass(tmp_path: Path) -> None:
    service = open_service(tmp_path / "i.sqlite3", market_world(), register=(GENERAL_FACILITY,))
    public = {name for name in dir(service) if not name.startswith("_")}
    assert public == {
        "accounting",
        "acknowledge",
        "database",
        "close",
        "journal_entry",
        "operation",
        "pending_operations",
        "purchase",
        "recover",
        "register_facility",
        "retrieve",
    }
    view = {name for name in dir(service.accounting) if not name.startswith("_")}
    assert view == {
        "chain_head",
        "delivery_attempt",
        "dispute_evidence",
        "disputes",
        "exposure",
        "late_delivery_disputes",
        "no_delivery_releases",
        "receipt_settled",
        "release_reason",
        "reservation",
    }


def test_r3_standalone_gateway_on_the_accounted_database_is_refused(tmp_path: Path) -> None:
    """R3: a standalone gateway built on the integration's accounted ledger neither reserves nor
    calls a provider, so no delivered inference can be falsely released afterwards."""
    from inference_world import GATEWAY_KEY, StepClock
    from market_world import GENERAL, candidates

    from credence.inference.gateway import Gateway, GatewayRefusal, Refused

    world = market_world()
    db = tmp_path / "i.sqlite3"
    service = open_service(db, world, register=(GENERAL_FACILITY,))
    standalone = Gateway(
        ledger=ReservationLedger(db),
        registry=world.registry(),
        gateway_key=GATEWAY_KEY,
        providers=(world.nebius, world.local),
        clock=StepClock(),
    )
    outcome = standalone.purchase(candidates(GENERAL_FACILITY, GENERAL)[0])
    assert isinstance(outcome, Refused)
    assert outcome.refusal is GatewayRefusal.ACCOUNTED_FACILITY
    assert world.nebius_transport.posts == [] and world.local.calls == 0
    assert count(db, "SELECT COUNT(*) FROM reservations") == 0
    assert service.accounting.no_delivery_releases() == []
    # the supported path still works on the same database
    settled_report(service.purchase(job(1), agent_signer))
