"""Inference Checkpoint 3 offline: two-provider routing over Core-qualified quotes.

Scenario A (general): both providers qualify; the cheaper Nebius quote wins on price.
Scenario B (controlled): Nebius is still cheaper but Core rejects it; the local provider wins.
Only procurement requirements and quotes differ; no provider evidence is altered. No network.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from pathlib import Path

import pytest
from market_world import (
    CONTROLLED,
    CONTROLLED_FACILITY,
    GENERAL,
    GENERAL_FACILITY,
    LOCAL_PROFILE,
    LOCAL_QUOTE,
    NEBIUS_PROFILE,
    NEBIUS_QUOTE,
    candidates,
    market_world,
)

from credence.core.billing import max_total_cost_value
from credence.core.primitives import DecisionOutcome, EvidenceTier, ReasonCode
from credence.inference.gateway import DeliveryVerified
from credence.inference.local_llama import LOCAL_QWEN_PROVIDER_ID as LOCAL_PROVIDER_ID
from credence.inference.nebius import NEBIUS_PROVIDER_ID


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield


def test_demo_prices_make_nebius_cheaper() -> None:
    assert max_total_cost_value(NEBIUS_QUOTE) < max_total_cost_value(LOCAL_QUOTE)


def test_scenario_a_general_both_qualify_cheaper_wins(tmp_path: Path) -> None:
    world = market_world()
    gateway = world.gateway(tmp_path)
    result = gateway.purchase_best(candidates(GENERAL_FACILITY, GENERAL))

    selection = result.selection
    assert [q.qualified for q in selection.qualifications] == [True, True]
    assert selection.qualifying_count == 2
    assert selection.chosen is not None
    assert selection.chosen.purchase.request.provider_id == NEBIUS_PROVIDER_ID
    outcome = result.outcome
    assert isinstance(outcome, DeliveryVerified)
    assert outcome.evidence.receipt.evidence_tier is EvidenceTier.WITNESSED
    assert world.nebius_transport.posts and world.local.calls == 0


def test_scenario_b_controlled_nebius_rejected_local_wins(tmp_path: Path) -> None:
    world = market_world()
    gateway = world.gateway(tmp_path)
    result = gateway.purchase_best(candidates(CONTROLLED_FACILITY, CONTROLLED))

    nebius, local = result.selection.qualifications
    assert not nebius.qualified
    # Core precedence: the structural match fails first (NOT ASSERTED precision never satisfies
    # a constrained class), before the evidence-tier rule is reached.
    assert nebius.reason_code is ReasonCode.PROFILE_NOT_ALLOWED
    assert NEBIUS_PROFILE.precision is None and NEBIUS_PROFILE.quantization is None
    assert local.qualified
    assert nebius.max_total_cost < local.max_total_cost  # Nebius was cheaper and still lost
    assert result.selection.chosen is local
    outcome = result.outcome
    assert isinstance(outcome, DeliveryVerified)
    assert outcome.evidence.receipt.evidence_tier is EvidenceTier.BOUND_AND_SIGNED
    assert outcome.evidence.receipt.execution_profile_digest == LOCAL_PROFILE.digest()
    assert world.nebius_transport.posts == []  # the rejected provider was never called
    assert world.local.calls == 1


def test_nebius_under_controlled_class_with_a_tier1_floor_is_tier_too_low(tmp_path: Path) -> None:
    """If the precision constraint is lifted, Core's next rule (evidence tier) excludes Nebius."""
    from dataclasses import replace

    lifted = replace(CONTROLLED, allowed_precisions=None, allowed_quantizations=None)
    facility = replace(CONTROLLED_FACILITY, allowed_procurement_classes=(lifted.digest(),))
    selection = market_world().gateway(tmp_path).select(candidates(facility, lifted))
    nebius, local = selection.qualifications
    assert nebius.reason_code is ReasonCode.TIER_TOO_LOW
    assert local.qualified and selection.chosen is local


def test_no_candidate_qualifies_nothing_is_reserved_or_called(tmp_path: Path) -> None:
    from dataclasses import replace

    world = market_world()
    frozen = replace(GENERAL_FACILITY, status=GENERAL_FACILITY.status.__class__("frozen"))
    result = world.gateway(tmp_path).purchase_best(candidates(frozen, GENERAL))
    assert result.selection.chosen is None and result.outcome is None
    assert {q.reason_code for q in result.selection.qualifications} == {
        ReasonCode.FACILITY_NOT_ACTIVE
    }
    assert world.nebius_transport.posts == [] and world.local.calls == 0


