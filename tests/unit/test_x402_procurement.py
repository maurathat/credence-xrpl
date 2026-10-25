"""X402-P0: an x402 offer decided by Core's own policy engine beside Nebius and local Qwen.

Covers brief tests 5 (asset/issuer at the Core boundary), 6 (excessive price) and 20 (Nebius and
Qwen procurement preserved). Core is called through the unchanged `qualify_and_select`; no network.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from inference_world import AGENT_KEY, Scenario, make_purchase, prompt
from market_world import (
    CONTROLLED,
    CONTROLLED_FACILITY,
    GENERAL,
    GENERAL_FACILITY,
    MODEL_FAMILY,
    candidates,
    market_world,
)
from x402_world import APPROVED, MERCHANT_PAY_TO, TRLUSD_ISSUER, TRLUSD_WIRE_CODE, account

from credence.core.billing import max_total_cost_value
from credence.core.objects import Facility, ProcurementClass
from credence.core.primitives import (
    CountryCode,
    EvidenceTier,
    MicroTrlusd,
    ProviderId,
    QuoteId,
    ReasonCode,
    Timestamp,
)
from credence.core.reservation_ledger import ReservationLedger
from credence.core.signatures import sign
from credence.inference.gateway import Purchase
from credence.inference.local_llama import LOCAL_QWEN_PROVIDER_ID
from credence.inference.market import Selection, qualify_and_select
from credence.inference.nebius import NEBIUS_PROVIDER_ID
from credence.x402.errors import X402Refusal, X402Refused
from credence.x402.procurement import (
    OperatorAssertions,
    X402Offer,
    X402Projection,
    micro_trlusd,
    project,
)
from credence.x402.proposal import XrplAmount

MERCHANT = ProviderId("x402-mock-merchant")
NOW = Timestamp("2026-10-24T15:00:00Z")
US = CountryCode("US")


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield


def offer(
    value: str = "0.002",
    *,
    assertions: OperatorAssertions | None = None,
    tier: EvidenceTier = EvidenceTier.WITNESSED,
    currency: str = TRLUSD_WIRE_CODE,
    issuer: str = TRLUSD_ISSUER,
) -> X402Offer:
    return X402Offer(
        merchant_id=MERCHANT,
        service_url="https://merchant.example/v1/chat",
        method="POST",
        model_identifier="qwen3-30b-a3b-instruct-2507",
        model_family=MODEL_FAMILY,
        price=XrplAmount(currency, issuer, value),
        network=APPROVED.network,
        destination=MERCHANT_PAY_TO,
        valid_until=Timestamp("2026-12-31T00:00:00Z"),
        verified_evidence_tier=tier,
        assertions=assertions or OperatorAssertions(US, (US,), 8192),
    )


def with_merchant(pc: ProcurementClass, fac: Facility) -> tuple[ProcurementClass, Facility]:
    providers = tuple(sorted((*pc.allowed_providers, MERCHANT)))
    pc2 = replace(pc, allowed_providers=providers)
    fac2 = replace(fac, allowed_providers=providers, allowed_procurement_classes=(pc2.digest(),))
    return pc2, fac2


def x402_purchase(p: X402Projection, pc: ProcurementClass, fac: Facility, n: int) -> Purchase:
    scenario = Scenario(fac, p.quote, p.service_class, p.execution_profile, pc)
    return make_purchase(scenario, n, request_prompt=prompt(1))


def market(tmp_path: Path, pc: ProcurementClass, fac: Facility, extra: list[Purchase]) -> Selection:
    # The Nebius and local Qwen candidates, signed against this facility (same quotes, evidence).
    resigned = [
        make_purchase(
            Scenario(fac, c.quote, c.service_class, c.execution_profile, pc),
            10 + i,
            request_prompt=prompt(1),
        )
        for i, c in enumerate(candidates(fac, pc))
    ]
    ledger = ReservationLedger(tmp_path / "reservations.sqlite3")
    return qualify_and_select(
        [*resigned, *extra], registry=market_world().registry(), ledger=ledger, now=NOW
    )


def test_projection_reproduces_the_flat_price_exactly() -> None:
    p = project(offer("0.002"), APPROVED, QuoteId("x402-quote-1"))
    assert p.price_micro_trlusd == MicroTrlusd(2_000)
    assert max_total_cost_value(p.quote) == 2_000
    assert p.billing_faithful is False


@pytest.mark.parametrize("value", ["0.0000001", "0", "-1"])
def test_price_not_whole_micro_units_is_refused(value: str) -> None:
    with pytest.raises(X402Refused) as exc:
        micro_trlusd(XrplAmount(TRLUSD_WIRE_CODE, TRLUSD_ISSUER, value), APPROVED)
    assert exc.value.code is X402Refusal.AMOUNT_NOT_REPRESENTABLE


def test_other_asset_is_never_converted_into_trlusd() -> None:
    rlusd = "524C555344000000000000000000000000000000"
    with pytest.raises(X402Refused) as exc:
        project(offer(currency=rlusd), APPROVED, QuoteId("q"))
    assert exc.value.code is X402Refusal.ASSET_NOT_APPROVED
    with pytest.raises(X402Refused) as exc:
        project(offer(issuer=account("look-alike-issuer")), APPROVED, QuoteId("q"))
    assert exc.value.code is X402Refusal.ISSUER_NOT_APPROVED


def test_x402_cannot_claim_tier_two() -> None:
    with pytest.raises(X402Refused):
        project(offer(tier=EvidenceTier.BOUND_AND_SIGNED), APPROVED, QuoteId("q"))


def test_core_accepts_a_compatible_x402_offer_and_cheapest_wins(tmp_path: Path) -> None:
    pc, fac = with_merchant(GENERAL, GENERAL_FACILITY)
    # 50 micro-tRLUSD, below Nebius's projected 86 and local Qwen's maximum.
    p = project(offer("0.00005"), APPROVED, QuoteId("x402-quote-1"))
    selection = market(tmp_path, pc, fac, [x402_purchase(p, pc, fac, 99)])
    assert [q.qualified for q in selection.qualifications] == [True, True, True]
    assert selection.chosen is not None
    assert selection.chosen.purchase.request.provider_id == MERCHANT


def test_excessive_price_is_rejected_by_core(tmp_path: Path) -> None:
    pc, fac = with_merchant(GENERAL, GENERAL_FACILITY)
    p = project(offer("0.002"), APPROVED, QuoteId("x402-quote-1"))
    purchase = x402_purchase(p, pc, fac, 99)
    # The agent's signed cap is one micro-tRLUSD below the merchant's flat price.
    capped = replace(
        purchase.request, max_total_cost_micro_trlusd=MicroTrlusd(p.price_micro_trlusd.value - 1)
    )
    resigned = replace(purchase, request=capped, request_signature=sign(capped, AGENT_KEY))
    selection = market(tmp_path, pc, fac, [resigned])
    x402 = selection.qualifications[-1]
    assert not x402.qualified
    assert x402.reason_code is ReasonCode.PRICE_ABOVE_MAX


def test_over_facility_limit_is_rejected_by_core(tmp_path: Path) -> None:
    pc, fac = with_merchant(GENERAL, GENERAL_FACILITY)
    p = project(offer("11"), APPROVED, QuoteId("x402-quote-1"))  # 11 tRLUSD > 10 tRLUSD limit
    selection = market(tmp_path, pc, fac, [x402_purchase(p, pc, fac, 99)])
    assert selection.qualifications[-1].reason_code is ReasonCode.OVER_LIMIT


def test_unasserted_location_is_profile_not_allowed(tmp_path: Path) -> None:
    pc, fac = with_merchant(GENERAL, GENERAL_FACILITY)
    p = project(offer(assertions=OperatorAssertions()), APPROVED, QuoteId("x402-quote-1"))
    selection = market(tmp_path, pc, fac, [x402_purchase(p, pc, fac, 99)])
    assert selection.qualifications[-1].reason_code is ReasonCode.PROFILE_NOT_ALLOWED


def test_tier_two_facility_rejects_x402(tmp_path: Path) -> None:
    pc, fac = with_merchant(replace(CONTROLLED, allowed_quantizations=None), CONTROLLED_FACILITY)
    p = project(offer("0.0001"), APPROVED, QuoteId("x402-quote-1"))
    selection = market(tmp_path, pc, fac, [x402_purchase(p, pc, fac, 99)])
    x402 = selection.qualifications[-1]
    assert x402.reason_code is ReasonCode.TIER_TOO_LOW
    assert selection.chosen is not None
    assert selection.chosen.purchase.request.provider_id == LOCAL_QWEN_PROVIDER_ID


def test_merchant_not_on_the_facility_is_provider_not_allowed(tmp_path: Path) -> None:
    p = project(offer("0.0001"), APPROVED, QuoteId("x402-quote-1"))
    pc, fac = GENERAL, GENERAL_FACILITY  # merchant not added
    selection = market(tmp_path, pc, fac, [x402_purchase(p, pc, fac, 99)])
    assert selection.qualifications[-1].reason_code is ReasonCode.PROVIDER_NOT_ALLOWED


# --- 20: Nebius and Qwen procurement scenarios unchanged -----------------------------------


@pytest.mark.parametrize(
    ("pc", "fac", "winner"),
    [
        (GENERAL, GENERAL_FACILITY, NEBIUS_PROVIDER_ID),
        (CONTROLLED, CONTROLLED_FACILITY, LOCAL_QWEN_PROVIDER_ID),
    ],
)
def test_nebius_and_qwen_outcomes_are_unchanged_by_an_x402_candidate(
    tmp_path: Path, pc: ProcurementClass, fac: Facility, winner: ProviderId
) -> None:
    ledger = ReservationLedger(tmp_path / "base.sqlite3")
    registry = market_world().registry()
    baseline = qualify_and_select(candidates(fac, pc), registry=registry, ledger=ledger, now=NOW)
    assert baseline.chosen is not None
    assert baseline.chosen.purchase.request.provider_id == winner

    # An expensive x402 offer for a widened facility must not change who wins or why.
    pc2, fac2 = with_merchant(pc, fac)
    p = project(offer("5"), APPROVED, QuoteId("x402-quote-1"))
    selection = market(tmp_path, pc2, fac2, [x402_purchase(p, pc2, fac2, 99)])
    assert selection.chosen is not None
    assert selection.chosen.purchase.request.provider_id == winner
    before = [(q.qualified, q.reason_code) for q in baseline.qualifications]
    after = [(q.qualified, q.reason_code) for q in selection.qualifications[:2]]
    assert before == after
