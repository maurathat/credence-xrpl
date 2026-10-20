"""Integration I1g, Core finding B-1: a facility used through the standalone path cannot be adopted.

B1-B: a standalone purchase on an unregistered facility (one mocked provider call, no Core delivery
attempt) cannot be registered, so no accounted `release_undelivered` can be aimed at that delivery.
B1-C: the same history cannot reach begin_delivery or settlement through registration.
Offline: fake Nebius transport and fake signing provider; no network, model or XRPL.
"""

from __future__ import annotations

import socket
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from i1_world import GENERAL, GENERAL_FACILITY, market_world
from inference_world import GATEWAY_KEY, NOW, StepClock
from market_world import MarketWorld, candidates

from credence.core.reservation_ledger import ReservationLedger
from credence.core.settlement_coordinator import (
    DeliverySubmission,
    NoDeliveryReason,
    PurchaseReference,
    SettlementCoordinator,
    SettlementRefused,
)
from credence.inference.gateway import DeliveryVerified, Gateway
from credence.inference.payload import payload_digest


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield


def snapshot(db: Path) -> dict[str, list[tuple[object, ...]]]:
    with sqlite3.connect(db) as conn:
        names = [
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        ]
        return {n: sorted(conn.execute(f'SELECT * FROM "{n}"').fetchall()) for n in names}


def standalone_delivery(
    tmp_path: Path,
) -> tuple[Path, MarketWorld, SettlementCoordinator, DeliveryVerified]:
    """A standalone purchase on an unregistered facility: one provider call, no Core attempt."""
    world = market_world()
    db = tmp_path / "db.sqlite3"
    ledger = ReservationLedger(db)
    coordinator = SettlementCoordinator(ledger, world.registry())
    gateway = Gateway(
        ledger=ledger,
        registry=world.registry(),
        gateway_key=GATEWAY_KEY,
        providers=(world.nebius, world.local),
        clock=StepClock(),
    )
    outcome = gateway.purchase(candidates(GENERAL_FACILITY, GENERAL)[0])
    assert isinstance(outcome, DeliveryVerified)
    assert len(world.nebius_transport.posts) == 1
    assert coordinator.delivery_attempt(outcome.reservation.reservation_id) is None
    return db, world, coordinator, outcome


def reference(out: DeliveryVerified) -> PurchaseReference:
    ev = out.evidence
    return PurchaseReference(
        out.reservation.reservation_id, ev.request, ev.quote, ev.service_class, ev.execution_profile
    )


def refused(call: object) -> str:
    with pytest.raises(SettlementRefused) as exc:
        call()  # type: ignore[operator]
    return exc.value.reason


def test_b1b_standalone_history_cannot_be_registered_or_falsely_released(tmp_path: Path) -> None:
    db, world, coordinator, out = standalone_delivery(tmp_path)
    before = snapshot(db)
    assert (
        refused(lambda: coordinator.register_facility(GENERAL_FACILITY)) == "FACILITY_HAS_HISTORY"
    )
    assert snapshot(db) == before  # no accounted state, no change to the delivery's history
    reason = refused(
        lambda: coordinator.release_undelivered(
            reference(out), NoDeliveryReason.ABANDONED_BEFORE_EXECUTION, NOW.plus_seconds(30)
        )
    )
    assert reason == "UNACCOUNTED_FACILITY"  # no accounted no-delivery path exists
    assert snapshot(db) == before
    assert len(world.nebius_transport.posts) == 1  # the real execution record is unchanged


def test_b1c_standalone_receipt_cannot_enter_registered_settlement(tmp_path: Path) -> None:
    db, _, coordinator, out = standalone_delivery(tmp_path)
    before = snapshot(db)
    assert (
        refused(lambda: coordinator.register_facility(GENERAL_FACILITY)) == "FACILITY_HAS_HISTORY"
    )
    assert refused(lambda: coordinator.begin_delivery(reference(out), NOW.plus_seconds(30))) == (
        "UNACCOUNTED_FACILITY"
    )
    ev = out.evidence
    submission = DeliverySubmission(
        reservation_id=out.reservation.reservation_id,
        receipt=ev.receipt,
        gateway_signature=ev.gateway_signature,
        provider_attestation=ev.provider_attestation,
        provider_signature=ev.provider_signature,
        delivered_response_digest=payload_digest(out.response),
        request=ev.request,
        quote=ev.quote,
        service_class=ev.service_class,
        execution_profile=ev.execution_profile,
    )
    assert refused(lambda: coordinator.settle(submission, NOW.plus_seconds(31))) == (
        "UNACCOUNTED_FACILITY"
    )
    assert snapshot(db) == before
    assert coordinator.chain_head(GENERAL_FACILITY.facility_id) is None
    assert not coordinator.receipt_settled(ev.receipt.digest())
