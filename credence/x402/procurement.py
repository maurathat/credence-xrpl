"""Map an x402 offer onto Core's procurement objects so Core's own policy engine decides it.

Nothing here decides eligibility. An x402 offer becomes an ExecutionProfile, a ServiceClass and a
Quote; the caller signs a PurchaseRequest for it as for any other provider; Core's
`policy_engine.evaluate` (through `credence.inference.market.qualify_and_select`, unchanged) decides
it beside Nebius and local Qwen candidates. Frozen procurement rules are not touched or bypassed.

Faithfulness limits (contracts/requests/0009):

- Price. Core quotes are per-token (`input/output_price_per_million_micro_trlusd` with token caps).
  An x402 merchant charges one fixed amount per request. The mapping is a *policy projection*:
  input price = the flat amount per FLAT_PRICE_UNITS units, cap = FLAT_PRICE_UNITS units, output
  price 0 and cap 0. Core's max_total_cost_value of the projected quote is exactly the flat amount,
  so PRICE_ABOVE_MAX and OVER_LIMIT are decided on the true price. The projected quote's token caps
  are not the merchant's token limits and it must never be billed or verified against token counts.
  `X402Projection.billing_faithful` is False to say so.
- Money. Core money is MicroTrlusd (tRLUSD, a test token). Only an offer whose observed asset is
  Credence's approved tRLUSD asset and whose value is an exact whole number of micro-units maps.
  RLUSD on Mainnet, or any other asset, is never converted into MicroTrlusd: it is refused with
  ASSET_NOT_APPROVED / ISSUER_NOT_APPROVED before Core is asked.
- Location. Core requires an asserted jurisdiction and a non-empty data residency to match any
  procurement class. An x402 listing rarely states them. Unless the operator asserts them for the
  merchant, they stay null/empty and Core rejects the offer with PROFILE_NOT_ALLOWED. That is the
  correct outcome, not a defect to work around.
- Evidence. Paying through x402 adds no evidence. With no merchant attestation that Credence can
  verify, the most an x402 offer can be is Tier 1 (Credence witnessed the merchant's response).
  `verified_evidence_tier` is capped at WITNESSED; a Tier-2 facility rejects it with TIER_TOO_LOW.

Owner: x402 (X402-P0).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from credence.core.billing import max_total_cost_value
from credence.core.objects import ExecutionProfile, Quote, SamplingPolicy, ServiceClass
from credence.core.primitives import (
    CountryCode,
    DeliveryMode,
    EvidenceTier,
    MicroTrlusd,
    ProviderId,
    QuoteId,
    SeedPolicy,
    ServiceTier,
    Timestamp,
)
from credence.x402.errors import X402Refusal, X402Refused
from credence.x402.proposal import XrplAmount

FLAT_PRICE_UNITS = 1_000_000
MAX_X402_EVIDENCE_TIER = EvidenceTier.WITNESSED


@dataclass(frozen=True)
class ApprovedExecution:
    """Credence's own approved execution network and asset, kept apart from what a merchant
    advertises. Under X402-P0 nothing is submitted on any network: `submission_authorized` is False.
    """

    network: str
    network_id: int
    currency: str
    issuer: str
    label: str = "tRLUSD"

    @property
    def submission_authorized(self) -> bool:
        return False


@dataclass(frozen=True)
class OperatorAssertions:
    """Facts about a merchant that only the operator can assert (not the merchant or a directory)."""

    jurisdiction: CountryCode | None = None
    data_residency: tuple[CountryCode, ...] = ()
    context_min_tokens: int = 0


@dataclass(frozen=True)
class X402Offer:
    merchant_id: ProviderId
    service_url: str
    method: str
    model_identifier: str
    model_family: str
    price: XrplAmount
    network: str
    destination: str
    valid_until: Timestamp
    verified_evidence_tier: EvidenceTier
    assertions: OperatorAssertions
    payment_before_delivery: bool = True


@dataclass(frozen=True)
class X402Projection:
    offer: X402Offer
    execution_profile: ExecutionProfile
    service_class: ServiceClass
    quote: Quote
    price_micro_trlusd: MicroTrlusd
    billing_faithful: bool = False


def micro_trlusd(price: XrplAmount, approved: ApprovedExecution) -> MicroTrlusd:
    """The observed price in Credence's accounting unit, only if it is the approved tRLUSD asset and
    an exact whole number of micro-units. Never converts another asset."""
    if price.currency != approved.currency:
        raise X402Refused(X402Refusal.ASSET_NOT_APPROVED, price.currency)
    if price.issuer != approved.issuer:
        raise X402Refused(X402Refusal.ISSUER_NOT_APPROVED, str(price.issuer))
    try:
        micro = Decimal(price.value).scaleb(6)
    except InvalidOperation:
        raise X402Refused(X402Refusal.MALFORMED_AMOUNT, "value") from None
    if not micro.is_finite() or micro != micro.to_integral_value() or micro <= 0:
        raise X402Refused(X402Refusal.AMOUNT_NOT_REPRESENTABLE, price.value)
    return MicroTrlusd(int(micro))


def project(offer: X402Offer, approved: ApprovedExecution, quote_id: QuoteId) -> X402Projection:
    """The Core objects Core's policy engine evaluates for this offer."""
    if offer.network != approved.network:
        raise X402Refused(X402Refusal.NETWORK_NOT_APPROVED, offer.network)
    price = micro_trlusd(offer.price, approved)
    tier = offer.verified_evidence_tier
    if tier.value > MAX_X402_EVIDENCE_TIER.value:
        raise X402Refused(X402Refusal.POLICY_REJECTED, "x402 payment adds no Tier-2 evidence")
    profile = ExecutionProfile(
        model_identifier=offer.model_identifier,
        model_artifact_digest=None,
        tokenizer_digest=None,
        adapter_digest=None,
        chat_template_digest=None,
        runtime=None,
        precision=None,
        quantization=None,
        context_min_tokens=offer.assertions.context_min_tokens,
        sampling=SamplingPolicy(temperature_milli=None, seed_policy=SeedPolicy.PROVIDER, seed=None),
    )
    service_class = ServiceClass(
        execution_profile_digest=profile.digest(),
        model_family=offer.model_family,
        delivery_mode=DeliveryMode.INTERACTIVE,
        service_tier=ServiceTier.BASE,
        jurisdiction=offer.assertions.jurisdiction,
        data_residency=offer.assertions.data_residency,
        residency_region=None,
        evidence_tier=tier,
        latency_p95_ms=None,
        availability_bps=None,
    )
    quote = Quote(
        quote_id=quote_id,
        provider_id=offer.merchant_id,
        service_class_digest=service_class.digest(),
        input_price_per_million_micro_trlusd=price,
        output_price_per_million_micro_trlusd=MicroTrlusd(0),
        max_input_tokens=FLAT_PRICE_UNITS,
        max_output_tokens=0,
        valid_until=offer.valid_until,
    )
    if max_total_cost_value(quote) != price.value:
        raise AssertionError("flat-price projection does not reproduce the merchant price")
    return X402Projection(offer, profile, service_class, quote, price)
