"""Integration I1b: characterization of known gateway/coordinator integration defects.

Each test asserts the CURRENT behaviour of the merged code (Core C3B + Inference Checkpoint 3)
precisely, so it passes today and fails as soon as that behaviour changes. They are not fixes.
I1c must change the behaviour and replace each test with one that asserts the approved target,
quoted in the docstring as "I1c target".

IDs follow the I0 audit: P1-P6, plus late delivery, chain divergence, gateway restart and
concurrent purchases. Offline: fake Nebius transport and fake signing provider; no network,
model or XRPL. Amounts are integer micro-tRLUSD (tRLUSD is a test token).
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from i1_world import (
    AFTER_EXPIRY,
    CONTROLLED,
    CONTROLLED_FACILITY,
    GENERAL,
    GENERAL_FACILITY,
    NOW,
    SETTLE_AT,
    LockedClock,
    RendezvousChain,
    RendezvousProvider,
    market_world,
    nebius_reply,
    rig,
    submission,
    verified_submission,
)
from market_world import NEBIUS_QUOTE, candidates

from credence.core.billing import max_total_cost_value
from credence.core.primitives import MicroTrlusd, ReasonCode, ReservationStatus, Timestamp
from credence.core.reservation_ledger import ReservationStateError
from credence.core.settlement_coordinator import (
    SettlementCoordinator,
    SettlementOutcome,
    SettlementRefused,
)
from credence.inference.gateway import (
    DeliveryRejected,
    DeliveryVerified,
    GatewayOutcome,
    ProviderFailed,
    ProviderFailureKind,
)
from credence.inference.receipt_state import ReceiptChainError


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield


def _verified(outcome: GatewayOutcome | None) -> DeliveryVerified:
    assert isinstance(outcome, DeliveryVerified), outcome
    return outcome


# --- P1: stale facility accounting ---------------------------------------------------------------


def test_defect_p1_stale_facility_qualifies_in_dry_run_then_escapes_purchase(
    tmp_path: Path,
) -> None:
    """The market dry-run trusts the caller's Facility.drawn; Gateway.purchase then lets Core's
    STALE_FACILITY_ACCOUNTING escape as an uncaught exception.

    I1c target: authoritative accounting is refreshed before signing; a stale facility is a typed
    outcome, never an exception, and is not qualified by the dry-run."""
    r = rig(tmp_path, register=(GENERAL_FACILITY,))
    first = _verified(r.gateway.purchase_best(candidates(GENERAL_FACILITY, GENERAL, 1)).outcome)
    r.coordinator.settle(verified_submission(first), SETTLE_AT)
    assert r.coordinator.exposure(GENERAL_FACILITY.facility_id, SETTLE_AT).drawn.value > 0

    stale = candidates(GENERAL_FACILITY, GENERAL, 2)  # still presents drawn = 0
    assert stale[0].facility.drawn_micro_trlusd.value == 0
    selection = r.gateway.select(stale)
    assert selection.chosen is not None and selection.qualifying_count == 2

    posts_before = len(r.world.nebius_transport.posts)
    with pytest.raises(ReservationStateError) as raised:
        r.gateway.purchase_best(stale)
    assert raised.value.reason == "STALE_FACILITY_ACCOUNTING"
    assert len(r.world.nebius_transport.posts) == posts_before  # refused before the provider


# --- P2: gateway verification is treated as settlement --------------------------------------------


def test_defect_p2_gateway_verification_advances_chain_and_releases_response_unsettled(
    tmp_path: Path,
) -> None:
    """A gateway-side verifier accept moves the gateway's chain head and hands back the response,
    while Core has settled nothing.

    I1c target: Core's persisted accounting-settled chain is authoritative; gateway verification
    alone never advances it; the agent receives the response only after accounting settlement."""
    r = rig(tmp_path, register=(GENERAL_FACILITY,))
    fid = GENERAL_FACILITY.facility_id
    out = _verified(r.gateway.purchase_best(r.refreshed(GENERAL_FACILITY, 1, GENERAL)).outcome)

    assert out.response  # released to the caller
    assert r.gateway.chain.head(fid) == out.evidence.receipt.digest()
    assert r.coordinator.chain_head(fid) is None
    assert not r.coordinator.receipt_settled(out.evidence.receipt.digest())
    assert r.ledger.get(out.reservation.reservation_id).status is ReservationStatus.HELD
    assert r.coordinator.exposure(fid, SETTLE_AT).drawn.value == 0


def test_defect_p2_chain_broken_releases_hold_and_keeps_no_delivery_evidence(
    tmp_path: Path,
) -> None:
    """Job 1 is verified at the gateway but never settled. Job 2 chains to job 1 at the gateway,
    so Core rejects it CHAIN_BROKEN: a terminal failure that releases the hold. The provider
    delivered, the response was handed back, the provider is not paid, and Core keeps only the
    receipt digest: the signed receipt and attestation are stored nowhere.

    I1c target: CHAIN_BROKEN alone must not discard evidence of delivered inference."""
    r = rig(tmp_path, register=(CONTROLLED_FACILITY,))
    fid = CONTROLLED_FACILITY.facility_id
    job1 = _verified(
        r.gateway.purchase_best(r.refreshed(CONTROLLED_FACILITY, 1, CONTROLLED)).outcome
    )
    job2 = _verified(
        r.gateway.purchase_best(r.refreshed(CONTROLLED_FACILITY, 2, CONTROLLED)).outcome
    )
    assert job2.evidence.receipt.previous_receipt_digest == job1.evidence.receipt.digest()
    assert job2.response and r.world.local.calls == 2

    result = r.coordinator.settle(verified_submission(job2), SETTLE_AT)

    assert result.outcome is SettlementOutcome.DELIVERY_FAILED
    assert result.verifier_record.reason_code is ReasonCode.CHAIN_BROKEN
    assert result.reservation_status is ReservationStatus.RELEASED
    assert r.coordinator.exposure(fid, SETTLE_AT).drawn.value == 0
    (dispute,) = r.coordinator.disputes()
    kind, reservation_id, _, receipt_digest, _, reason, _ = dispute
    assert (kind, reason) == ("delivery_failure", "CHAIN_BROKEN")
    assert reservation_id == job2.reservation.reservation_id.value
    assert receipt_digest == job2.evidence.receipt.digest().value
    assert job2.evidence.provider_attestation is not None
    assert r.database_contains(job2.policy_decision.record.canonical_bytes())  # control
    assert not r.database_contains(job2.evidence.receipt.canonical_bytes())
    assert not r.database_contains(job2.evidence.provider_attestation.canonical_bytes())


# --- chain divergence -----------------------------------------------------------------------------


def test_defect_chain_divergence_persists_after_the_late_receipt_settles(tmp_path: Path) -> None:
    """Once the gateway and Core heads differ, settling the missing receipt does not reconcile
    them: the gateway's head is job 2, Core's is job 1, and job 3 fails CHAIN_BROKEN as well.

    I1c target: the gateway reads its predecessor from Core's persisted chain head."""
    r = rig(tmp_path, register=(GENERAL_FACILITY,))
    fid = GENERAL_FACILITY.facility_id
    jobs = []
    for job in (1, 2):
        out = r.gateway.purchase_best(r.refreshed(GENERAL_FACILITY, job, GENERAL)).outcome
        jobs.append(_verified(out))
    job1, job2 = jobs
    assert r.coordinator.settle(
        verified_submission(job2), SETTLE_AT
    ).verifier_record.reason_code is (ReasonCode.CHAIN_BROKEN)
    late = r.coordinator.settle(verified_submission(job1), SETTLE_AT)
    assert late.outcome is SettlementOutcome.ACCOUNTING_SETTLED

    assert r.coordinator.chain_head(fid) == job1.evidence.receipt.digest()
    assert r.gateway.chain.head(fid) == job2.evidence.receipt.digest()

    job3 = _verified(r.gateway.purchase_best(r.refreshed(GENERAL_FACILITY, 3, GENERAL)).outcome)
    third = r.coordinator.settle(verified_submission(job3), SETTLE_AT)
    assert third.verifier_record.reason_code is ReasonCode.CHAIN_BROKEN
    assert third.reservation_status is ReservationStatus.RELEASED


