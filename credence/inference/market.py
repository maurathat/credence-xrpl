"""Choosing among Core-qualified quotes for one job. Not a policy engine.

Each candidate is a complete signed purchase for the same job (same facility, same prompt), one per
offered quote. Eligibility is decided only by Core: every candidate is run through the real
`policy_engine.evaluate` with its real C3A signature facts and the ledger's current headroom and
nonce use, as a dry run (nothing is reserved or stored). Routing then applies one rule to the
candidates Core accepted:

    lowest maximum cost (Core's max_total_cost_value for the quote),
    then provider ID, then quote ID (deterministic tie break)

A candidate Core rejected is never chosen, however cheap. The chosen purchase then goes through
the gateway's normal path, where `ReservationLedger.reserve` decides again and is the authority.

Owner: Inference.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from credence.core.policy_engine import PolicyDecision, PolicyInput, evaluate
from credence.core.primitives import DecisionOutcome, MicroTrlusd, ReasonCode, Timestamp
from credence.core.reservation_ledger import ReservationLedger
from credence.core.signatures import KeyRegistry, policy_signature_facts
from credence.inference.gateway import GatewayOutcome, Purchase


@dataclass(frozen=True)
class Qualification:
    """Core's dry-run decision for one candidate. Not stored; not a reservation."""

    purchase: Purchase
    decision: PolicyDecision
    currently_reserved: MicroTrlusd
    nonce_already_used: bool

    @property
    def qualified(self) -> bool:
        return self.decision.record.outcome is DecisionOutcome.ACCEPT

    @property
    def reason_code(self) -> ReasonCode | None:
        return self.decision.record.reason_code

    @property
    def max_total_cost(self) -> int:
        return self.decision.max_total_cost_value


@dataclass(frozen=True)
class Selection:
    """Every candidate's qualification, in the order given, and the chosen one (None: none
    qualified)."""

    qualifications: tuple[Qualification, ...]
    chosen: Qualification | None

    @property
    def qualifying_count(self) -> int:
        return sum(q.qualified for q in self.qualifications)


@dataclass(frozen=True)
class MarketOutcome:
    """The selection, and the gateway outcome for the chosen candidate (None when no candidate
    qualified: nothing was reserved and no provider was called)."""

    selection: Selection
    outcome: GatewayOutcome | None


def qualify_and_select(
    candidates: Sequence[Purchase],
    *,
    registry: KeyRegistry,
    ledger: ReservationLedger,
    now: Timestamp,
) -> Selection:
    """Ask Core about every candidate, then pick the cheapest one Core accepted."""
    if not candidates:
        raise ValueError("no candidates")
    first = candidates[0]
    for candidate in candidates[1:]:
        if candidate.request.facility_id != first.request.facility_id:
            raise ValueError("candidates must be for the same facility")
        if candidate.request.prompt_digest != first.request.prompt_digest:
            raise ValueError("candidates must be for the same prompt")

    qualifications = []
    for candidate in candidates:
        facts = policy_signature_facts(
            candidate.request, candidate.request_signature, candidate.facility, registry
        )
        reserved = ledger.held_amount(candidate.request.facility_id, now)
        nonce_used = ledger.nonce_used(candidate.request.facility_id, candidate.request.nonce)
        decision = evaluate(
            PolicyInput(
                request=candidate.request,
                request_signature_valid=facts.request_signature_valid,
                working_key_current=facts.working_key_current,
                facility=candidate.facility,
                quote=candidate.quote,
                service_class=candidate.service_class,
                execution_profile=candidate.execution_profile,
                procurement_classes=candidate.procurement_classes,
                currently_reserved=reserved,
                nonce_already_used=nonce_used,
                evaluated_at=now,
            )
        )
        qualifications.append(Qualification(candidate, decision, reserved, nonce_used))

    qualified = [q for q in qualifications if q.qualified]
    chosen = min(
        qualified,
        key=lambda q: (
            q.max_total_cost,
            q.purchase.request.provider_id.value,
            q.purchase.request.quote_id.value,
        ),
        default=None,
    )
    return Selection(tuple(qualifications), chosen)
