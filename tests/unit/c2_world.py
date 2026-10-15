"""Coherent C2 test worlds built from the committed C1A vectors, and one breaker per rule.

A world holds the objects and facts for one policy or verifier evaluation. `wired()` recomputes the
digests that tie the objects together (profile -> service class -> quote, procurement classes ->
facility), so breakers can change any object and compose freely. Each breaker makes exactly one
rule fail on the base world; the engines themselves decide every outcome.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from credence.core.objects import (
    DeliveryReceiptBody,
    ExecutionProfile,
    Facility,
    ProcurementClass,
    ProviderAttestationBody,
    PurchaseRequest,
    Quote,
    ServiceClass,
)
from credence.core.policy_engine import PolicyInput
from credence.core.primitives import (
    EvidenceTier,
    FacilityStatus,
    MicroTrlusd,
    Nonce,
    ProviderId,
    QuoteId,
    ReasonCode,
    Sha256Digest,
    Timestamp,
)
from credence.core.reservation_ledger import PurchaseContext
from credence.core.verifier import ProviderSignatureFact, VerifierInput

VECTORS = Path(__file__).resolve().parents[2] / "vectors" / "c1"


def _load(name: str) -> dict[str, Any]:
    value: dict[str, Any] = json.loads((VECTORS / f"{name}.json").read_text())["input"]
    return value


REQUEST = PurchaseRequest.from_canonical(_load("purchase_request-basic"))
FACILITY = Facility.from_canonical(_load("facility-active"))
QUOTE = Quote.from_canonical(_load("quote-basic"))
SERVICE_B = ServiceClass.from_canonical(_load("service_class-tier2"))
PROFILE_B = ExecutionProfile.from_canonical(_load("execution_profile-full"))
PROCUREMENT = ProcurementClass.from_canonical(_load("procurement_class-basic"))
RECEIPT_TIER2 = DeliveryReceiptBody.from_canonical(_load("delivery_receipt-tier2"))
ATTESTATION = ProviderAttestationBody.from_canonical(_load("provider_attestation-basic"))

NOW = Timestamp("2026-10-24T15:00:00Z")
MAX_COST = 2458  # ceil(8192 * 150000 / 1e6) + ceil(2048 * 600000 / 1e6) = 1229 + 1229
PROVIDER_A = ProviderId("provider-a")
OTHER_DIGEST = Sha256Digest.from_hex("f" * 64)


@dataclass(frozen=True)
class PolicyWorld:
    request: PurchaseRequest = REQUEST
    facility: Facility = FACILITY
    quote: Quote = QUOTE
    service_class: ServiceClass = SERVICE_B
    profile: ExecutionProfile = PROFILE_B
    procurement: tuple[ProcurementClass, ...] = (PROCUREMENT,)
    request_signature_valid: bool = True
    working_key_current: bool = True
    currently_reserved: MicroTrlusd = field(default_factory=lambda: MicroTrlusd(0))
    nonce_already_used: bool = False
    evaluated_at: Timestamp = NOW

    def _wired_objects(
        self,
    ) -> tuple[Facility, Quote, ServiceClass, tuple[ProcurementClass, ...]]:
        service_class = replace(self.service_class, execution_profile_digest=self.profile.digest())
        quote = replace(self.quote, service_class_digest=service_class.digest())
        procurement = tuple(sorted(self.procurement, key=lambda pc: pc.digest().value))
        facility = replace(
            self.facility, allowed_procurement_classes=tuple(pc.digest() for pc in procurement)
        )
        return facility, quote, service_class, procurement

    def wired(self) -> PolicyInput:
        facility, quote, service_class, procurement = self._wired_objects()
        return PolicyInput(
            request=self.request,
            request_signature_valid=self.request_signature_valid,
            working_key_current=self.working_key_current,
            facility=facility,
            quote=quote,
            service_class=service_class,
            execution_profile=self.profile,
            procurement_classes=procurement,
            currently_reserved=self.currently_reserved,
            nonce_already_used=self.nonce_already_used,
            evaluated_at=self.evaluated_at,
        )

    def context(self) -> PurchaseContext:
        facility, quote, service_class, procurement = self._wired_objects()
        return PurchaseContext(
            request=self.request,
            request_signature_valid=self.request_signature_valid,
            working_key_current=self.working_key_current,
            facility=facility,
            quote=quote,
            service_class=service_class,
            execution_profile=self.profile,
            procurement_classes=procurement,
        )


def headroom(world: PolicyWorld) -> int:
    return world.facility.limit_micro_trlusd.value - world.facility.drawn_micro_trlusd.value


POLICY_BREAKERS: dict[ReasonCode, Callable[[PolicyWorld], PolicyWorld]] = {
    ReasonCode.SIG_INVALID: lambda w: replace(w, request_signature_valid=False),
    ReasonCode.KEY_REVOKED: lambda w: replace(w, working_key_current=False),
    ReasonCode.FACILITY_NOT_ACTIVE: lambda w: replace(
        w, facility=replace(w.facility, status=FacilityStatus.FROZEN)
    ),
    ReasonCode.FACILITY_EXPIRED: lambda w: replace(
        w, evaluated_at=w.request.expires_at.plus_seconds(1)
    ),
    ReasonCode.QUOTE_EXPIRED: lambda w: replace(
        w, quote=replace(w.quote, valid_until=NOW.plus_seconds(-1))
    ),
    ReasonCode.PROVIDER_NOT_ALLOWED: lambda w: replace(
        w, facility=replace(w.facility, allowed_providers=(PROVIDER_A,))
    ),
    ReasonCode.PROFILE_NOT_ALLOWED: lambda w: replace(
        w, service_class=replace(w.service_class, model_family="other-model-family")
    ),
    ReasonCode.TIER_TOO_LOW: lambda w: replace(
        w, facility=replace(w.facility, minimum_evidence_tier=EvidenceTier.MEASURED)
    ),
    ReasonCode.PRICE_ABOVE_MAX: lambda w: replace(
        w, request=replace(w.request, max_total_cost_micro_trlusd=MicroTrlusd(MAX_COST - 1))
    ),
    ReasonCode.OVER_LIMIT: lambda w: replace(
        w, currently_reserved=MicroTrlusd(headroom(w) - MAX_COST + 1)
    ),
    ReasonCode.NONCE_REUSED: lambda w: replace(w, nonce_already_used=True),
}


@dataclass(frozen=True)
class VerifierWorld:
    receipt: DeliveryReceiptBody = RECEIPT_TIER2
    attestation: ProviderAttestationBody | None = ATTESTATION
    request: PurchaseRequest = REQUEST
    quote: Quote = QUOTE
    service_class: ServiceClass = SERVICE_B
    profile: ExecutionProfile = PROFILE_B
    gateway_signature_valid: bool = True
    provider_signature: ProviderSignatureFact = ProviderSignatureFact.VALID
    delivered_response_digest: Sha256Digest = RECEIPT_TIER2.response_digest
    expected_previous_receipt_digest: Sha256Digest | None = RECEIPT_TIER2.previous_receipt_digest
    receipt_already_settled: bool = False
    evaluated_at: Timestamp = Timestamp("2026-10-24T15:00:04Z")
    wire_attestation: bool = True

    def wired(self) -> VerifierInput:
        service_class = replace(self.service_class, execution_profile_digest=self.profile.digest())
        quote = replace(self.quote, service_class_digest=service_class.digest())
        receipt = self.receipt
        if self.wire_attestation and self.attestation is not None:
            receipt = replace(receipt, provider_attestation_digest=self.attestation.digest())
        return VerifierInput(
            receipt=receipt,
            gateway_signature_valid=self.gateway_signature_valid,
            provider_signature=self.provider_signature,
            provider_attestation=self.attestation,
            request=self.request,
            quote=quote,
            service_class=service_class,
            execution_profile=self.profile,
            delivered_response_digest=self.delivered_response_digest,
            expected_previous_receipt_digest=self.expected_previous_receipt_digest,
            receipt_already_settled=self.receipt_already_settled,
            evaluated_at=self.evaluated_at,
        )


def _over_cap(w: VerifierWorld) -> VerifierWorld:
    count = w.quote.max_input_tokens + 1
    assert w.attestation is not None
    return replace(
        w,
        receipt=replace(w.receipt, input_token_count=count),
        attestation=replace(w.attestation, input_token_count=count),
    )


VERIFIER_BREAKERS: dict[ReasonCode, Callable[[VerifierWorld], VerifierWorld]] = {
    ReasonCode.RECEIPT_SIG_INVALID: lambda w: replace(w, gateway_signature_valid=False),
    ReasonCode.BINDING_MISMATCH: lambda w: replace(
        w, receipt=replace(w.receipt, nonce=Nonce("0" * 32))
    ),
    ReasonCode.RESPONSE_DIGEST_MISMATCH: lambda w: replace(
        w, delivered_response_digest=OTHER_DIGEST
    ),
    ReasonCode.TOKEN_COUNT_EXCEEDS_QUOTE: _over_cap,
    ReasonCode.BILLED_AMOUNT_MISMATCH: lambda w: replace(
        w,
        receipt=replace(
            w.receipt, price_micro_trlusd=w.receipt.price_micro_trlusd + MicroTrlusd(1)
        ),
    ),
    ReasonCode.CHAIN_BROKEN: lambda w: replace(w, expected_previous_receipt_digest=OTHER_DIGEST),
    ReasonCode.REPLAYED_RECEIPT: lambda w: replace(w, receipt_already_settled=True),
}


def tier1_world() -> VerifierWorld:
    """A coherent Tier 1 (witnessed) purchase from provider A with its own quote and receipt."""
    profile_a = replace(PROFILE_B, model_artifact_digest=None, runtime=None)
    service_a = replace(
        SERVICE_B,
        execution_profile_digest=profile_a.digest(),
        evidence_tier=EvidenceTier.WITNESSED,
    )
    quote_a = replace(
        QUOTE,
        quote_id=QuoteId("quote-a-0001"),
        provider_id=PROVIDER_A,
        service_class_digest=service_a.digest(),
    )
    request_a = replace(REQUEST, provider_id=PROVIDER_A, quote_id=quote_a.quote_id)
    receipt_a = replace(
        RECEIPT_TIER2,
        purchase_request_digest=request_a.digest(),
        execution_profile_digest=profile_a.digest(),
        provider_id=PROVIDER_A,
        evidence_tier=EvidenceTier.WITNESSED,
        provider_attestation_digest=None,
        previous_receipt_digest=None,
    )
    return VerifierWorld(
        receipt=receipt_a,
        attestation=None,
        request=request_a,
        quote=quote_a,
        service_class=service_a,
        profile=profile_a,
        provider_signature=ProviderSignatureFact.NOT_PRESENT,
        expected_previous_receipt_digest=None,
    )


def world_for_chain(length: int) -> list[VerifierWorld]:
    """`length` Tier 1 purchases on one facility, each receipt chained to the one before."""
    base = tier1_world()
    worlds: list[VerifierWorld] = []
    previous: Sha256Digest | None = None
    for index in range(length):
        request = replace(base.request, nonce=Nonce(f"{index + 1:032x}"))
        receipt = replace(
            base.receipt,
            purchase_request_digest=request.digest(),
            nonce=request.nonce,
            previous_receipt_digest=previous,
        )
        world = replace(
            base, request=request, receipt=receipt, expected_previous_receipt_digest=previous
        )
        worlds.append(world)
        previous = world.wired().receipt.digest()
    return worlds
