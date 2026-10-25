"""From an observed challenge to an offer for Core, and from Core's decision to a proposal.

    candidate (CHALLENGE_COMPATIBLE) --offer_from--> X402Offer --procurement.project--> Core objects
    Core's policy engine accepts --prepare_proposal--> PaymentAuthorizationProposal (unsigned)

`prepare_proposal` re-checks, at proposal time, everything a substituted or stale challenge could
change: the request is the one the challenge answered (request digest), the challenge names the
pinned endpoint (URL, origin, method, network, payTo), the requirement is one of the challenge's
own, it is policy-evaluable, it is still inside its validity window, the ledger context is on the
requirement's network, and Core accepted. It never signs, submits or contacts anyone.

Owner: x402 (X402-P0).
"""

from __future__ import annotations

from credence.core.policy_engine import PolicyDecision
from credence.core.primitives import EvidenceTier, ProviderId, Timestamp
from credence.x402.challenge import (
    X402Challenge,
    XrplPaymentRequirement,
    check_live,
    request_digest,
)
from credence.x402.compatibility import CompatibilityDecision
from credence.x402.discovery import PinnedEndpoint, ServiceCandidate
from credence.x402.errors import X402Refusal, X402Refused
from credence.x402.procurement import MAX_X402_EVIDENCE_TIER, OperatorAssertions, X402Offer
from credence.x402.proposal import (
    InvoiceBinding,
    LedgerContext,
    PaymentAuthorizationProposal,
    PurchaseIntent,
    build_proposal,
)
from credence.x402.transport import HttpRequest


def offer_from(
    candidate: ServiceCandidate,
    *,
    merchant_id: ProviderId,
    model_family: str,
    assertions: OperatorAssertions,
    now: Timestamp,
) -> X402Offer:
    """An offer for Core from a selectable candidate. The model identifier is the merchant's own
    advertised model if its challenge names one, else the resource URL; the model family is the
    operator's mapping, never inferred. Evidence is capped at Tier 1 (witnessed)."""
    requirement = candidate.compatible_requirement()
    challenge = candidate.challenge
    assert challenge is not None
    return X402Offer(
        merchant_id=merchant_id,
        service_url=challenge.resource_url,
        method=challenge.observed.request_method,
        model_identifier=candidate.advertised_model or challenge.resource_url,
        model_family=model_family,
        price=requirement.amount,
        network=requirement.network,
        destination=requirement.pay_to,
        valid_until=check_live(requirement, challenge.observed, now),
        verified_evidence_tier=MAX_X402_EVIDENCE_TIER,
        assertions=assertions,
    )


def prepare_proposal(
    *,
    challenge: X402Challenge,
    requirement: XrplPaymentRequirement,
    decision: CompatibilityDecision,
    request: HttpRequest,
    pinned: PinnedEndpoint,
    policy: PolicyDecision,
    intent: PurchaseIntent,
    merchant_id: str,
    ledger: LedgerContext,
    required_evidence_tier: EvidenceTier,
    now: Timestamp,
) -> PaymentAuthorizationProposal:
    if requirement not in challenge.requirements:
        raise X402Refused(X402Refusal.BINDING_ALTERED, "requirement is not from this challenge")
    if request_digest(request) != challenge.observed.request_digest:
        raise X402Refused(X402Refusal.RESOURCE_MISMATCH, "request differs from the challenged one")
    if request.url != challenge.resource_url:
        raise X402Refused(X402Refusal.RESOURCE_MISMATCH, "request URL")
    identity = (
        challenge.resource_url,
        challenge.observed.origin,
        challenge.observed.request_method,
        requirement.network,
        requirement.pay_to,
    )
    if identity != (pinned.url, pinned.origin, pinned.method, pinned.network, pinned.pay_to):
        raise X402Refused(X402Refusal.ENDPOINT_CHANGED, "challenge is not the pinned endpoint")
    if (
        decision.observed_network != requirement.network
        or decision.observed_currency != requirement.amount.currency
        or decision.observed_issuer != requirement.amount.issuer
        or decision.observed_amount != requirement.amount.value
    ):
        raise X402Refused(X402Refusal.BINDING_ALTERED, "decision is for other terms")
    if not decision.policy_evaluable:
        raise X402Refused(decision.reasons[0], "requirement is not policy-evaluable")
    expires_at = check_live(requirement, challenge.observed, now)
    if not policy.accepted:
        reason = policy.record.reason_code
        raise X402Refused(X402Refusal.POLICY_REJECTED, reason.value if reason else "rejected")
    if ledger.network_id != requirement.network_id:
        raise X402Refused(X402Refusal.NETWORK_NOT_APPROVED, "ledger context network")
    return build_proposal(
        intent=intent,
        merchant_id=merchant_id,
        resource_url=challenge.resource_url,
        request_method=challenge.observed.request_method,
        request_digest=challenge.observed.request_digest,
        requirement_digest=requirement.requirement_digest,
        invoice=InvoiceBinding(
            requirement.invoice_id, requirement.source_tag, requirement.destination_tag
        ),
        amount=requirement.amount,
        destination=requirement.pay_to,
        network=requirement.network,
        expires_at=expires_at,
        ledger=ledger,
        required_evidence_tier=required_evidence_tier,
    )
