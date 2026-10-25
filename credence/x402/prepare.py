"""From an observed challenge to an offer for Core, and from Core's decision to a proposal.

    candidate --offer_from--> X402Offer --procurement.project--> Core objects
    signed PurchaseRequest, Core policy evaluation --prepare_proposal--> unsigned proposal

Nothing a caller asserts is taken as authority. `prepare_proposal` re-derives every fact it relies
on from the inputs themselves:

- Compatibility is recomputed with `assess` from the actual requirement, the approved execution and
  the facilitator profile; no caller-supplied decision is accepted (E2). The ledger context must be
  on the approved execution network.
- The Core decision is checked against the purchase it claims to decide (E1): the request signature
  is re-verified against the trusted key registry, and Core's pure `policy_engine.evaluate` is run
  again on the same inputs. The recomputed decision record must equal the supplied one exactly, so
  its input digests are those of this request, facility, quote, service class, execution profile and
  procurement classes, and it is a policy-stage acceptance.
- The quote, service class and execution profile must be exactly the flat-price projection of this
  requirement (re-projected here). Core's approved maximum cost, its reservation amount, the
  projected price and the merchant's amount must be one and the same number of micro-tRLUSD.
- The provider must be the reserved x402 identity of the pinned endpoint (`x402_provider_id`), so an
  x402 purchase can never reuse a Nebius, local Qwen or other provider's decision, and a merchant
  cannot take another merchant's identity.
- The HTTP request sent to the merchant must be the one the challenge answered, and its body must be
  exactly the bytes the signed purchase request commits to (prompt digest).
- The agent identity is the purchase request's signing key; the facility, purchase request and
  prompt come from the signed request; the paying account must be the facility's borrower account;
  the required evidence tier is the strictest of the facility's and its procurement classes'.

Remaining trust (see contracts/requests/0009): the key registry, the facility's reservation
headroom and nonce use, and the evaluation time come from the caller's Core context. The decision
record itself is unsigned, so it is authoritative only because it is recomputed here; no Core
reservation is held, and the proposal remains non-submittable. It never signs, submits or contacts
anyone.

Owner: x402 (X402-P0, P2).
"""

from __future__ import annotations

import hashlib

from credence.core.kessai import canonicalize
from credence.core.policy_engine import PolicyInput, evaluate
from credence.core.primitives import (
    DecisionOutcome,
    DecisionStage,
    EvidenceTier,
    ProviderId,
    Timestamp,
)
from credence.core.signatures import KeyRegistry, policy_signature_facts
from credence.inference.market import Qualification
from credence.inference.payload import payload_digest
from credence.x402.challenge import (
    X402Challenge,
    XrplPaymentRequirement,
    check_live,
    request_digest,
)
from credence.x402.compatibility import FacilitatorProfile, assess
from credence.x402.discovery import PinnedEndpoint, ServiceCandidate
from credence.x402.errors import X402Refusal, X402Refused
from credence.x402.procurement import (
    MAX_X402_EVIDENCE_TIER,
    ApprovedExecution,
    OperatorAssertions,
    X402Offer,
    micro_trlusd,
    project,
)
from credence.x402.proposal import (
    InvoiceBinding,
    LedgerContext,
    PaymentAuthorizationProposal,
    ProposalTerms,
    PurchaseIntent,
    build_proposal,
    verify_binding,
)
from credence.x402.transport import HttpRequest

X402_PROVIDER_PREFIX = "x402."


def x402_provider_id(pinned: PinnedEndpoint) -> ProviderId:
    """The reserved Credence provider identity of one pinned x402 endpoint:
    `x402.<host>.<12 hex of sha256(url, method, network, payTo)>`. Bound to the exact endpoint and
    destination, and in a namespace no non-x402 provider in this repository uses. (Core does not yet
    reserve the namespace; contracts/requests/0009.)"""
    host = pinned.origin.split("://", 1)[-1].rsplit(":", 1)[0]
    binding = canonicalize(
        {
            "url": pinned.url,
            "method": pinned.method,
            "network": pinned.network,
            "pay_to": pinned.pay_to,
        }
    ).data
    return ProviderId(f"{X402_PROVIDER_PREFIX}{host}.{hashlib.sha256(binding).hexdigest()[:12]}")


def _recomputed_requirement(
    candidate: ServiceCandidate, approved: ApprovedExecution, facilitator: FacilitatorProfile
) -> XrplPaymentRequirement:
    challenge = candidate.challenge
    if challenge is None:
        raise X402Refused(X402Refusal.SERVICE_UNVERIFIED, candidate.status.value)
    for req in challenge.requirements:
        if assess(req, approved, facilitator, version=challenge.version).policy_evaluable:
            return req
    raise X402Refused(X402Refusal.SERVICE_UNVERIFIED, "no policy-evaluable requirement")


