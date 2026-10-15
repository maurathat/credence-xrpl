"""Verifier.

Tech: Python library

Responsibility (System architecture table, build plan):
Checks receipts: signatures, bindings, replay, profile match. Produces an immutable decision record

`verify` is pure and deterministic: no database, XRPL, network or clock, and no cryptography.
Signature results arrive as facts from a signature layer outside C2. The verifier compares digests
only; it never sees prompt or response text. The first failing check wins, in the order of
VERIFIER_REASON_CODES (contracts/POLICY-VERIFIER.md). Inputs whose objects do not refer to each
other raise VerifierInputError; that is a caller bug, not a decision.

Owner: Credence Core.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from credence.core.billing import total_cost_value
from credence.core.objects import (
    DecisionRecord,
    DeliveryReceiptBody,
    ExecutionProfile,
    ProviderAttestationBody,
    PurchaseRequest,
    Quote,
    ServiceClass,
)
from credence.core.primitives import (
    VERIFIER_REASON_CODES,
    DecisionOutcome,
    DecisionStage,
    EngineVersion,
    EvidenceTier,
    ReasonCode,
    Sha256Digest,
    Timestamp,
)

ENGINE_VERSION = EngineVersion("credence-core-verifier-c2")


class VerifierInputError(ValueError):
    """The inputs are inconsistent with each other; no decision is made."""


class ProviderSignatureFact(Enum):
    """What the signature layer established about the provider's attestation signature."""

    VALID = "valid"
    INVALID = "invalid"
    NOT_PRESENT = "not_present"


@dataclass(frozen=True)
class VerifierInput:
    """Everything one verification depends on.

    request, quote, service_class and execution_profile are what the policy engine approved.
    delivered_response_digest is computed independently from the delivered response.
    expected_previous_receipt_digest is the facility's last receipt digest, or None for the first.
    """

    receipt: DeliveryReceiptBody
    gateway_signature_valid: bool
    provider_signature: ProviderSignatureFact
    provider_attestation: ProviderAttestationBody | None
    request: PurchaseRequest
    quote: Quote
    service_class: ServiceClass
    execution_profile: ExecutionProfile
    delivered_response_digest: Sha256Digest
    expected_previous_receipt_digest: Sha256Digest | None
    receipt_already_settled: bool
    evaluated_at: Timestamp


@dataclass(frozen=True)
class VerifierDecision:
    record: DecisionRecord

    @property
    def accepted(self) -> bool:
        return self.record.outcome is DecisionOutcome.ACCEPT


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise VerifierInputError(message)


def _check_consistency(inp: VerifierInput) -> None:
    for name in ("gateway_signature_valid", "receipt_already_settled"):
        _require(type(getattr(inp, name)) is bool, f"{name} must be a bool")
    _require(isinstance(inp.provider_signature, ProviderSignatureFact), "provider_signature fact")
    _require(isinstance(inp.delivered_response_digest, Sha256Digest), "delivered_response_digest")
    _require(
        inp.expected_previous_receipt_digest is None
        or isinstance(inp.expected_previous_receipt_digest, Sha256Digest),
        "expected_previous_receipt_digest",
    )
    _require(isinstance(inp.evaluated_at, Timestamp), "evaluated_at: Timestamp")
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
    if inp.receipt.evidence_tier is EvidenceTier.WITNESSED:
        _require(inp.provider_attestation is None, "Tier 1 receipt with a provider attestation")
        _require(
            inp.provider_signature is ProviderSignatureFact.NOT_PRESENT,
            "Tier 1 receipt with a provider signature fact",
        )
    else:  # DeliveryReceiptBody admits only Tier 1 and Tier 2
        _require(inp.provider_attestation is not None, "Tier 2 receipt without its attestation")


def _binds(inp: VerifierInput) -> bool:
    receipt, request = inp.receipt, inp.request
    if (
        receipt.purchase_request_digest != request.digest()
        or receipt.execution_profile_digest != inp.execution_profile.digest()
        or receipt.provider_id != request.provider_id
        or receipt.nonce != request.nonce
        or receipt.evidence_tier is not inp.service_class.evidence_tier
    ):
        return False
    attestation = inp.provider_attestation
    if receipt.evidence_tier is EvidenceTier.BOUND_AND_SIGNED:
        assert attestation is not None  # checked in _check_consistency
        return (
            receipt.provider_attestation_digest == attestation.digest()
            and attestation.purchase_request_digest == receipt.purchase_request_digest
            and attestation.execution_profile_digest == receipt.execution_profile_digest
            and attestation.response_digest == receipt.response_digest
            and attestation.input_token_count == receipt.input_token_count
            and attestation.output_token_count == receipt.output_token_count
        )
    return True


def input_digests(inp: VerifierInput) -> tuple[Sha256Digest, ...]:
    """receipt, request, quote, service class, profile, delivered response, then the attestation."""
    digests = [
        inp.receipt.digest(),
        inp.request.digest(),
        inp.quote.digest(),
        inp.service_class.digest(),
        inp.execution_profile.digest(),
        inp.delivered_response_digest,
    ]
    if inp.provider_attestation is not None:
        digests.append(inp.provider_attestation.digest())
    return tuple(digests)


def _first_failure(inp: VerifierInput) -> ReasonCode | None:
    receipt, quote = inp.receipt, inp.quote
    tier2 = receipt.evidence_tier is EvidenceTier.BOUND_AND_SIGNED
    if not inp.gateway_signature_valid or (
        tier2 and inp.provider_signature is not ProviderSignatureFact.VALID
    ):
        return ReasonCode.RECEIPT_SIG_INVALID
    if not _binds(inp):
        return ReasonCode.BINDING_MISMATCH
    if inp.delivered_response_digest != receipt.response_digest:
        return ReasonCode.RESPONSE_DIGEST_MISMATCH
    if (
        receipt.input_token_count > quote.max_input_tokens
        or receipt.output_token_count > quote.max_output_tokens
    ):
        return ReasonCode.TOKEN_COUNT_EXCEEDS_QUOTE
    billed = total_cost_value(quote, receipt.input_token_count, receipt.output_token_count)
    if receipt.price_micro_trlusd.value != billed:
        return ReasonCode.BILLED_AMOUNT_MISMATCH
    if receipt.previous_receipt_digest != inp.expected_previous_receipt_digest:
        return ReasonCode.CHAIN_BROKEN
    if inp.receipt_already_settled:
        return ReasonCode.REPLAYED_RECEIPT
    return None


def verify(inp: VerifierInput) -> VerifierDecision:
    """Verify one receipt. Pure: the same input always gives the same decision record."""
    _check_consistency(inp)
    reason = _first_failure(inp)
    if reason is not None and reason not in VERIFIER_REASON_CODES:
        raise AssertionError(f"verifier produced a non-verifier code {reason}")
    return VerifierDecision(
        record=DecisionRecord(
            decision_stage=DecisionStage.VERIFIER,
            input_digests=input_digests(inp),
            outcome=DecisionOutcome.ACCEPT if reason is None else DecisionOutcome.REJECT,
            reason_code=reason,
            engine_version=ENGINE_VERSION,
            decided_at=inp.evaluated_at,
        )
    )
