"""A synthetic, deterministic world for the Inference Checkpoint 1 tests and fixtures.

Every object here is synthetic: model names, digests, accounts and prompts are made up and harmless.
TEST keys (NOT FOR PRODUCTION) are derived at runtime from public labels, as in Core's c3a_keys,
so nothing secret is committed; only public key IDs and signatures ever reach a file.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from pathlib import Path

from nacl.signing import SigningKey
from xrpl.core.addresscodec import encode_classic_address

from credence.core.billing import max_total_cost_value
from credence.core.objects import (
    ExecutionProfile,
    Facility,
    ProcurementClass,
    PurchaseRequest,
    Quote,
    SamplingPolicy,
    ServiceClass,
    SignedObject,
)
from credence.core.primitives import (
    CountryCode,
    DeliveryMode,
    EvidenceTier,
    FacilityId,
    FacilityStatus,
    MicroTrlusd,
    Nonce,
    Purpose,
    QuoteId,
    SeedPolicy,
    ServiceTier,
    Sha256Digest,
    Timestamp,
    XrplAccount,
)
from credence.core.reservation_ledger import ReservationLedger
from credence.core.signatures import (
    KeyRegistry,
    KeyRole,
    KeyStatus,
    RegisteredKey,
    key_id_for,
    sign,
)
from credence.inference.fake_providers import (
    FAKE_TIER1_PROVIDER_ID,
    FAKE_TIER2_PROVIDER_ID,
    FakeSigningProvider,
    FakeWitnessedProvider,
)
from credence.inference.gateway import Gateway, Purchase
from credence.inference.payload import payload_digest
from credence.inference.provider import InferenceProvider
from credence.inference.receipt_state import ReceiptChain, SettledReceipts

TEST_KEY_PREFIX = b"CREDENCE INFERENCE TEST KEY - NOT FOR PRODUCTION - "


def make_test_key(label: str) -> SigningKey:
    return SigningKey(hashlib.sha256(TEST_KEY_PREFIX + label.encode("ascii")).digest())


def synthetic_digest(label: str) -> Sha256Digest:
    return Sha256Digest.from_hex(
        hashlib.sha256(f"credence-inference-c1:{label}".encode()).hexdigest()
    )


AGENT_KEY = make_test_key("agent-working-key")
GATEWAY_KEY = make_test_key("gateway")
TIER2_PROVIDER_KEY = make_test_key("synthetic-signing-provider")
TIER1_PROVIDER_KEY = make_test_key("synthetic-witnessed-provider")  # registered, never used to sign
ATTACKER_KEY = make_test_key("attacker-unregistered")

GATEWAY_SUBJECT = "credence-gateway"
BORROWER = XrplAccount(
    encode_classic_address(hashlib.sha256(b"credence-inference-c1:borrower").digest()[:20])
)

NOW = Timestamp("2026-10-24T15:00:00Z")
FAR_FUTURE = Timestamp("2026-12-31T00:00:00Z")
US = CountryCode("US")
MODEL_FAMILY = "synthetic-model-8b"

# Provider A-style: the model name is declared; everything else is NOT ASSERTED (null).
PROFILE_TIER1 = ExecutionProfile(
    model_identifier="synthetic-model-8b-instruct",
    model_artifact_digest=None,
    tokenizer_digest=None,
    adapter_digest=None,
    chat_template_digest=None,
    runtime=None,
    precision=None,
    quantization=None,
    context_min_tokens=32768,
    sampling=SamplingPolicy(temperature_milli=None, seed_policy=SeedPolicy.PROVIDER, seed=None),
)

# Tier 2: every controlled field asserted (synthetic digests; no weights file exists).
PROFILE_TIER2 = ExecutionProfile(
    model_identifier="synthetic-model-8b-instruct",
    model_artifact_digest=synthetic_digest("weights"),
    tokenizer_digest=synthetic_digest("tokenizer"),
    adapter_digest=None,
    chat_template_digest=synthetic_digest("chat-template"),
    runtime="synthetic-runtime 0.0.0",
    precision="BF16",
    quantization="none",
    context_min_tokens=32768,
    sampling=SamplingPolicy(temperature_milli=0, seed_policy=SeedPolicy.FIXED, seed=7),
)


def service_class(profile: ExecutionProfile, tier: EvidenceTier) -> ServiceClass:
    return ServiceClass(
        execution_profile_digest=profile.digest(),
        model_family=MODEL_FAMILY,
        delivery_mode=DeliveryMode.INTERACTIVE,
        service_tier=ServiceTier.BASE,
        jurisdiction=US,
        data_residency=(US,),
        residency_region=None,
        evidence_tier=tier,
        latency_p95_ms=None,
        availability_bps=None,
    )


SERVICE_TIER1 = service_class(PROFILE_TIER1, EvidenceTier.WITNESSED)
SERVICE_TIER2 = service_class(PROFILE_TIER2, EvidenceTier.BOUND_AND_SIGNED)

BOTH_PROVIDERS = tuple(sorted((FAKE_TIER1_PROVIDER_ID, FAKE_TIER2_PROVIDER_ID)))

# The C2A general class: precision and quantization unconstrained (null), Tier 1 floor.
GENERAL_TIER1 = ProcurementClass(
    model_family=MODEL_FAMILY,
    allowed_precisions=None,
    allowed_quantizations=None,
    minimum_evidence_tier=EvidenceTier.WITNESSED,
    delivery_mode=DeliveryMode.INTERACTIVE,
    allowed_jurisdictions=(US,),
    allowed_data_residencies=(US,),
    allowed_providers=BOTH_PROVIDERS,
)

# The C2A controlled class: BF16 and no quantization required, Tier 2 floor.
CONTROLLED_TIER2 = ProcurementClass(
    model_family=MODEL_FAMILY,
    allowed_precisions=("BF16",),
    allowed_quantizations=("none",),
    minimum_evidence_tier=EvidenceTier.BOUND_AND_SIGNED,
    delivery_mode=DeliveryMode.INTERACTIVE,
    allowed_jurisdictions=(US,),
    allowed_data_residencies=(US,),
    allowed_providers=BOTH_PROVIDERS,
)


def facility(
    facility_id: str, procurement: ProcurementClass, minimum_tier: EvidenceTier
) -> Facility:
    return Facility(
        facility_id=FacilityId(facility_id),
        borrower_account=BORROWER,
        xrpl_loan_id=None,
        limit_micro_trlusd=MicroTrlusd(10_000_000),  # 10 tRLUSD (a test token)
        drawn_micro_trlusd=MicroTrlusd(0),
        term_seconds=86_400,
        maturity_at=FAR_FUTURE,
        spread_bps=600,
        allowed_providers=BOTH_PROVIDERS,
        allowed_procurement_classes=(procurement.digest(),),
        minimum_evidence_tier=minimum_tier,
        purpose=Purpose.INFERENCE,
        owner_approval_digest=synthetic_digest(f"owner-approval:{facility_id}"),
        status=FacilityStatus.ACTIVE,
    )


FACILITY_TIER1 = facility("facility-synthetic-tier1", GENERAL_TIER1, EvidenceTier.WITNESSED)
FACILITY_TIER2 = facility(
    "facility-synthetic-tier2", CONTROLLED_TIER2, EvidenceTier.BOUND_AND_SIGNED
)

QUOTE_TIER1 = Quote(
    quote_id=QuoteId("quote-synthetic-tier1-0001"),
    provider_id=FAKE_TIER1_PROVIDER_ID,
    service_class_digest=SERVICE_TIER1.digest(),
    input_price_per_million_micro_trlusd=MicroTrlusd(200_000),
    output_price_per_million_micro_trlusd=MicroTrlusd(800_000),
    max_input_tokens=4096,
    max_output_tokens=1024,
    valid_until=FAR_FUTURE,
)
QUOTE_TIER2 = Quote(
    quote_id=QuoteId("quote-synthetic-tier2-0001"),
    provider_id=FAKE_TIER2_PROVIDER_ID,
    service_class_digest=SERVICE_TIER2.digest(),
    input_price_per_million_micro_trlusd=MicroTrlusd(150_000),
    output_price_per_million_micro_trlusd=MicroTrlusd(600_000),
    max_input_tokens=4096,
    max_output_tokens=1024,
    valid_until=FAR_FUTURE,
)


def prompt(index: int) -> bytes:
    return f"Synthetic prompt {index}: list three facts about the number {index}.".encode()


@dataclass(frozen=True)
class Scenario:
    facility: Facility
    quote: Quote
    service_class: ServiceClass
    profile: ExecutionProfile
    procurement: ProcurementClass


TIER1 = Scenario(FACILITY_TIER1, QUOTE_TIER1, SERVICE_TIER1, PROFILE_TIER1, GENERAL_TIER1)
TIER2 = Scenario(FACILITY_TIER2, QUOTE_TIER2, SERVICE_TIER2, PROFILE_TIER2, CONTROLLED_TIER2)


def make_purchase(
    scenario: Scenario,
    index: int = 1,
    *,
    signer: SigningKey | None = AGENT_KEY,
    request_prompt: bytes | None = None,
    sent_prompt: bytes | None = None,
) -> Purchase:
    """A purchase signed by `signer` (None: unsigned). The request commits to `request_prompt`;
    the gateway is handed `sent_prompt` (both default to prompt(index))."""
    committed = prompt(index) if request_prompt is None else request_prompt
    request = PurchaseRequest(
        facility_id=scenario.facility.facility_id,
        provider_id=scenario.quote.provider_id,
        quote_id=scenario.quote.quote_id,
        purpose=Purpose.INFERENCE,
        max_total_cost_micro_trlusd=MicroTrlusd(max_total_cost_value(scenario.quote)),
        prompt_digest=payload_digest(committed),
        nonce=Nonce(f"{index:032x}"),
        expires_at=FAR_FUTURE,
    )
    signature: SignedObject | None = None if signer is None else sign(request, signer)
    return Purchase(
        request=request,
        request_signature=signature,
        prompt=committed if sent_prompt is None else sent_prompt,
        facility=scenario.facility,
        quote=scenario.quote,
        service_class=scenario.service_class,
        execution_profile=scenario.profile,
        procurement_classes=(scenario.procurement,),
    )


def with_facility(purchase: Purchase, **changes: object) -> Purchase:
    """The same signed purchase against a changed facility (same facility ID)."""
    return replace(purchase, facility=replace(purchase.facility, **changes))  # type: ignore[arg-type]


def _registered(key: SigningKey, role: KeyRole, subject: str) -> RegisteredKey:
    return RegisteredKey(key_id_for(key.verify_key), role, subject, KeyStatus.CURRENT)


REGISTERED_KEYS: tuple[RegisteredKey, ...] = (
    _registered(AGENT_KEY, KeyRole.AGENT, BORROWER.value),
    _registered(GATEWAY_KEY, KeyRole.GATEWAY, GATEWAY_SUBJECT),
    _registered(TIER2_PROVIDER_KEY, KeyRole.PROVIDER, FAKE_TIER2_PROVIDER_ID.value),
    _registered(TIER1_PROVIDER_KEY, KeyRole.PROVIDER, FAKE_TIER1_PROVIDER_ID.value),
)


def registry() -> KeyRegistry:
    return KeyRegistry(REGISTERED_KEYS)


class StepClock:
    """Deterministic clock: each call returns the next whole second from `start`."""

    def __init__(self, start: Timestamp = NOW) -> None:
        self._next = start

    def __call__(self) -> Timestamp:
        current = self._next
        self._next = current.plus_seconds(1)
        return current


def tier1_provider() -> FakeWitnessedProvider:
    return FakeWitnessedProvider(PROFILE_TIER1)


def tier2_provider() -> FakeSigningProvider:
    return FakeSigningProvider(PROFILE_TIER2, TIER2_PROVIDER_KEY)


def make_gateway(
    db_dir: Path,
    *providers: InferenceProvider,
    chain: ReceiptChain | None = None,
    settled: SettledReceipts | None = None,
    clock: StepClock | None = None,
) -> Gateway:
    return Gateway(
        ledger=ReservationLedger(db_dir / "reservations.sqlite3"),
        registry=registry(),
        gateway_key=GATEWAY_KEY,
        providers=providers,
        clock=StepClock() if clock is None else clock,
        chain=chain,
        settled=settled,
    )