def offer_from(
    candidate: ServiceCandidate,
    *,
    approved: ApprovedExecution,
    facilitator: FacilitatorProfile,
    model_family: str,
    assertions: OperatorAssertions,
    now: Timestamp,
) -> X402Offer:
    """An offer for Core from a probed candidate. Compatibility is recomputed, not read from the
    candidate. The merchant identity is the reserved x402 identity of the endpoint; the model
    identifier is the merchant's advertised model if its challenge names one, else the resource
    URL; the model family is the operator's mapping. Evidence is capped at Tier 1 (witnessed)."""
    requirement = _recomputed_requirement(candidate, approved, facilitator)
    challenge = candidate.challenge
    assert challenge is not None
    return _offer(
        challenge,
        requirement,
        merchant_id=x402_provider_id(_pin(challenge, requirement)),
        model_identifier=candidate.advertised_model or challenge.resource_url,
        model_family=model_family,
        assertions=assertions,
        valid_until=check_live(requirement, challenge.observed, now),
    )


def _pin(challenge: X402Challenge, requirement: XrplPaymentRequirement) -> PinnedEndpoint:
    return PinnedEndpoint(
        url=challenge.resource_url,
        origin=challenge.observed.origin,
        method=challenge.observed.request_method,
        network=requirement.network,
        pay_to=requirement.pay_to,
    )


def _offer(
    challenge: X402Challenge,
    requirement: XrplPaymentRequirement,
    *,
    merchant_id: ProviderId,
    model_identifier: str,
    model_family: str,
    assertions: OperatorAssertions,
    valid_until: Timestamp,
) -> X402Offer:
    return X402Offer(
        merchant_id=merchant_id,
        service_url=challenge.resource_url,
        method=challenge.observed.request_method,
        model_identifier=model_identifier,
        model_family=model_family,
        price=requirement.amount,
        network=requirement.network,
        destination=requirement.pay_to,
        valid_until=valid_until,
        verified_evidence_tier=MAX_X402_EVIDENCE_TIER,
        assertions=assertions,
    )


def _check_endpoint(
    challenge: X402Challenge,
    requirement: XrplPaymentRequirement,
    request: HttpRequest,
    pinned: PinnedEndpoint,
) -> None:
    if requirement not in challenge.requirements:
        raise X402Refused(X402Refusal.BINDING_ALTERED, "requirement is not from this challenge")
    if request_digest(request) != challenge.observed.request_digest:
        raise X402Refused(X402Refusal.RESOURCE_MISMATCH, "request differs from the challenged one")
    if request.url != challenge.resource_url:
        raise X402Refused(X402Refusal.RESOURCE_MISMATCH, "request URL")
    if _pin(challenge, requirement) != pinned:
        raise X402Refused(X402Refusal.ENDPOINT_CHANGED, "challenge is not the pinned endpoint")


def _check_core(
    qualification: Qualification,
    registry: KeyRegistry,
) -> None:
    """The supplied decision is exactly Core's policy acceptance of this purchase."""
    purchase, decision = qualification.purchase, qualification.decision
    record = decision.record
    if record.decision_stage is not DecisionStage.POLICY:
        raise X402Refused(X402Refusal.CORE_DECISION_MISMATCH, "not a policy-stage decision")
    if record.outcome is not DecisionOutcome.ACCEPT or not decision.accepted:
        reason = record.reason_code
        raise X402Refused(X402Refusal.POLICY_REJECTED, reason.value if reason else "rejected")
    if purchase.request_signature is None:
        raise X402Refused(X402Refusal.CORE_DECISION_MISMATCH, "purchase request is unsigned")
    facts = policy_signature_facts(
        purchase.request, purchase.request_signature, purchase.facility, registry
    )
    try:
        recomputed = evaluate(
            PolicyInput(
                request=purchase.request,
                request_signature_valid=facts.request_signature_valid,
                working_key_current=facts.working_key_current,
                facility=purchase.facility,
                quote=purchase.quote,
                service_class=purchase.service_class,
                execution_profile=purchase.execution_profile,
                procurement_classes=purchase.procurement_classes,
                currently_reserved=qualification.currently_reserved,
                nonce_already_used=qualification.nonce_already_used,
                evaluated_at=record.decided_at,
            )
        )
    except ValueError as exc:  # PolicyInputError: objects that do not refer to each other
        raise X402Refused(X402Refusal.CORE_DECISION_MISMATCH, str(exc)) from None
    if recomputed.record != record:
        raise X402Refused(X402Refusal.CORE_DECISION_MISMATCH, "decision is not for this purchase")
    if (
        recomputed.max_total_cost_value != decision.max_total_cost_value
        or recomputed.reservation_amount != decision.reservation_amount
    ):
        raise X402Refused(X402Refusal.CORE_DECISION_MISMATCH, "approved amount differs")


