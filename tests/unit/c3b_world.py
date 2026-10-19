"""Signed purchases and deliveries for the C3B settlement coordinator tests and fixtures.

Built from the committed C1 vector objects and the deterministic C3A TEST keys (NOT FOR
PRODUCTION). Every signature is real Ed25519; every decision comes from the real engines.
"""

from dataclasses import replace
from pathlib import Path

from c2_world import (
    FACILITY,
    GENERAL_TIER1,
    QUOTE,
    REQUEST,
    PolicyWorld,
    provider_a_policy_world,
)
from c3a_keys import AGENT, GATEWAY, PROVIDER_B, registry
from nacl.signing import SigningKey

from credence.core.billing import total_cost
from credence.core.objects import (
    DeliveryReceiptBody,
    Facility,
    ProviderAttestationBody,
    Quote,
)
from credence.core.primitives import (
    EvidenceTier,
    MicroTrlusd,
    Nonce,
    ReservationId,
    Sha256Digest,
    Timestamp,
)
from credence.core.reservation_ledger import ReservationLedger
from credence.core.settlement_coordinator import (
    DeliverySubmission,
    SettlementCoordinator,
    SignedPurchase,
)
from credence.core.signatures import sign

NOW = Timestamp("2026-10-24T15:00:00Z")
RESPONSE = Sha256Digest.from_hex("ab" * 32)
LIMIT = 1_000_000


def facility(limit: int = LIMIT, drawn: int = 0) -> Facility:
    return replace(
        FACILITY,
        limit_micro_trlusd=MicroTrlusd(limit),
        drawn_micro_trlusd=MicroTrlusd(drawn),
    )


def coordinator(
    path: Path, base: Facility | None = None, **kwargs: object
) -> SettlementCoordinator:
    coord = SettlementCoordinator(ReservationLedger(path), registry(), **kwargs)  # type: ignore[arg-type]
    if base is not None:
        coord.register_facility(base)
    return coord


def current_facility(coord: SettlementCoordinator, base: Facility) -> Facility:
    """`base` with the stored drawn amount: what a caller must present to reserve."""
    exposure = coord.exposure(base.facility_id, NOW)
    return replace(base, drawn_micro_trlusd=exposure.drawn)


def purchase(
    index: int,
    base: Facility,
    *,
    quote: Quote = QUOTE,
    tier: int = 2,
    signer: SigningKey = AGENT,
) -> SignedPurchase:
    """Purchase number `index` (its own nonce) against `base`, signed by `signer`."""
    if tier == 1:
        world = provider_a_policy_world((GENERAL_TIER1,))
        world = replace(world, facility=base, quote=replace(world.quote, **_prices(quote)))
    else:
        world = PolicyWorld(facility=base, quote=quote)
    world = replace(
        world,
        request=replace(
            world.request,
            nonce=Nonce(f"{index:032x}"),
            max_total_cost_micro_trlusd=REQUEST.max_total_cost_micro_trlusd,
        ),
    )
    context = world.context()
    return SignedPurchase(
        request=context.request,
        request_signature=sign(context.request, signer),
        facility=context.facility,
        quote=context.quote,
        service_class=context.service_class,
        execution_profile=context.execution_profile,
        procurement_classes=context.procurement_classes,
    )


def _prices(quote: Quote) -> dict[str, object]:
    return {
        "input_price_per_million_micro_trlusd": quote.input_price_per_million_micro_trlusd,
        "output_price_per_million_micro_trlusd": quote.output_price_per_million_micro_trlusd,
        "max_input_tokens": quote.max_input_tokens,
        "max_output_tokens": quote.max_output_tokens,
    }


def delivery(
    p: SignedPurchase,
    reservation_id: ReservationId,
    *,
    previous: Sha256Digest | None,
    input_tokens: int = 1234,
    output_tokens: int = 1,
    gateway: SigningKey | None = GATEWAY,
    provider: SigningKey | None = None,
    delivered: Sha256Digest = RESPONSE,
    price_delta: int = 0,
    **receipt_changes: object,
) -> DeliverySubmission:
    """A signed delivery for purchase `p`. Tier 2 attests with provider-b unless overridden."""
    tier2 = p.service_class.evidence_tier is EvidenceTier.BOUND_AND_SIGNED
    attestation = (
        ProviderAttestationBody(
            purchase_request_digest=p.request.digest(),
            execution_profile_digest=p.execution_profile.digest(),
            response_digest=RESPONSE,
            input_token_count=input_tokens,
            output_token_count=output_tokens,
        )
        if tier2
        else None
    )
    receipt = DeliveryReceiptBody(
        purchase_request_digest=p.request.digest(),
        execution_profile_digest=p.execution_profile.digest(),
        provider_id=p.request.provider_id,
        response_digest=RESPONSE,
        input_token_count=input_tokens,
        output_token_count=output_tokens,
        price_micro_trlusd=MicroTrlusd(
            total_cost(p.quote, input_tokens, output_tokens).value + price_delta
        ),
        started_at=NOW.plus_seconds(1),
        completed_at=NOW.plus_seconds(3),
        nonce=p.request.nonce,
        evidence_tier=p.service_class.evidence_tier,
        provider_attestation_digest=None if attestation is None else attestation.digest(),
        previous_receipt_digest=previous,
    )
    receipt = replace(receipt, **receipt_changes)
    provider_key = provider if provider is not None else (PROVIDER_B if tier2 else None)
    return DeliverySubmission(
        reservation_id=reservation_id,
        receipt=receipt,
        gateway_signature=None if gateway is None else sign(receipt, gateway),
        provider_attestation=attestation,
        provider_signature=(
            None if attestation is None or provider_key is None else sign(attestation, provider_key)
        ),
        delivered_response_digest=delivered,
        request=p.request,
        quote=p.quote,
        service_class=p.service_class,
        execution_profile=p.execution_profile,
    )