def test_routing_never_overrides_a_core_rejection(tmp_path: Path) -> None:
    """Even as the only, cheapest candidate, a rejected quote is not chosen."""
    world = market_world()
    nebius_only = candidates(CONTROLLED_FACILITY, CONTROLLED)[:1]
    result = world.gateway(tmp_path).purchase_best(nebius_only)
    assert result.selection.chosen is None and result.outcome is None
    assert world.nebius_transport.posts == []


def test_tie_break_is_provider_id(tmp_path: Path) -> None:
    from dataclasses import replace

    from inference_world import Scenario, make_purchase, prompt
    from market_world import LOCAL_SERVICE, NEBIUS_SERVICE

    same_local = replace(
        LOCAL_QUOTE,
        input_price_per_million_micro_trlusd=NEBIUS_QUOTE.input_price_per_million_micro_trlusd,
        output_price_per_million_micro_trlusd=NEBIUS_QUOTE.output_price_per_million_micro_trlusd,
    )
    cands = [
        make_purchase(
            Scenario(GENERAL_FACILITY, NEBIUS_QUOTE, NEBIUS_SERVICE, NEBIUS_PROFILE, GENERAL),
            1,
            request_prompt=prompt(1),
        ),
        make_purchase(
            Scenario(GENERAL_FACILITY, same_local, LOCAL_SERVICE, LOCAL_PROFILE, GENERAL),
            2,
            request_prompt=prompt(1),
        ),
    ]
    selection = market_world().gateway(tmp_path).select(cands)
    assert selection.qualifying_count == 2
    assert selection.chosen is not None
    expected = min(LOCAL_PROVIDER_ID.value, NEBIUS_PROVIDER_ID.value)
    assert selection.chosen.purchase.request.provider_id.value == expected


def test_candidates_must_be_one_job(tmp_path: Path) -> None:
    a = candidates(GENERAL_FACILITY, GENERAL, 1)[0]
    b = candidates(GENERAL_FACILITY, GENERAL, 2)[1]
    with pytest.raises(ValueError, match="same prompt"):
        market_world().gateway(tmp_path).select([a, b])


def test_selection_is_a_dry_run(tmp_path: Path) -> None:
    from credence.core.reservation_ledger import ReservationLedger

    world = market_world()
    cands = candidates(GENERAL_FACILITY, GENERAL)
    selection = world.gateway(tmp_path).select(cands)
    assert all(
        q.decision.record.outcome is DecisionOutcome.ACCEPT for q in selection.qualifications
    )
    ledger = ReservationLedger(tmp_path / "reservations.sqlite3")
    for candidate in cands:
        assert not ledger.nonce_used(candidate.request.facility_id, candidate.request.nonce)


def test_service_locations_match_the_buyer_allowlist_under_frozen_core() -> None:
    """Nebius FI and local US each satisfy {FI, US} through Core's own matcher."""
    from market_world import (
        BUYER_LOCATIONS,
        LOCAL_SERVICE,
        NEBIUS_SERVICE,
    )

    from credence.core.procurement import structural_failures

    assert BUYER_LOCATIONS == tuple(sorted(BUYER_LOCATIONS))
    assert NEBIUS_SERVICE.jurisdiction is not None and NEBIUS_SERVICE.jurisdiction.value == "FI"
    assert [c.value for c in NEBIUS_SERVICE.data_residency] == ["FI"]
    assert LOCAL_SERVICE.jurisdiction is not None and LOCAL_SERVICE.jurisdiction.value == "US"
    assert [c.value for c in LOCAL_SERVICE.data_residency] == ["US"]
    for svc, profile, provider in (
        (NEBIUS_SERVICE, NEBIUS_PROFILE, NEBIUS_QUOTE.provider_id),
        (LOCAL_SERVICE, LOCAL_PROFILE, LOCAL_QUOTE.provider_id),
    ):
        failures = structural_failures(GENERAL, svc, profile, provider)
        assert failures == ()  # full structural match, locations included
