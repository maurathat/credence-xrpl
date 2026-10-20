"""Gateway.deliver, connected and check_prompt (Integration I1c, contracts/requests/0002).

`deliver` runs the provider for a reservation Core already made, under Core's recorded delivery
attempt, chains the receipt to the predecessor it is given, and runs no verifier and moves no
receipt chain. Offline: fake Nebius transport and fake signing provider; no network, model or XRPL.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from inference_world import GATEWAY_KEY, NOW, StepClock
from market_world import (
    CONTROLLED,
    CONTROLLED_FACILITY,
    GENERAL,
    GENERAL_FACILITY,
    MarketWorld,
    candidates,
    market_world,
)

from credence.core.primitives import EvidenceTier, Sha256Digest
from credence.core.reservation_ledger import ReservationLedger, ReserveResult
from credence.core.settlement_coordinator import (
    DeliveryAttempt,
    PurchaseReference,
    SettlementCoordinator,
    SignedPurchase,
)
from credence.inference.gateway import (
    DeliveryCaptured,
    Gateway,
    GatewayRefusal,
    ProviderFailed,
    ProviderFailureKind,
    Purchase,
)
from credence.inference.payload import payload_digest
from credence.inference.receipt_state import ReceiptChain

HEAD = Sha256Digest.from_hex("ab" * 32)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield


class Rig:
    def __init__(self, tmp_path: Path, world: MarketWorld, *providers: object) -> None:
        ledger = ReservationLedger(tmp_path / "db.sqlite3")
        self.coordinator = SettlementCoordinator(ledger, world.registry())
        self.chain = ReceiptChain()
        self.gateway = Gateway(
            ledger=ledger,
            registry=world.registry(),
            gateway_key=GATEWAY_KEY,
            providers=providers or (world.nebius, world.local),  # type: ignore[arg-type]
            clock=StepClock(),
            chain=self.chain,
        )

    def reserve(self, purchase: Purchase) -> tuple[ReserveResult, DeliveryAttempt]:
        reserved = self.coordinator.reserve(
            SignedPurchase(
                purchase.request,
                purchase.request_signature,
                purchase.facility,
                purchase.quote,
                purchase.service_class,
                purchase.execution_profile,
                purchase.procurement_classes,
            ),
            NOW,
        )
        assert reserved.reservation is not None
        attempt = self.coordinator.begin_delivery(
            PurchaseReference(
                reserved.reservation.reservation_id,
                purchase.request,
                purchase.quote,
                purchase.service_class,
                purchase.execution_profile,
            ),
            NOW.plus_seconds(1),
        )
        return reserved, attempt


def test_deliver_builds_signed_evidence_on_the_given_predecessor(tmp_path: Path) -> None:
    world = market_world()
    rig = Rig(tmp_path, world)
    rig.coordinator.register_facility(CONTROLLED_FACILITY)
    local = candidates(CONTROLLED_FACILITY, CONTROLLED)[1]
    reserved, attempt = rig.reserve(local)
    outcome = rig.gateway.deliver(local, reserved, attempt, previous_receipt_digest=HEAD)
    assert isinstance(outcome, DeliveryCaptured)
    receipt = outcome.evidence.receipt
    assert receipt.previous_receipt_digest == HEAD
    assert receipt.evidence_tier is EvidenceTier.BOUND_AND_SIGNED
    assert receipt.response_digest == payload_digest(outcome.response)
    assert outcome.evidence.provider_signature is not None
    assert outcome.reservation == reserved.reservation
    assert rig.chain.head(CONTROLLED_FACILITY.facility_id) is None  # never moved
    assert world.local.calls == 1


def test_deliver_never_runs_the_verifier(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import credence.inference.gateway as gateway_module

    def boom(*args: object) -> None:
        raise AssertionError("deliver must not verify")

    monkeypatch.setattr(gateway_module, "verify", boom)
    world = market_world()
    rig = Rig(tmp_path, world)
    rig.coordinator.register_facility(GENERAL_FACILITY)
    nebius = candidates(GENERAL_FACILITY, GENERAL)[0]
    reserved, attempt = rig.reserve(nebius)
    outcome = rig.gateway.deliver(nebius, reserved, attempt, previous_receipt_digest=None)
    assert isinstance(outcome, DeliveryCaptured)
    assert outcome.evidence.receipt.evidence_tier is EvidenceTier.WITNESSED


def test_deliver_requires_cores_attempt_for_this_reservation(tmp_path: Path) -> None:
    world = market_world()
    rig = Rig(tmp_path, world)
    rig.coordinator.register_facility(GENERAL_FACILITY)
    first, second = (
        candidates(GENERAL_FACILITY, GENERAL, 1)[0],
        candidates(GENERAL_FACILITY, GENERAL, 2)[0],
    )
    reserved_1, attempt_1 = rig.reserve(first)
    reserved_2, _ = rig.reserve(second)
    with pytest.raises(ValueError, match="delivery attempt"):
        rig.gateway.deliver(first, reserved_2, attempt_1, previous_receipt_digest=None)
    with pytest.raises(ValueError, match="delivery attempt"):
        rig.gateway.deliver(second, reserved_1, attempt_1, previous_receipt_digest=None)
    assert len(world.nebius_transport.posts) == 0  # refused before any provider call


def test_deliver_reports_provider_failure(tmp_path: Path) -> None:
    world = market_world()
    rig = Rig(tmp_path, world, world.nebius)  # local not connected
    rig.coordinator.register_facility(CONTROLLED_FACILITY)
    local = candidates(CONTROLLED_FACILITY, CONTROLLED)[1]
    reserved, attempt = rig.reserve(local)
    outcome = rig.gateway.deliver(local, reserved, attempt, previous_receipt_digest=None)
    assert isinstance(outcome, ProviderFailed)
    assert outcome.kind is ProviderFailureKind.PROVIDER_UNAVAILABLE


def test_connected_and_check_prompt(tmp_path: Path) -> None:
    world = market_world()
    rig = Rig(tmp_path, world, world.nebius)
    assert rig.gateway.connected(world.nebius.provider_id)
    assert not rig.gateway.connected(world.local.provider_id)
    nebius = candidates(GENERAL_FACILITY, GENERAL)[0]
    assert rig.gateway.check_prompt(nebius) is None
    refused = rig.gateway.check_prompt(replace(nebius, prompt=b"another prompt"))
    assert refused is not None and refused.refusal is GatewayRefusal.PROMPT_DIGEST_MISMATCH
    refused = rig.gateway.check_prompt(replace(nebius, prompt=b"\xff"))
    assert refused is not None and refused.refusal is GatewayRefusal.PROMPT_NOT_UTF8
