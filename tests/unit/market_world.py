"""Offline two-provider Qwen world for Inference Checkpoint 3: no network, no credentials.

Shared model family: qwen3-30b-a3b-instruct-2507. Provider B uses the REAL local Qwen Q8_0
ExecutionProfile recorded in Checkpoint 3 (contracts/fixtures/inference_c3/
local-qwen-tier2-observation.json), served here by the deterministic fake signing provider, so no
model is needed. Provider A is the real NebiusProvider adapter, with the real Nebius model ID and
its NOT ASSERTED profile, over a fake transport returning the documented chat-completion shape.

Quotes are configured DEMO prices in micro-tRLUSD (tRLUSD is a test token), not Nebius's billed
price; the Nebius quote is deliberately cheaper so price-vs-evidence outcomes are visible. Only
procurement requirements and quotes differ between scenarios; no provider evidence is altered.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from inference_world import (
    AGENT_KEY,
    BORROWER,
    FAR_FUTURE,
    GATEWAY_KEY,
    GATEWAY_SUBJECT,
    US,
    Scenario,
    StepClock,
    make_purchase,
    make_test_key,
    prompt,
    synthetic_digest,
)

from credence.core.objects import (
    ExecutionProfile,
    Facility,
    ProcurementClass,
    Quote,
    ServiceClass,
)
from credence.core.primitives import (
    CountryCode,
    DeliveryMode,
    EvidenceTier,
    FacilityId,
    FacilityStatus,
    MicroTrlusd,
    ProviderId,
    Purpose,
    QuoteId,
    ServiceTier,
)
from credence.core.reservation_ledger import ReservationLedger
from credence.core.signatures import KeyRegistry, KeyRole, KeyStatus, RegisteredKey, key_id_for
from credence.inference.fake_providers import FakeSigningProvider
from credence.inference.gateway import Gateway, Purchase
from credence.inference.local_llama import LOCAL_QWEN_PROVIDER_ID as LOCAL_PROVIDER_ID
from credence.inference.nebius import NEBIUS_PROVIDER_ID, NebiusProvider, nebius_profile
from credence.inference.provider import InferenceProvider

OBSERVATION = (
    Path(__file__).resolve().parents[2]
    / "contracts"
    / "fixtures"
    / "inference_c3"
    / "local-qwen-tier2-observation.json"
)
MODEL_FAMILY = "qwen3-30b-a3b-instruct-2507"
NEBIUS_MODEL = "Qwen/Qwen3-30B-A3B-Instruct-2507"
LOCAL_TEST_KEY = make_test_key("local-qwen-provider-offline")

LOCAL_PROFILE = ExecutionProfile.from_canonical(
    json.loads(OBSERVATION.read_text(encoding="ascii"))["objects"]["execution_profile"]
)
NEBIUS_PROFILE = nebius_profile(NEBIUS_MODEL, context_min_tokens=8192)
PROVIDERS = tuple(sorted((LOCAL_PROVIDER_ID, NEBIUS_PROVIDER_ID)))


# Service location (Inference contract, Service location): jurisdiction is the country where the
# service's inference executes. Nebius: the country code of its declared service region
# (FI, eu-north1; Nebius-declared catalog metadata). Local: the operator-supplied deployment fact
# that the execution machine operates in the US. Neither is a legal or corporate jurisdiction.
FI = CountryCode("FI")
NEBIUS_LOCATION = (FI, (FI,))
LOCAL_LOCATION = (US, (US,))
BUYER_LOCATIONS = (FI, US)  # a buyer allowlist, not a claim that any provider runs in both


def service(
    profile: ExecutionProfile,
    tier: EvidenceTier,
    location: tuple[CountryCode, tuple[CountryCode, ...]],
) -> ServiceClass:
    return ServiceClass(
        execution_profile_digest=profile.digest(),
        model_family=MODEL_FAMILY,
        delivery_mode=DeliveryMode.INTERACTIVE,
        service_tier=ServiceTier.BASE,
        jurisdiction=location[0],
        data_residency=location[1],
        residency_region=None,
        evidence_tier=tier,
        latency_p95_ms=None,
        availability_bps=None,
    )


LOCAL_SERVICE = service(LOCAL_PROFILE, EvidenceTier.BOUND_AND_SIGNED, LOCAL_LOCATION)
NEBIUS_SERVICE = service(NEBIUS_PROFILE, EvidenceTier.WITNESSED, NEBIUS_LOCATION)


def procurement(
    precisions: tuple[str, ...] | None, quantizations: tuple[str, ...] | None, tier: EvidenceTier
) -> ProcurementClass:
    return ProcurementClass(
        model_family=MODEL_FAMILY,
        allowed_precisions=precisions,
        allowed_quantizations=quantizations,
        minimum_evidence_tier=tier,
        delivery_mode=DeliveryMode.INTERACTIVE,
        allowed_jurisdictions=BUYER_LOCATIONS,
        allowed_data_residencies=BUYER_LOCATIONS,
        allowed_providers=PROVIDERS,
    )


GENERAL = procurement(None, None, EvidenceTier.WITNESSED)
# Requires the local artifact's own asserted representation: quantization Q8_0 (precision is not
# asserted for Q8_0, so it is left unconstrained), and Tier 2.
CONTROLLED = procurement(None, ("Q8_0",), EvidenceTier.BOUND_AND_SIGNED)


def facility(name: str, procurement_class: ProcurementClass, tier: EvidenceTier) -> Facility:
    return Facility(
        facility_id=FacilityId(name),
        borrower_account=BORROWER,
        xrpl_loan_id=None,
        limit_micro_trlusd=MicroTrlusd(10_000_000),
        drawn_micro_trlusd=MicroTrlusd(0),
        term_seconds=86_400,
        maturity_at=FAR_FUTURE,
        spread_bps=600,
        allowed_providers=PROVIDERS,
        allowed_procurement_classes=(procurement_class.digest(),),
        minimum_evidence_tier=tier,
        purpose=Purpose.INFERENCE,
        owner_approval_digest=synthetic_digest(f"owner-approval:{name}"),
        status=FacilityStatus.ACTIVE,
    )


GENERAL_FACILITY = facility("facility-market-general", GENERAL, EvidenceTier.WITNESSED)
CONTROLLED_FACILITY = facility(
    "facility-market-controlled", CONTROLLED, EvidenceTier.BOUND_AND_SIGNED
)


def quote(provider: ProviderId, svc: ServiceClass, input_price: int, output_price: int) -> Quote:
    return Quote(
        quote_id=QuoteId(f"quote-{provider.value}-0001"),
        provider_id=provider,
        service_class_digest=svc.digest(),
        input_price_per_million_micro_trlusd=MicroTrlusd(input_price),
        output_price_per_million_micro_trlusd=MicroTrlusd(output_price),
        max_input_tokens=4096,
        max_output_tokens=64,
        valid_until=FAR_FUTURE,
    )


# Configured demo prices (not Nebius billing rates): Nebius is cheaper on purpose.
NEBIUS_QUOTE = quote(NEBIUS_PROVIDER_ID, NEBIUS_SERVICE, 20_000, 60_000)
LOCAL_QUOTE = quote(LOCAL_PROVIDER_ID, LOCAL_SERVICE, 100_000, 400_000)


def candidates(
    facility_: Facility, procurement_class: ProcurementClass, index: int = 1
) -> list[Purchase]:
    """One signed purchase per offered quote, for the same job (same prompt and facility)."""
    out = []
    for nonce_offset, (svc, prof, q) in enumerate(
        (
            (NEBIUS_SERVICE, NEBIUS_PROFILE, NEBIUS_QUOTE),
            (LOCAL_SERVICE, LOCAL_PROFILE, LOCAL_QUOTE),
        )
    ):
        scenario = Scenario(facility_, q, svc, prof, procurement_class)
        out.append(make_purchase(scenario, index * 10 + nonce_offset, request_prompt=prompt(index)))
    return out


class FakeNebiusTransport:
    """Returns the documented non-streaming chat-completion shape. Counts calls."""

    def __init__(
        self, reply: Mapping[str, object] | None = None, model: str = NEBIUS_MODEL
    ) -> None:
        self.reply = reply
        self.model = model
        self.posts: list[tuple[str, Mapping[str, object]]] = []

    def get(self, path: str) -> Mapping[str, object]:
        raise AssertionError("not used")

    def post(self, path: str, body: Mapping[str, object]) -> Mapping[str, object]:
        self.posts.append((path, body))
        if self.reply is not None:
            return self.reply
        return {
            "id": "chatcmpl-synthetic",
            "object": "chat.completion",
            "created": 0,
            "model": self.model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Synthetic Nebius-shaped reply."},
                    "finish_reason": "stop",
                    "logprobs": None,
                }
            ],
            "usage": {"prompt_tokens": 21, "completion_tokens": 6, "total_tokens": 27},
            "service_tier": "default",
        }


@dataclass
class MarketWorld:
    nebius_transport: FakeNebiusTransport
    nebius: NebiusProvider
    local: FakeSigningProvider

    def registry(self) -> KeyRegistry:
        return KeyRegistry(
            [
                RegisteredKey(
                    key_id_for(AGENT_KEY.verify_key),
                    KeyRole.AGENT,
                    BORROWER.value,
                    KeyStatus.CURRENT,
                ),
                RegisteredKey(
                    key_id_for(GATEWAY_KEY.verify_key),
                    KeyRole.GATEWAY,
                    GATEWAY_SUBJECT,
                    KeyStatus.CURRENT,
                ),
                RegisteredKey(
                    key_id_for(LOCAL_TEST_KEY.verify_key),
                    KeyRole.PROVIDER,
                    LOCAL_PROVIDER_ID.value,
                    KeyStatus.CURRENT,
                ),
            ]
        )

    def gateway(self, db_dir: Path, *providers: InferenceProvider) -> Gateway:
        return Gateway(
            ledger=ReservationLedger(db_dir / "reservations.sqlite3"),
            registry=self.registry(),
            gateway_key=GATEWAY_KEY,
            providers=providers or (self.nebius, self.local),
            clock=StepClock(),
        )


def market_world(reply: Mapping[str, object] | None = None) -> MarketWorld:
    transport = FakeNebiusTransport(reply)
    return MarketWorld(
        transport,
        NebiusProvider(NEBIUS_PROFILE, transport=transport),
        FakeSigningProvider(LOCAL_PROFILE, LOCAL_TEST_KEY, provider_id=LOCAL_PROVIDER_ID),
    )
