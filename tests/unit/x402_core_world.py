"""Core-backed X402 flow for tests: probe -> offer -> projection -> signed purchase -> Core.

The mock merchant is on the configured local standalone network asking for local synthetic tRLUSD
(a test token). The x402 purchase is signed by the test agent key and decided by Core's real policy
engine beside the Nebius and local Qwen candidates. The HTTP body sent to the merchant is exactly
the prompt the purchase request commits to. Nothing is signed for payment or submitted.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from inference_world import AGENT_KEY, BORROWER, Scenario, make_purchase
from market_world import GENERAL, GENERAL_FACILITY, MODEL_FAMILY, candidates, market_world
from x402_world import (
    APPROVED,
    LEDGER,
    LOCAL_FACILITATOR,
    MERCHANT_URL,
    NOW,
    PROBE_BODY,
    challenge_doc,
    client,
    endpoint,
    probe_request,
    requirement_doc,
)
from x402_world import route as mock_route

from credence.core.objects import Facility, ProcurementClass
from credence.core.primitives import CountryCode, MicroTrlusd, ProviderId, QuoteId
from credence.core.reservation_ledger import ReservationLedger
from credence.core.signatures import KeyRegistry, sign
from credence.inference.gateway import Purchase
from credence.inference.market import Qualification, Selection, qualify_and_select
from credence.x402.challenge import X402Challenge
from credence.x402.discovery import PinnedEndpoint, ServiceCandidate, probe
from credence.x402.errors import X402Refusal, X402Refused
from credence.x402.prepare import offer_from, prepare_proposal, proposal_terms, x402_provider_id
from credence.x402.procurement import OperatorAssertions, X402Projection, project
from credence.x402.proposal import PaymentAuthorizationProposal, ProposalTerms

US = CountryCode("US")
PAY_LEDGER = replace(LEDGER, source_account=BORROWER.value)  # the facility's borrower account
CHEAP = challenge_doc(requirement_doc(amount="0.00005"))  # 50 micro-tRLUSD: below Nebius 86
_LEDGERS = itertools.count()  # one fresh reservation ledger per qualification


def widened(merchant: ProviderId) -> tuple[ProcurementClass, Facility]:
    providers = tuple(sorted((*GENERAL.allowed_providers, merchant)))
    pc = replace(GENERAL, allowed_providers=providers)
    fac = replace(
        GENERAL_FACILITY, allowed_providers=providers, allowed_procurement_classes=(pc.digest(),)
    )
    return pc, fac


@dataclass
class Flow:
    tmp: Path
    candidate: ServiceCandidate
    challenge: X402Challenge
    pinned: PinnedEndpoint
    merchant: ProviderId
    projection: X402Projection
    procurement: ProcurementClass
    facility: Facility
    selection: Selection
    registry: KeyRegistry

    @property
    def qualification(self) -> Qualification:
        return self.selection.qualifications[-1]  # the x402 candidate

    @property
    def winner(self) -> ProviderId:
        assert self.selection.chosen is not None
        return self.selection.chosen.purchase.request.provider_id


def x402_purchase(f: Flow, nonce: int, cap: int | None = None) -> Purchase:
    p = f.projection
    purchase = make_purchase(
        Scenario(f.facility, p.quote, p.service_class, p.execution_profile, f.procurement),
        nonce,
        request_prompt=PROBE_BODY,
    )
    if cap is not None:
        capped = replace(purchase.request, max_total_cost_micro_trlusd=MicroTrlusd(cap))
        purchase = replace(purchase, request=capped, request_signature=sign(capped, AGENT_KEY))
    return purchase


def qualify(f: Flow, purchases: list[Purchase]) -> Selection:
    others = [
        make_purchase(
            Scenario(f.facility, c.quote, c.service_class, c.execution_profile, f.procurement),
            10 + i,
            request_prompt=PROBE_BODY,
        )
        for i, c in enumerate(candidates(f.facility, f.procurement))
    ]
    return qualify_and_select(
        [*others, *purchases],
        registry=f.registry,
        ledger=ReservationLedger(f.tmp / f"reservations-{next(_LEDGERS)}.sqlite3"),
        now=NOW,
    )


def flow(tmp_path: Path, doc: dict[str, Any] | None = None, cap: int | None = None) -> Flow:
    guarded, _ = client({("POST", MERCHANT_URL): mock_route(doc or challenge_doc())})
    candidate = probe(
        endpoint(), guarded, observed_at=NOW, approved=APPROVED, facilitator=LOCAL_FACILITATOR
    )
    assert candidate.selectable and candidate.challenge is not None
    offer = offer_from(
        candidate,
        approved=APPROVED,
        facilitator=LOCAL_FACILITATOR,
        model_family=MODEL_FAMILY,
        assertions=OperatorAssertions(US, (US,), 8192),
        now=NOW,
    )
    pinned = candidate.pin()
    merchant = x402_provider_id(pinned)
    assert offer.merchant_id == merchant
    projection = project(offer, APPROVED, QuoteId("x402-quote-1"))
    pc, fac = widened(merchant)
    f = Flow(
        tmp_path,
        candidate,
        candidate.challenge,
        pinned,
        merchant,
        projection,
        pc,
        fac,
        selection=Selection((), None),
        registry=market_world().registry(),
    )
    f.selection = qualify(f, [x402_purchase(f, 99, cap)])
    return f


def inputs(f: Flow, **changes: object) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "challenge": f.challenge,
        "requirement": f.challenge.requirement(),
        "request": probe_request(),
        "pinned": f.pinned,
        "qualification": f.qualification,
        "registry": f.registry,
        "approved": APPROVED,
        "facilitator": LOCAL_FACILITATOR,
        "ledger": PAY_LEDGER,
        "now": NOW,
    }
    kwargs.update(changes)
    return kwargs


def propose(f: Flow, **changes: object) -> PaymentAuthorizationProposal:
    return prepare_proposal(**inputs(f, **changes))  # type: ignore[arg-type]


def terms(f: Flow, **changes: object) -> ProposalTerms:
    return proposal_terms(**inputs(f, **changes))  # type: ignore[arg-type]


def refusal(f: Flow, **changes: object) -> X402Refusal:
    try:
        propose(f, **changes)
    except X402Refused as exc:
        return exc.code
    raise AssertionError("proposal was not refused")
