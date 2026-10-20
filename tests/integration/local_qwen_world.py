"""A real local Qwen3 30B A3B Instruct 2507 (Q8_0) world for Inference Checkpoint 3.

Needs Ollama on loopback with qwen3:30b-a3b-instruct-2507-q8_0 installed and
CREDENCE_LOCAL_QWEN=1. The provider key is generated at runtime and never written; the agent and
gateway use the deterministic TEST keys from tests/unit/inference_world.py (NOT FOR PRODUCTION).

Service location: jurisdiction US, residency {US}, from Maura's operator-supplied deployment fact
that the execution machine physically operates in the United States (Inference contract, Service
location). Prices are configured demo values in micro-tRLUSD (tRLUSD is a test token).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from nacl.signing import SigningKey

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))

from inference_world import (  # noqa: E402
    AGENT_KEY,
    BORROWER,
    FAR_FUTURE,
    GATEWAY_KEY,
    GATEWAY_SUBJECT,
    US,
    Scenario,
    synthetic_digest,
)

from credence.core.objects import (  # noqa: E402
    ExecutionProfile,
    Facility,
    ProcurementClass,
    Quote,
    ServiceClass,
)
from credence.core.primitives import (  # noqa: E402
    CountryCode,
    DeliveryMode,
    EvidenceTier,
    FacilityId,
    FacilityStatus,
    MicroTrlusd,
    Purpose,
    QuoteId,
    ServiceTier,
    Timestamp,
)
from credence.core.reservation_ledger import ReservationLedger  # noqa: E402
from credence.core.signatures import (  # noqa: E402
    KeyRegistry,
    KeyRole,
    KeyStatus,
    RegisteredKey,
    key_id_for,
)
from credence.inference.gateway import Gateway  # noqa: E402
from credence.inference.local_llama import (  # noqa: E402
    LOCAL_QWEN_PROVIDER_ID,
    BoundArtifact,
    LocalLlamaProvider,
    LoopbackOllamaTransport,
    bind_local_artifact,
)
from credence.inference.nebius import NEBIUS_PROVIDER_ID  # noqa: E402
from credence.inference.provider import InferenceProvider  # noqa: E402
from credence.inference.receipt_state import ReceiptChain, SettledReceipts  # noqa: E402

ENABLE_ENV = "CREDENCE_LOCAL_QWEN"
TAG = "qwen3:30b-a3b-instruct-2507-q8_0"
MODEL_FAMILY = "qwen3-30b-a3b-instruct-2507"
MAX_OUTPUT_TOKENS = 48
PROMPT = b"Synthetic Credence test prompt: reply with exactly the word OK and nothing else."
FI = CountryCode("FI")
BUYER_LOCATIONS = (FI, US)  # a buyer allowlist, not a claim that a provider runs in both
MARKET_PROVIDERS = tuple(sorted((LOCAL_QWEN_PROVIDER_ID, NEBIUS_PROVIDER_ID)))


def enabled() -> bool:
    return os.environ.get(ENABLE_ENV) == "1"


def wall_clock() -> Timestamp:
    return Timestamp(datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))


def local_service(profile: ExecutionProfile) -> ServiceClass:
    return ServiceClass(
        execution_profile_digest=profile.digest(),
        model_family=MODEL_FAMILY,
        delivery_mode=DeliveryMode.INTERACTIVE,
        service_tier=ServiceTier.BASE,
        jurisdiction=US,
        data_residency=(US,),
        residency_region=None,
        evidence_tier=EvidenceTier.BOUND_AND_SIGNED,
        latency_p95_ms=None,
        availability_bps=None,
    )


def controlled_class(profile: ExecutionProfile) -> ProcurementClass:
    """Requires the local artifact's own asserted representation (quantization only) and Tier 2."""
    return ProcurementClass(
        model_family=MODEL_FAMILY,
        allowed_precisions=None if profile.precision is None else (profile.precision,),
        allowed_quantizations=None if profile.quantization is None else (profile.quantization,),
        minimum_evidence_tier=EvidenceTier.BOUND_AND_SIGNED,
        delivery_mode=DeliveryMode.INTERACTIVE,
        allowed_jurisdictions=BUYER_LOCATIONS,
        allowed_data_residencies=BUYER_LOCATIONS,
        allowed_providers=MARKET_PROVIDERS,
    )


def facility_for(procurement: ProcurementClass, name: str, tier: EvidenceTier) -> Facility:
    return Facility(
        facility_id=FacilityId(name),
        borrower_account=BORROWER,
        xrpl_loan_id=None,
        limit_micro_trlusd=MicroTrlusd(10_000_000),
        drawn_micro_trlusd=MicroTrlusd(0),
        term_seconds=86_400,
        maturity_at=FAR_FUTURE,
        spread_bps=600,
        allowed_providers=MARKET_PROVIDERS,
        allowed_procurement_classes=(procurement.digest(),),
        minimum_evidence_tier=tier,
        purpose=Purpose.INFERENCE,
        owner_approval_digest=synthetic_digest(f"owner-approval:{name}"),
        status=FacilityStatus.ACTIVE,
    )


def local_quote(service: ServiceClass) -> Quote:
    """Configured demo price; not a market price."""
    return Quote(
        quote_id=QuoteId("quote-local-qwen-0001"),
        provider_id=LOCAL_QWEN_PROVIDER_ID,
        service_class_digest=service.digest(),
        input_price_per_million_micro_trlusd=MicroTrlusd(100_000),
        output_price_per_million_micro_trlusd=MicroTrlusd(400_000),
        max_input_tokens=4096,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        valid_until=FAR_FUTURE,
    )


def tier2_scenario(profile: ExecutionProfile) -> Scenario:
    service = local_service(profile)
    procurement = controlled_class(profile)
    facility = facility_for(procurement, "facility-local-qwen-tier2", EvidenceTier.BOUND_AND_SIGNED)
    return Scenario(facility, local_quote(service), service, profile, procurement)


@dataclass
class QwenWorld:
    bound: BoundArtifact
    transport: LoopbackOllamaTransport
    provider_key: SigningKey
    provider: LocalLlamaProvider
    scenario: Scenario

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
                    key_id_for(self.provider_key.verify_key),
                    KeyRole.PROVIDER,
                    LOCAL_QWEN_PROVIDER_ID.value,
                    KeyStatus.CURRENT,
                ),
            ]
        )

    def gateway(
        self,
        db_dir: Path,
        provider: InferenceProvider | None = None,
        *,
        chain: ReceiptChain | None = None,
        settled: SettledReceipts | None = None,
    ) -> Gateway:
        return Gateway(
            ledger=ReservationLedger(db_dir / "reservations.sqlite3"),
            registry=self.registry(),
            gateway_key=GATEWAY_KEY,
            providers=[self.provider if provider is None else provider],
            clock=wall_clock,
            chain=chain,
            settled=settled,
        )


def build_qwen_world() -> QwenWorld:
    """Bind the installed Q8_0 artifact (hashes the ~32 GB GGUF) and generate a provider key."""
    transport = LoopbackOllamaTransport()
    bound = bind_local_artifact(TAG, transport=transport)
    key = SigningKey.generate()
    provider = LocalLlamaProvider(
        bound, transport=transport, signing_key=key, provider_id=LOCAL_QWEN_PROVIDER_ID
    )
    return QwenWorld(bound, transport, key, provider, tier2_scenario(bound.profile))
