"""Integration I1c: the integrated purchase lifecycle, end to end through IntegrationService.

Scenario A (general facility, Tier 1 floor): both providers qualify; Nebius wins on price; its
Tier 1 delivery settles in Core accounting.
Scenario B (controlled facility, Q8_0 and Tier 2 floor): Core rejects Nebius; the local provider
wins; its Tier 2 delivery settles in Core accounting.

Also: repeated sequential purchases, response suppression before settlement, settlement
atomicity, and no SQLite write lock during provider inference. Offline: fake Nebius transport
and fake signing provider; no network, model or XRPL. Accounting settlement only: the drawn
amount is Credence's record of verified spending, not XRPL loan principal.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from pathlib import Path

import pytest
from i1_world import (
    AGENT_KEY_ID,
    CONTROLLED,
    CONTROLLED_FACILITY,
    GENERAL,
    GENERAL_FACILITY,
    NOW,
    SimulatedCrash,
    WriteLockProbe,
    agent_signer,
    count,
    crash_at,
    job,
    market_world,
    open_service,
)

from credence.core.billing import max_total_cost_value
from credence.core.objects import DeliveryReceiptBody, Facility, ProcurementClass
from credence.core.primitives import (
    EvidenceTier,
    ProviderId,
    ReservationId,
    ReservationStatus,
    Sha256Digest,
)
from credence.core.settlement_coordinator import FAULT_POINTS
from credence.inference.local_llama import LOCAL_QWEN_PROVIDER_ID
from credence.inference.nebius import NEBIUS_PROVIDER_ID
from credence.integration.journal import OperationState as S
from credence.integration.service import (
    IntegrationService,
    PurchaseReport,
    ResponseUnavailable,
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield


def settled(service: IntegrationService, report: object) -> tuple[PurchaseReport, bytes]:
    assert isinstance(report, PurchaseReport), report
    assert report.state is S.SETTLED, report
    response = service.retrieve(report.operation_id, AGENT_KEY_ID)
    service.acknowledge(report.operation_id, AGENT_KEY_ID)
    return report, response


def receipt_of(service: IntegrationService, report: PurchaseReport) -> DeliveryReceiptBody:
    receipt = service.journal_entry(report.operation_id).receipt()
    assert receipt is not None
    return receipt


@pytest.mark.parametrize(
    ("scenario", "facility", "procurement", "provider", "tier"),
    [
        ("A", GENERAL_FACILITY, GENERAL, NEBIUS_PROVIDER_ID, EvidenceTier.WITNESSED),
        (
            "B",
            CONTROLLED_FACILITY,
            CONTROLLED,
            LOCAL_QWEN_PROVIDER_ID,
            EvidenceTier.BOUND_AND_SIGNED,
        ),
    ],
)
def test_scenarios_settle_in_accounting_end_to_end(
    tmp_path: Path,
    scenario: str,
    facility: Facility,
    procurement: ProcurementClass,
    provider: ProviderId,
    tier: EvidenceTier,
) -> None:
    world = market_world()
    db = tmp_path / "integration.sqlite3"
    service = open_service(db, world, register=(facility,))
    fid = facility.facility_id
    drawn = 0
    previous: Sha256Digest | None = None
    for index in (1, 2):
        j = job(index, facility, procurement)
        report, response = settled(service, service.purchase(j, agent_signer))
        receipt = receipt_of(service, report)
        assert report.provider_id == provider.value
        assert receipt.evidence_tier is tier
        assert receipt.previous_receipt_digest == previous
        assert response  # exact bytes were held until retrieval
        drawn += receipt.price_micro_trlusd.value
        exposure = service.coordinator.exposure(fid, receipt.completed_at)
        assert (exposure.drawn.value, exposure.held.value) == (drawn, 0)
        assert service.coordinator.chain_head(fid) == receipt.digest()
        assert service.gateway.chain.head(fid) is None  # the gateway's own chain is never used
        previous = receipt.digest()
    assert service.coordinator.disputes() == []
    if scenario == "A":
        assert len(world.nebius_transport.posts) == 2 and world.local.calls == 0
    else:
        assert world.nebius_transport.posts == [] and world.local.calls == 2
        nebius_offer = max_total_cost_value(j.offers[0].quote)
        assert nebius_offer < max_total_cost_value(j.offers[1].quote)  # cheaper, still rejected


def test_repeated_sequential_purchases_chain_and_account_exactly(tmp_path: Path) -> None:
    service = open_service(tmp_path / "i.sqlite3", market_world(), register=(GENERAL_FACILITY,))
    fid = GENERAL_FACILITY.facility_id
    receipts = []
    for index in range(1, 9):
        report, _ = settled(service, service.purchase(job(index), agent_signer))
        receipts.append(receipt_of(service, report))
    assert [r.previous_receipt_digest for r in receipts] == [None] + [
        r.digest() for r in receipts[:-1]
    ]
    exposure = service.coordinator.exposure(fid, receipts[-1].completed_at)
    assert exposure.drawn.value == sum(r.price_micro_trlusd.value for r in receipts)
    assert exposure.held.value == 0
    assert count(tmp_path / "i.sqlite3", "SELECT COUNT(*) FROM accounting_settlements") == 8


def test_no_sqlite_write_lock_is_held_during_provider_inference(tmp_path: Path) -> None:
    world = market_world()
    db = tmp_path / "i.sqlite3"
    probe = WriteLockProbe(world.local, db)
    service = open_service(
        db, world, providers=(world.nebius, probe), register=(CONTROLLED_FACILITY,)
    )
    for index in (1, 2):
        settled(
            service, service.purchase(job(index, CONTROLLED_FACILITY, CONTROLLED), agent_signer)
        )
    assert probe.write_lock_free_during_execute == [True, True]


def test_response_is_suppressed_until_accounting_settlement(tmp_path: Path) -> None:
    """Evidence and the exact response are stored, Core has not settled: nothing is released."""
    world = market_world()
    db = tmp_path / "i.sqlite3"
    service = open_service(
        db, world, register=(GENERAL_FACILITY,), crash_hook=crash_at("after_evidence_stored")
    )
    with pytest.raises(SimulatedCrash):
        service.purchase(job(1), agent_signer)
    service.close()

    reopened = open_service(db, world, recover=False)
    (op,) = [reopened.journal_entry(o.operation_id) for o in reopened.pending_operations()]
    assert op.state is S.DELIVERED and op.response is not None
    assert not reopened.coordinator.receipt_settled(Sha256Digest(str(op.receipt_digest)))
    with pytest.raises(ResponseUnavailable):
        reopened.retrieve(op.operation_id, AGENT_KEY_ID)
    # recovery settles from the durable evidence; only then is the response released
    reopened.recover()
    assert reopened.operation(op.operation_id).state is S.SETTLED
    assert reopened.retrieve(op.operation_id, AGENT_KEY_ID) == op.response


@pytest.mark.parametrize("point", FAULT_POINTS)
def test_settlement_is_atomic_and_settles_once_after_recovery(tmp_path: Path, point: str) -> None:
    world = market_world()
    db = tmp_path / "i.sqlite3"

    def fault(reached: str) -> None:
        if reached == point:
            raise RuntimeError(f"injected: {point}")

    service = open_service(db, world, register=(GENERAL_FACILITY,), coordinator_fault=fault)
    with pytest.raises(RuntimeError, match="injected"):
        service.purchase(job(1), agent_signer)
    (op,) = service.pending_operations()
    assert op.state is S.DELIVERED
    rid = ReservationId(str(op.reservation_id))
    assert service.ledger.get(rid).status is ReservationStatus.HELD
    assert service.coordinator.exposure(GENERAL_FACILITY.facility_id, NOW).drawn.value == 0
    assert service.coordinator.chain_head(GENERAL_FACILITY.facility_id) is None
    service.close()

    reopened = open_service(db, world)  # recovery resubmits the stored evidence
    assert reopened.operation(op.operation_id).state is S.SETTLED
    assert count(db, "SELECT COUNT(*) FROM accounting_settlements") == 1
    receipt = reopened.journal_entry(op.operation_id).receipt()
    assert receipt is not None
    exposure = reopened.coordinator.exposure(GENERAL_FACILITY.facility_id, receipt.completed_at)
    assert exposure.drawn == receipt.price_micro_trlusd
    assert world.nebius_transport.posts and len(world.nebius_transport.posts) == 1
