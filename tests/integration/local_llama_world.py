"""A real local Llama 3.1 8B Instruct world for the Inference Checkpoint 2 integration tests and
the runtime-observation recorder.

Needs Ollama on loopback with llama3.1:8b-instruct-fp16 installed, and CREDENCE_LOCAL_LLAMA=1.
The provider's signing key is generated at runtime and never written anywhere; the agent and
gateway use the deterministic TEST keys from tests/unit/inference_world.py (NOT FOR PRODUCTION).
Commercial terms (prices) are synthetic test values in micro-tRLUSD (tRLUSD is a test token).
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
    LOCAL_PROVIDER_ID,
    BoundArtifact,
    LocalLlamaProvider,
    LoopbackOllamaTransport,
    bind_local_artifact,
)
from credence.inference.provider import InferenceProvider  # noqa: E402
from credence.inference.receipt_state import ReceiptChain, SettledReceipts  # noqa: E402

ENABLE_ENV = "CREDENCE_LOCAL_LLAMA"
TAG = "llama3.1:8b-instruct-fp16"
MODEL_FAMILY = "llama-3.1-8b-instruct"
MAX_OUTPUT_TOKENS = 48
PROMPT = b"Synthetic Credence test prompt: reply with exactly the word OK and nothing else."


def enabled() -> bool:
    return os.environ.get(ENABLE_ENV) == "1"


def wall_clock() -> Timestamp:
    return Timestamp(datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))


def scenario_for(profile: ExecutionProfile) -> Scenario:
    """A controlled Tier 2 purchase of exactly `profile` from the local provider."""
    service = ServiceClass(
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
    procurement = ProcurementClass(
        model_family=MODEL_FAMILY,
        allowed_precisions=("F16",),
        allowed_quantizations=("none",),
        minimum_evidence_tier=EvidenceTier.BOUND_AND_SIGNED,
        delivery_mode=DeliveryMode.INTERACTIVE,
        allowed_jurisdictions=(US,),
        allowed_data_residencies=(US,),
        allowed_providers=(LOCAL_PROVIDER_ID,),
    )
    facility = Facility(
        facility_id=FacilityId("facility-local-llama-tier2"),
        borrower_account=BORROWER,
        xrpl_loan_id=None,
        limit_micro_trlusd=MicroTrlusd(10_000_000),
        drawn_micro_trlusd=MicroTrlusd(0),
        term_seconds=86_400,
        maturity_at=FAR_FUTURE,
        spread_bps=600,
        allowed_providers=(LOCAL_PROVIDER_ID,),
        allowed_procurement_classes=(procurement.digest(),),
        minimum_evidence_tier=EvidenceTier.BOUND_AND_SIGNED,
        purpose=Purpose.INFERENCE,
        owner_approval_digest=synthetic_digest("owner-approval:facility-local-llama-tier2"),
        status=FacilityStatus.ACTIVE,
    )
    quote = Quote(
        quote_id=QuoteId("quote-local-llama-0001"),
        provider_id=LOCAL_PROVIDER_ID,
        service_class_digest=service.digest(),
        input_price_per_million_micro_trlusd=MicroTrlusd(100_000),
        output_price_per_million_micro_trlusd=MicroTrlusd(400_000),
        max_input_tokens=4096,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        valid_until=FAR_FUTURE,
    )
    return Scenario(facility, quote, service, profile, procurement)


@dataclass
class LocalWorld:
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
                    LOCAL_PROVIDER_ID.value,
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


def build_local_world() -> LocalWorld:
    """Bind the installed artifact (hashes the ~16 GB GGUF) and generate a provider key."""
    transport = LoopbackOllamaTransport()
    bound = bind_local_artifact(TAG, transport=transport)
    provider_key = SigningKey.generate()
    provider = LocalLlamaProvider(bound, transport=transport, signing_key=provider_key)
    return LocalWorld(bound, transport, provider_key, provider, scenario_for(bound.profile))