def _check_projection(
    qualification: Qualification,
    challenge: X402Challenge,
    requirement: XrplPaymentRequirement,
    approved: ApprovedExecution,
    merchant_id: ProviderId,
    expires_at: Timestamp,
) -> None:
    """The quote, service class and profile are exactly this requirement's flat-price projection,
    and every amount is the same number of micro-tRLUSD."""
    purchase = qualification.purchase
    profile, service = purchase.execution_profile, purchase.service_class
    reprojected = project(
        _offer(
            challenge,
            requirement,
            merchant_id=merchant_id,
            model_identifier=profile.model_identifier,
            model_family=service.model_family,
            assertions=OperatorAssertions(
                service.jurisdiction, service.data_residency, profile.context_min_tokens
            ),
            valid_until=expires_at,
        ),
        approved,
        purchase.quote.quote_id,
    )
    if reprojected.execution_profile != profile:
        raise X402Refused(X402Refusal.CORE_DECISION_MISMATCH, "execution profile")
    if reprojected.service_class != service:
        raise X402Refused(X402Refusal.CORE_DECISION_MISMATCH, "service class or evidence tier")
    if reprojected.quote != purchase.quote:
        raise X402Refused(X402Refusal.CORE_DECISION_MISMATCH, "quote is not this flat price")
    price = micro_trlusd(requirement.amount, approved).value
    decision = qualification.decision
    reserved = decision.reservation_amount
    if (
        decision.max_total_cost_value != price
        or reserved is None
        or reserved.value != price
        or reprojected.price_micro_trlusd.value != price
    ):
        raise X402Refused(X402Refusal.CORE_DECISION_MISMATCH, "approved amount is not the price")


def prepare_proposal(
    *,
    challenge: X402Challenge,
    requirement: XrplPaymentRequirement,
    request: HttpRequest,
    pinned: PinnedEndpoint,
    qualification: Qualification,
    registry: KeyRegistry,
    approved: ApprovedExecution,
    facilitator: FacilitatorProfile,
    ledger: LedgerContext,
    now: Timestamp,
) -> PaymentAuthorizationProposal:
    terms = proposal_terms(
        challenge=challenge,
        requirement=requirement,
        request=request,
        pinned=pinned,
        qualification=qualification,
        registry=registry,
        approved=approved,
        facilitator=facilitator,
        ledger=ledger,
        now=now,
    )
    proposal = build_proposal(**vars(terms))
    verify_binding(proposal, terms)
    return proposal


def proposal_terms(
    *,
    challenge: X402Challenge,
    requirement: XrplPaymentRequirement,
    request: HttpRequest,
    pinned: PinnedEndpoint,
    qualification: Qualification,
    registry: KeyRegistry,
    approved: ApprovedExecution,
    facilitator: FacilitatorProfile,
    ledger: LedgerContext,
    now: Timestamp,
) -> ProposalTerms:
    """The verified terms of the one proposal these inputs allow. Use with `verify_binding` to
    re-check a stored proposal against the same inputs."""
    _check_endpoint(challenge, requirement, request, pinned)

    decision = assess(requirement, approved, facilitator, version=challenge.version)
    if not decision.policy_evaluable:
        raise X402Refused(decision.reasons[0], "requirement is not policy-evaluable")
    if ledger.network_id != approved.network_id or requirement.network_id != approved.network_id:
        raise X402Refused(X402Refusal.NETWORK_NOT_APPROVED, "ledger context network")
    expires_at = check_live(requirement, challenge.observed, now)

    purchase = qualification.purchase
    merchant_id = x402_provider_id(pinned)
    if not merchant_id.value.startswith(X402_PROVIDER_PREFIX):
        raise AssertionError("x402 provider identity outside its namespace")
    if purchase.request.provider_id != merchant_id or purchase.quote.provider_id != merchant_id:
        raise X402Refused(X402Refusal.PROVIDER_NAMESPACE, "purchase is not for this x402 merchant")
    _check_core(qualification, registry)
    _check_projection(qualification, challenge, requirement, approved, merchant_id, expires_at)

    if payload_digest(request.body) != purchase.request.prompt_digest:
        raise X402Refused(X402Refusal.RESOURCE_MISMATCH, "request body is not the signed prompt")
    if ledger.source_account != purchase.facility.borrower_account.value:
        raise X402Refused(X402Refusal.AGENT_MISMATCH, "paying account is not the facility's")
    assert purchase.request_signature is not None  # checked in _check_core
    required_tier = max(
        (
            purchase.facility.minimum_evidence_tier,
            *(pc.minimum_evidence_tier for pc in purchase.procurement_classes),
        ),
        key=lambda tier: tier.value,
    )
    if required_tier.value > EvidenceTier.WITNESSED.value:
        raise X402Refused(X402Refusal.POLICY_REJECTED, "x402 delivery cannot meet this tier")
    return ProposalTerms(
        intent=PurchaseIntent(
            agent_id=purchase.request_signature.key_id.value,
            facility_id=purchase.request.facility_id.value,
            purchase_request_digest=purchase.request.digest().value,
            prompt_digest=purchase.request.prompt_digest.value,
        ),
        merchant_id=merchant_id.value,
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
        required_evidence_tier=required_tier,
    )