# --- gateway restart ------------------------------------------------------------------------------


def test_defect_gateway_restart_forgets_the_chain(tmp_path: Path) -> None:
    """A new gateway process starts with an empty in-memory chain. Its own verifier accepts a
    receipt with a null predecessor, which Core rejects CHAIN_BROKEN after one settlement.

    I1c target: single-process gateway with enforced process ownership and durable recovery;
    the predecessor comes from Core's persisted chain head."""
    r = rig(tmp_path, register=(GENERAL_FACILITY,))
    fid = GENERAL_FACILITY.facility_id
    job1 = _verified(r.gateway.purchase_best(r.refreshed(GENERAL_FACILITY, 1, GENERAL)).outcome)
    assert r.coordinator.settle(verified_submission(job1), SETTLE_AT).outcome is (
        SettlementOutcome.ACCOUNTING_SETTLED
    )

    restarted = r.new_gateway(start=Timestamp("2026-10-24T15:00:30Z"))
    assert restarted.chain.head(fid) is None
    job2 = _verified(restarted.purchase_best(r.refreshed(GENERAL_FACILITY, 2, GENERAL)).outcome)
    assert job2.evidence.receipt.previous_receipt_digest is None
    assert r.coordinator.chain_head(fid) == job1.evidence.receipt.digest()

    result = r.coordinator.settle(verified_submission(job2), SETTLE_AT)
    assert result.outcome is SettlementOutcome.DELIVERY_FAILED
    assert result.verifier_record.reason_code is ReasonCode.CHAIN_BROKEN


