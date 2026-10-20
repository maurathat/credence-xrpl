"""Integration I1b: successful inference-to-accounting paths, gateway to coordinator.

Scenario A (general facility, Tier 1 floor): both providers qualify; Nebius wins on price; its
Tier 1 delivery settles in Core accounting.
Scenario B (controlled facility, Q8_0 and Tier 2 floor): Core rejects Nebius; the local provider
wins; its Tier 2 delivery settles in Core accounting.

Two jobs per scenario, so the second receipt chains to the first. Offline: fake Nebius transport
and fake signing provider; no network, model or XRPL. Accounting settlement only: the drawn
amount is Credence's record of verified spending, not XRPL loan principal.

These pass on the merged code because the caller here does what I1c will make the glue do:
register the facility, present the stored drawn amount, and submit each delivery to the
coordinator in order. Known defects are in test_i1_known_defects.py.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from pathlib import Path

import pytest
from i1_world import (
    CONTROLLED,
    CONTROLLED_FACILITY,
    GENERAL,
    GENERAL_FACILITY,
    SETTLE_AT,
    WriteLockProbe,
    market_world,
    rig,
    verified_submission,
)

from credence.core.billing import max_total_cost_value
from credence.core.primitives import EvidenceTier, ReasonCode, ReservationStatus
from credence.core.settlement_coordinator import SettlementOutcome
from credence.inference.gateway import DeliveryVerified
from credence.inference.local_llama import LOCAL_QWEN_PROVIDER_ID
from credence.inference.nebius import NEBIUS_PROVIDER_ID


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield


def test_scenario_a_general_nebius_tier1_settles_in_accounting(tmp_path: Path) -> None:
    r = rig(tmp_path, register=(GENERAL_FACILITY,))
    fid = GENERAL_FACILITY.facility_id
    drawn = 0
    previous = None
    for job in (1, 2):
        result = r.gateway.purchase_best(r.refreshed(GENERAL_FACILITY, job, GENERAL))
        nebius, local = result.selection.qualifications
        assert nebius.qualified and local.qualified
        assert nebius.max_total_cost < local.max_total_cost
        out = result.outcome
        assert isinstance(out, DeliveryVerified)
        receipt = out.evidence.receipt
        assert receipt.provider_id == NEBIUS_PROVIDER_ID
        assert receipt.evidence_tier is EvidenceTier.WITNESSED
        assert out.evidence.provider_attestation is None
        assert receipt.previous_receipt_digest == previous == r.coordinator.chain_head(fid)

        settled = r.coordinator.settle(verified_submission(out), SETTLE_AT)

        assert settled.outcome is SettlementOutcome.ACCOUNTING_SETTLED
        assert settled.reservation_status is ReservationStatus.SETTLED
        assert settled.actual_amount == receipt.price_micro_trlusd
        assert settled.actual_amount.value <= max_total_cost_value(out.evidence.quote)
        drawn += receipt.price_micro_trlusd.value
        exposure = r.coordinator.exposure(fid, SETTLE_AT)
        assert (exposure.drawn.value, exposure.held.value) == (drawn, 0)
        assert r.coordinator.receipt_settled(receipt.digest())
        assert r.coordinator.chain_head(fid) == receipt.digest() == r.gateway.chain.head(fid)
        previous = receipt.digest()
    assert len(r.world.nebius_transport.posts) == 2 and r.world.local.calls == 0
    assert r.coordinator.disputes() == []


def test_scenario_b_controlled_local_tier2_settles_in_accounting(tmp_path: Path) -> None:
    r = rig(tmp_path, register=(CONTROLLED_FACILITY,))
    fid = CONTROLLED_FACILITY.facility_id
    drawn = 0
    previous = None
    for job in (1, 2):
        result = r.gateway.purchase_best(r.refreshed(CONTROLLED_FACILITY, job, CONTROLLED))
        nebius, local = result.selection.qualifications
        assert not nebius.qualified and nebius.reason_code is ReasonCode.PROFILE_NOT_ALLOWED
        assert local.qualified and nebius.max_total_cost < local.max_total_cost
        out = result.outcome
        assert isinstance(out, DeliveryVerified)
        receipt = out.evidence.receipt
        assert receipt.provider_id == LOCAL_QWEN_PROVIDER_ID
        assert receipt.evidence_tier is EvidenceTier.BOUND_AND_SIGNED
        assert out.evidence.provider_attestation is not None
        assert out.evidence.provider_signature is not None
        assert receipt.previous_receipt_digest == previous == r.coordinator.chain_head(fid)

        settled = r.coordinator.settle(verified_submission(out), SETTLE_AT)

        assert settled.outcome is SettlementOutcome.ACCOUNTING_SETTLED
        drawn += receipt.price_micro_trlusd.value
        exposure = r.coordinator.exposure(fid, SETTLE_AT)
        assert (exposure.drawn.value, exposure.held.value) == (drawn, 0)
        assert r.coordinator.chain_head(fid) == receipt.digest() == r.gateway.chain.head(fid)
        previous = receipt.digest()
    assert r.world.nebius_transport.posts == [] and r.world.local.calls == 2
    assert r.coordinator.disputes() == []


def test_no_sqlite_write_transaction_is_held_across_the_provider_call(tmp_path: Path) -> None:
    """Invariant to keep through I1c: reserve commits before the provider is called."""
    world = market_world()
    probe = WriteLockProbe(world.local, tmp_path / "reservations.sqlite3")
    r = rig(tmp_path, world=world, providers=(world.nebius, probe), register=(CONTROLLED_FACILITY,))
    assert r.db == tmp_path / "reservations.sqlite3"
    out = r.gateway.purchase_best(r.refreshed(CONTROLLED_FACILITY, 1, CONTROLLED)).outcome
    assert isinstance(out, DeliveryVerified)
    assert probe.write_lock_free_during_execute == [True]
