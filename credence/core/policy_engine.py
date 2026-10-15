"""Policy engine.

Tech: Python library, pure functions

Responsibility (System architecture table, build plan):
Decides every purchase: accept or reject with a reason code

`evaluate` is pure and deterministic: no database, XRPL, network or clock. Everything it needs
arrives in PolicyInput, including facts established elsewhere: the request signature check and the
working-key status (no cryptography here), the amount currently held by live reservations, whether
the nonce was already used, and the evaluation time. The first failing rule wins, in the order of
POLICY_REASON_CODES (contracts/POLICY-VERIFIER.md). Inputs whose objects do not refer to each other
raise PolicyInputError; that is a caller bug, not a decision.

Owner: Credence Core.
"""

from __future__ import annotations

from dataclasses import dataclass

from credence.core.billing import max_total_cost_value
from credence.core.objects import (
    DecisionRecord,
    ExecutionProfile,
    Facility,
    ProcurementClass,
    PurchaseRequest,
    Quote,
    ServiceClass,
)
from credence.core.primitives import (
    POLICY_REASON_CODES,
    DecisionOutcome,
    DecisionStage,
    EngineVersion,
    FacilityStatus,
    MicroTrlusd,
    Purpose,
    ReasonCode,
    Sha256Digest,
    Timestamp,
)
from credence.core.procurement import meets_tier, required_tier, structural_failures

ENGINE_VERSION = EngineVersion("credence-core-policy-c2")


class PolicyInputError(ValueError):
    """The inputs are inconsistent with each other; no decision is made."""


@dataclass(frozen=True)
class PolicyInput:
    """Everything one policy decision depends on.

    procurement_classes are the facility's allowed procurement classes, resolved, in the order of
    facility.allowed_procurement_classes.
    """

    request: PurchaseRequest
    request_signature_valid: bool
    working_key_current: bool
    facility: Facility
    quote: Quote
    service_class: ServiceClass
    execution_profile: ExecutionProfile
    procurement_classes: tuple[ProcurementClass, ...]
    currently_reserved: MicroTrlusd
    nonce_already_used: bool
    evaluated_at: Timestamp


@dataclass(frozen=True)
class PolicyDecision:
    """The decision record, plus the reservation amount (the quote's maximum cost) on accept."""

    record: DecisionRecord
    max_total_cost_value: int
    reservation_amount: MicroTrlusd | None

    @property
    def accepted(self) -> bool:
        return self.record.outcome is DecisionOutcome.ACCEPT


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PolicyInputError(message)


def _check_consistency(inp: PolicyInput) -> None:
    for name in ("request_signature_valid", "working_key_current", "nonce_already_used"):
        _require(type(getattr(inp, name)) is bool, f"{name} must be a bool")
    _require(isinstance(inp.currently_reserved, MicroTrlusd), "currently_reserved: MicroTrlusd")
    _require(isinstance(inp.evaluated_at, Timestamp), "evaluated_at: Timestamp")
    _require(inp.request.facility_id == inp.facility.facility_id, "request is for another facility")
    _require(inp.request.quote_id == inp.quote.quote_id, "quote is not the requested quote")
    _require(inp.request.provider_id == inp.quote.provider_id, "quote is from another provider")
    _require(
        inp.quote.service_class_digest == inp.service_class.digest(),
        "service class is not the quoted service class",
    )
    _require(
        inp.service_class.execution_profile_digest == inp.execution_profile.digest(),
        "execution profile is not the service class's profile",
    )
    _require(
        tuple(pc.digest() for pc in inp.procurement_classes)
        == inp.facility.allowed_procurement_classes,
        "procurement classes are not the facility's allowed procurement classes",
    )


def input_digests(inp: PolicyInput) -> tuple[Sha256Digest, ...]:
    """request, facility, quote, service class, execution profile, then each procurement class."""
    return (
        inp.request.digest(),
        inp.facility.digest(),
        inp.quote.digest(),
        inp.service_class.digest(),
        inp.execution_profile.digest(),
        *(pc.digest() for pc in inp.procurement_classes),
    )


def _first_failure(inp: PolicyInput, max_cost: int) -> ReasonCode | None:
    request, facility, at = inp.request, inp.facility, inp.evaluated_at
    if not inp.request_signature_valid:
        return ReasonCode.SIG_INVALID
    if not inp.working_key_current:
        return ReasonCode.KEY_REVOKED
    if facility.status is not FacilityStatus.ACTIVE:
        return ReasonCode.FACILITY_NOT_ACTIVE
    if at > facility.maturity_at or at > request.expires_at:
        return ReasonCode.FACILITY_EXPIRED
    if at > inp.quote.valid_until:
        return ReasonCode.QUOTE_EXPIRED
    if request.purpose is not Purpose.INFERENCE or facility.purpose is not Purpose.INFERENCE:
        return ReasonCode.PURPOSE_MISMATCH  # unreachable under C1A typing: Purpose has one value
    if request.provider_id not in facility.allowed_providers:
        return ReasonCode.PROVIDER_NOT_ALLOWED
    structural = [
        pc
        for pc in inp.procurement_classes
        if not structural_failures(
            pc, inp.service_class, inp.execution_profile, request.provider_id
        )
    ]
    if not structural:
        return ReasonCode.PROFILE_NOT_ALLOWED
    if not any(
        meets_tier(inp.service_class, required_tier(facility.minimum_evidence_tier, pc))
        for pc in structural
    ):
        return ReasonCode.TIER_TOO_LOW
    if max_cost > request.max_total_cost_micro_trlusd.value:
        return ReasonCode.PRICE_ABOVE_MAX
    committed = facility.drawn_micro_trlusd.value + inp.currently_reserved.value + max_cost
    if committed > facility.limit_micro_trlusd.value:
        return ReasonCode.OVER_LIMIT
    if inp.nonce_already_used:
        return ReasonCode.NONCE_REUSED
    return None


def evaluate(inp: PolicyInput) -> PolicyDecision:
    """Decide one purchase. Pure: the same input always gives the same decision record."""
    _check_consistency(inp)
    max_cost = max_total_cost_value(inp.quote)
    reason = _first_failure(inp, max_cost)
    if reason is not None and reason not in POLICY_REASON_CODES:
        raise AssertionError(f"policy engine produced a non-policy code {reason}")
    record = DecisionRecord(
        decision_stage=DecisionStage.POLICY,
        input_digests=input_digests(inp),
        outcome=DecisionOutcome.ACCEPT if reason is None else DecisionOutcome.REJECT,
        reason_code=reason,
        engine_version=ENGINE_VERSION,
        decided_at=inp.evaluated_at,
    )
    return PolicyDecision(
        record=record,
        max_total_cost_value=max_cost,
        reservation_amount=MicroTrlusd(max_cost) if reason is None else None,
    )