def test_defect_two_gateway_processes_can_serve_one_facility(tmp_path: Path) -> None:
    """Nothing stops a second gateway from serving the same facility and database concurrently.

    I1c target: enforced process ownership (one gateway process per database)."""
    r = rig(tmp_path, register=(GENERAL_FACILITY,))
    second = r.new_gateway(start=Timestamp("2026-10-24T15:00:30Z"))
    a = r.gateway.purchase_best(r.refreshed(GENERAL_FACILITY, 1, GENERAL)).outcome
    b = second.purchase_best(r.refreshed(GENERAL_FACILITY, 2, GENERAL)).outcome
    assert isinstance(a, DeliveryVerified) and isinstance(b, DeliveryVerified)
    assert a.evidence.receipt.previous_receipt_digest is None
    assert b.evidence.receipt.previous_receipt_digest is None


# --- concurrent purchases -------------------------------------------------------------------------


def _run_concurrently(
    purchase: Callable[[int], GatewayOutcome | None], jobs: tuple[int, ...]
) -> dict[int, object]:
    """Run one purchase per job on its own thread; record each outcome or raised exception."""
    results: dict[int, object] = {}

    def run(job: int) -> None:
        try:
            results[job] = purchase(job)
        except Exception as exc:  # recorded for the assertion
            results[job] = exc

    threads = [threading.Thread(target=run, args=(job,)) for job in jobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return results


def test_defect_concurrent_purchases_gateway_and_core_disagree(tmp_path: Path) -> None:
    """Two purchases on one facility with overlapping provider calls both build on head None.
    The gateway accepts the first to advance and rejects the other CHAIN_BROKEN (withholding its
    response, hold left held). Core would accept whichever settles first: submitted first, the
    gateway-rejected delivery settles and the gateway-accepted one fails CHAIN_BROKEN.

    I1c target: one in-flight purchase per facility."""
    world = market_world()
    overlapping = RendezvousProvider(world.local, parties=2)
    r = rig(
        tmp_path,
        world=world,
        providers=(world.nebius, overlapping),
        clock=LockedClock(),
        register=(CONTROLLED_FACILITY,),
    )
    fid = CONTROLLED_FACILITY.facility_id
    results = _run_concurrently(
        lambda job: (
            r.gateway.purchase_best(candidates(CONTROLLED_FACILITY, CONTROLLED, job)).outcome
        ),
        (1, 2),
    )
    verified = [o for o in results.values() if isinstance(o, DeliveryVerified)]
    rejected = [o for o in results.values() if isinstance(o, DeliveryRejected)]
    assert len(verified) == 1 and len(rejected) == 1, results
    (accepted,), (refused,) = verified, rejected
    assert refused.decision.record.reason_code is ReasonCode.CHAIN_BROKEN
    assert accepted.evidence.receipt.previous_receipt_digest is None
    assert refused.evidence.receipt.previous_receipt_digest is None
    assert world.local.calls == 2
    max_cost = max_total_cost_value(accepted.evidence.quote)
    assert r.coordinator.exposure(fid, SETTLE_AT).held.value == 2 * max_cost

    first = r.coordinator.settle(
        submission(refused.reservation, refused.evidence, refused.evidence.receipt.response_digest),
        SETTLE_AT,
    )
    assert first.outcome is SettlementOutcome.ACCOUNTING_SETTLED
    second = r.coordinator.settle(verified_submission(accepted), SETTLE_AT)
    assert second.outcome is SettlementOutcome.DELIVERY_FAILED
    assert second.verifier_record.reason_code is ReasonCode.CHAIN_BROKEN


def test_defect_concurrent_purchases_can_raise_receipt_chain_error(tmp_path: Path) -> None:
    """When both purchases verify before either advances the head, the second advance raises
    ReceiptChainError out of Gateway.purchase after the provider delivered; its hold stays held.

    I1c target: one in-flight purchase per facility; no uncaught exception after delivery."""
    world = market_world()
    r = rig(
        tmp_path,
        world=world,
        clock=LockedClock(),
        chain=RendezvousChain(parties=2),
        register=(CONTROLLED_FACILITY,),
    )
    fid = CONTROLLED_FACILITY.facility_id
    results = _run_concurrently(
        lambda job: (
            r.gateway.purchase_best(candidates(CONTROLLED_FACILITY, CONTROLLED, job)).outcome
        ),
        (1, 2),
    )
    verified = [o for o in results.values() if isinstance(o, DeliveryVerified)]
    errors = [o for o in results.values() if isinstance(o, ReceiptChainError)]
    assert len(verified) == 1 and len(errors) == 1, results
    assert world.local.calls == 2
    max_cost = max_total_cost_value(verified[0].evidence.quote)
    assert r.coordinator.exposure(fid, SETTLE_AT).held.value == 2 * max_cost


# --- P5: unregistered facility --------------------------------------------------------------------


def test_defect_p5_unregistered_facility_spends_past_its_limit(tmp_path: Path) -> None:
    """The gateway reserves through the bare ledger, so an unregistered facility is served. The
    coordinator refuses to settle it, but the C2 bare settle still works and never moves drawn,
    so settled spending passes the facility limit.

    I1c target: all facilities must be registered; reservation goes through the coordinator."""
    max_cost = max_total_cost_value(NEBIUS_QUOTE)
    reply = nebius_reply(NEBIUS_QUOTE.max_input_tokens, NEBIUS_QUOTE.max_output_tokens)
    r = rig(tmp_path, world=market_world(reply))
    limit = max_cost + max_cost // 2  # room for one maximum-cost purchase, not two
    small = replace(GENERAL_FACILITY, limit_micro_trlusd=MicroTrlusd(limit))

    settled_total = 0
    for job in (1, 2):
        out = _verified(r.gateway.purchase_best(candidates(small, GENERAL, job)).outcome)
        assert out.evidence.receipt.price_micro_trlusd.value == max_cost
        with pytest.raises(SettlementRefused) as refused:
            r.coordinator.settle(verified_submission(out), SETTLE_AT)
        assert refused.value.reason == "UNACCOUNTED_FACILITY"
        r.ledger.settle(out.reservation.reservation_id, SETTLE_AT)
        settled_total += max_cost

    assert settled_total == 2 * max_cost > limit
    with pytest.raises(SettlementRefused):
        r.coordinator.exposure(small.facility_id, SETTLE_AT)  # no accounting exists


# --- P6: provider failure -------------------------------------------------------------------------


def test_defect_p6_provider_failure_has_no_auditable_release(tmp_path: Path) -> None:
    """A provider failure leaves the hold held. The coordinator has no release API; the bare
    ledger release works on a registered facility and leaves no audit record.

    I1c target: an authenticated terminal no-delivery failure is released through an auditable
    mechanism."""
    world = market_world()
    r = rig(tmp_path, world=world, providers=(world.nebius,), register=(CONTROLLED_FACILITY,))
    fid = CONTROLLED_FACILITY.facility_id
    out = r.gateway.purchase_best(r.refreshed(CONTROLLED_FACILITY, 1, CONTROLLED)).outcome
    assert isinstance(out, ProviderFailed)
    assert out.kind is ProviderFailureKind.PROVIDER_UNAVAILABLE
    held = r.coordinator.exposure(fid, SETTLE_AT).held.value
    assert held == out.reservation.maximum_amount_micro_trlusd.value > 0
    assert not hasattr(SettlementCoordinator, "release")

    released = r.ledger.release(out.reservation.reservation_id, SETTLE_AT)
    assert released.status is ReservationStatus.RELEASED
    assert r.ledger.release_reason(out.reservation.reservation_id) == "released"
    assert r.coordinator.exposure(fid, SETTLE_AT).held.value == 0
    assert r.coordinator.disputes() == []


# --- late delivery --------------------------------------------------------------------------------


def test_defect_late_delivery_is_refused_without_a_dispute_or_evidence(tmp_path: Path) -> None:
    """An authenticated delivery settled at or after expires_at is refused EXPIRED and the hold
    is released, but no dispute row is written and the delivery evidence is not kept.

    I1c target: authenticated late delivery creates a dispute record, without automatic
    settlement."""
    r = rig(tmp_path, register=(CONTROLLED_FACILITY,))
    fid = CONTROLLED_FACILITY.facility_id
    out = _verified(
        r.gateway.purchase_best(r.refreshed(CONTROLLED_FACILITY, 1, CONTROLLED)).outcome
    )
    assert out.reservation.expires_at.value <= AFTER_EXPIRY.value
    assert out.reservation.created_at.value >= NOW.value

    with pytest.raises(SettlementRefused) as refused:
        r.coordinator.settle(verified_submission(out), AFTER_EXPIRY)
    assert refused.value.reason == "EXPIRED"
    assert r.ledger.release_reason(out.reservation.reservation_id) == "expired"
    assert r.coordinator.disputes() == []
    assert r.coordinator.exposure(fid, AFTER_EXPIRY).drawn.value == 0
    assert r.database_contains(out.policy_decision.record.canonical_bytes())  # control
    assert not r.database_contains(out.evidence.receipt.canonical_bytes())
    with pytest.raises(SettlementRefused) as again:
        r.coordinator.settle(verified_submission(out), AFTER_EXPIRY)
    assert again.value.reason == "NOT_HELD"
