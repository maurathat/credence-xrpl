"""Inference CP3B live run: Nebius Qwen Tier 1 flow, Tier 1 attacks, and the two-provider market.

Run from the repository root, by the operator, with the key in a temporary subshell (it is never
passed on a command line, written, logged or printed):

    ( read -rs NEBIUS_API_KEY; export NEBIUS_API_KEY
      CREDENCE_NEBIUS_LIVE=1 uv run python contracts/fixtures/record_qwen_market_live.py )

Needs Ollama on loopback with qwen3:30b-a3b-instruct-2507-q8_0 installed. Makes EXACTLY ONE Nebius
inference call: market scenario A (general), where routing is expected to choose Nebius on price.
That one real delivery is also the Tier 1 gateway evidence, and the Tier 1 attacks reuse it in
memory. Scenario B (controlled) calls only the local model, because Core rejects Nebius before any
provider is called. A guard aborts the run rather than make a second Nebius call. Writes RUNTIME
OBSERVATION evidence (not deterministically regenerable):

    contracts/fixtures/inference_c3/nebius-qwen-tier1-observation.json
    contracts/fixtures/inference_c3/qwen-market-live-observation.json

Written: canonical objects, digests, public key IDs, signatures, decision records, Nebius usage
numbers, HTTP status, latency, synthetic prompts. Not written: the key, any header, response text,
private keys, model weights, machine paths. Nothing is settled; no XRPL transaction.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent / "inference_c3"
sys.path.insert(0, str(ROOT / "tests" / "integration"))
sys.path.insert(0, str(ROOT / "tests" / "unit"))

from inference_world import (  # noqa: E402
    AGENT_KEY,
    ATTACKER_KEY,
    BORROWER,
    FAR_FUTURE,
    GATEWAY_KEY,
    GATEWAY_SUBJECT,
    Scenario,
    make_purchase,
)
from local_qwen_world import (  # noqa: E402
    BUYER_LOCATIONS,
    MARKET_PROVIDERS,
    MAX_OUTPUT_TOKENS,
    MODEL_FAMILY,
    build_qwen_world,
    controlled_class,
    facility_for,
    local_quote,
    wall_clock,
)

from credence.core.billing import total_cost  # noqa: E402
from credence.core.objects import (  # noqa: E402
    CanonicalObject,
    ProcurementClass,
    ProviderAttestationBody,
    Quote,
    ServiceClass,
)
from credence.core.primitives import (  # noqa: E402
    CountryCode,
    DeliveryMode,
    EvidenceTier,
    MicroTrlusd,
    QuoteId,
    ServiceTier,
)
from credence.core.reservation_ledger import ReservationLedger  # noqa: E402
from credence.core.signatures import key_id_for, sign  # noqa: E402
from credence.core.verifier import VerifierInputError  # noqa: E402
from credence.inference.gateway import (  # noqa: E402
    DeliveryVerified,
    Gateway,
    GatewayOutcome,
    ProviderFailed,
)
from credence.inference.local_llama import LOCAL_QWEN_PROVIDER_ID  # noqa: E402
from credence.inference.market import MarketOutcome  # noqa: E402
from credence.inference.nebius import (  # noqa: E402
    NEBIUS_BASE_URL,
    NEBIUS_PROVIDER_ID,
    HttpsNebiusTransport,
    NebiusProvider,
    nebius_profile,
)
from credence.inference.provider import (  # noqa: E402
    AttestationCapability,
    ProviderAttestation,
    ProviderRequest,
    ProviderResult,
)
from credence.inference.receipt_state import ReceiptChain  # noqa: E402

NEBIUS_MODEL = "Qwen/Qwen3-30B-A3B-Instruct-2507"
FI = CountryCode("FI")
PROMPTS = {
    # Byte-identical to the prompt in the operator's successful exact-body probe request.
    "general": b"Synthetic Credence test prompt one: reply with exactly the word OK and nothing else.",
    "controlled": b"Synthetic Credence test prompt three: reply with exactly the word NO and nothing else.",
}


def _c(obj: CanonicalObject | None) -> Any:
    return None if obj is None else obj.to_canonical()


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def nebius_service(model_profile_digest: Any) -> ServiceClass:
    """Location: Nebius-declared service region eu-north1, country FI (not a legal jurisdiction)."""
    return ServiceClass(
        execution_profile_digest=model_profile_digest,
        model_family=MODEL_FAMILY,
        delivery_mode=DeliveryMode.INTERACTIVE,
        service_tier=ServiceTier.BASE,
        jurisdiction=FI,
        data_residency=(FI,),
        residency_region=None,
        evidence_tier=EvidenceTier.WITNESSED,
        latency_p95_ms=None,
        availability_bps=None,
    )


def nebius_quote(service: ServiceClass) -> Quote:
    """Configured DEMO price, deliberately cheaper than local. Not Nebius's billed price."""
    return Quote(
        quote_id=QuoteId("quote-nebius-qwen-0001"),
        provider_id=NEBIUS_PROVIDER_ID,
        service_class_digest=service.digest(),
        input_price_per_million_micro_trlusd=MicroTrlusd(20_000),
        output_price_per_million_micro_trlusd=MicroTrlusd(60_000),
        max_input_tokens=4096,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        valid_until=FAR_FUTURE,
    )


def general_class() -> ProcurementClass:
    return ProcurementClass(
        model_family=MODEL_FAMILY,
        allowed_precisions=None,
        allowed_quantizations=None,
        minimum_evidence_tier=EvidenceTier.WITNESSED,
        delivery_mode=DeliveryMode.INTERACTIVE,
        allowed_jurisdictions=BUYER_LOCATIONS,
        allowed_data_residencies=BUYER_LOCATIONS,
        allowed_providers=MARKET_PROVIDERS,
    )


class Replaying:
    """Returns an already-obtained real Nebius result, altered. Makes no network call."""

    attestation_capability = AttestationCapability.NONE
    provider_id = NEBIUS_PROVIDER_ID

    def __init__(self, result: ProviderResult) -> None:
        self._result = result

    def execute(self, request: ProviderRequest) -> ProviderResult:
        return self._result


def outcome_summary(outcome: GatewayOutcome | None) -> dict[str, Any]:
    if outcome is None:
        return {"outcome": None}
    summary: dict[str, Any] = {"outcome": type(outcome).__name__}
    if isinstance(outcome, DeliveryVerified):
        e = outcome.evidence
        summary.update(
            receipt=_c(e.receipt),
            receipt_digest=e.receipt.digest().value,
            gateway_signature=_c(e.gateway_signature),
            provider_attestation=_c(e.provider_attestation),
            provider_attestation_signature=_c(e.provider_signature),
            policy_decision=_c(outcome.policy_decision.record),
            verifier_decision=_c(outcome.decision.record),
            reservation=_c(outcome.reservation),
        )
    elif isinstance(outcome, ProviderFailed):
        summary.update(kind=outcome.kind.value, detail=outcome.detail)
    return summary


def selection_summary(result: MarketOutcome) -> dict[str, Any]:
    return {
        "qualifications": [
            {
                "provider_id": q.purchase.request.provider_id.value,
                "quote_id": q.purchase.request.quote_id.value,
                "qualified": q.qualified,
                "reason_code": None if q.reason_code is None else q.reason_code.value,
                "max_total_cost_micro_trlusd": q.max_total_cost,
                "dry_run_policy_decision": _c(q.decision.record),
                "purchase_request": _c(q.purchase.request),
                "purchase_request_signature": _c(q.purchase.request_signature),
                "currently_reserved_micro_trlusd": q.currently_reserved.value,
                "nonce_already_used": q.nonce_already_used,
            }
            for q in result.selection.qualifications
        ],
        "qualifying_count": result.selection.qualifying_count,
        "chosen_provider_id": None
        if result.selection.chosen is None
        else result.selection.chosen.purchase.request.provider_id.value,
        "delivery": outcome_summary(result.outcome),
    }


def main() -> None:
    if os.environ.get("CREDENCE_NEBIUS_LIVE") != "1":
        raise SystemExit("set CREDENCE_NEBIUS_LIVE=1 (and NEBIUS_API_KEY) to call Nebius")
    transport = HttpsNebiusTransport.from_environment()
    qwen = build_qwen_world()
    n_profile = nebius_profile(NEBIUS_MODEL, context_min_tokens=8192)
    nebius = NebiusProvider(n_profile, transport=transport)
    n_service = nebius_service(n_profile.digest())
    n_quote = nebius_quote(n_service)
    l_profile = qwen.bound.profile
    l_service = qwen.scenario.service_class
    l_quote = local_quote(l_service)
    general = general_class()
    controlled = controlled_class(l_profile)
    f_general = facility_for(general, "facility-qwen-market-general", EvidenceTier.WITNESSED)
    f_controlled = facility_for(
        controlled, "facility-qwen-market-controlled", EvidenceTier.BOUND_AND_SIGNED
    )
    registry = qwen.registry()

    def gateway(db: Path, *providers: Any, chain: ReceiptChain | None = None) -> Gateway:
        return Gateway(
            ledger=ReservationLedger(db / "reservations.sqlite3"),
            registry=registry,
            gateway_key=GATEWAY_KEY,
            providers=providers,
            clock=wall_clock,
            chain=chain,
        )

    def candidates(
        facility: Any, procurement: ProcurementClass, prompt: bytes, base: int
    ) -> list[Any]:
        return [
            make_purchase(
                Scenario(facility, n_quote, n_service, n_profile, procurement),
                base,
                request_prompt=prompt,
            ),
            make_purchase(
                Scenario(facility, l_quote, l_service, l_profile, procurement),
                base + 1,
                request_prompt=prompt,
            ),
        ]

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp)
        (db / "attacks").mkdir()
        (db / "boundary").mkdir()
        (db / "market").mkdir()

        # 1. Market scenario A, GENERAL. The only Nebius inference call of this run.
        captured: dict[str, ProviderResult] = {}

        class CapturingOnce:
            attestation_capability = AttestationCapability.NONE
            provider_id = NEBIUS_PROVIDER_ID

            def execute(self, request: ProviderRequest) -> ProviderResult:
                if "result" in captured:
                    raise SystemExit("guard: refusing a second Nebius inference call")
                captured["result"] = nebius.execute(request)
                return captured["result"]

        market = gateway(db / "market", CapturingOnce(), qwen.provider)
        scenario_a = market.purchase_best(candidates(f_general, general, PROMPTS["general"], 10))
        tier1 = scenario_a.outcome
        chosen = scenario_a.selection.chosen
        tier1_usage, tier1_latency = nebius.last_usage, nebius.last_latency_ms
        tier1_status, tier1_finish = transport.last_status, nebius.last_finish_reason
        if chosen is None or chosen.purchase.request.provider_id != NEBIUS_PROVIDER_ID:
            raise SystemExit(f"scenario A did not choose Nebius: {selection_summary(scenario_a)}")
        if not isinstance(tier1, DeliveryVerified):
            raise SystemExit(f"Nebius delivery did not verify: {outcome_summary(tier1)}")
        purchase = chosen.purchase
        evidence, response = tier1.evidence, tier1.response

        # 2. Tier 1 attacks on the real evidence. No further Nebius calls.
        checker = gateway(db / "attacks", chain=ReceiptChain())

        def check(ev: Any, delivered: bytes = response) -> str | None:
            code = checker.verify_delivery(ev, delivered).record.reason_code
            return None if code is None else code.value

        r = evidence.receipt
        inserted = ProviderAttestationBody(
            r.purchase_request_digest,
            r.execution_profile_digest,
            r.response_digest,
            r.input_token_count,
            r.output_token_count,
        )
        try:
            checker.verify_delivery(replace(evidence, provider_attestation=inserted), response)
            a_verifier = "accepted-input"
        except VerifierInputError:
            a_verifier = "VerifierInputError"
        boundary = gateway(
            db / "boundary",
            Replaying(replace(captured["result"], attestation=ProviderAttestation(inserted, None))),
        ).purchase(
            make_purchase(
                Scenario(f_general, n_quote, n_service, n_profile, general),
                90,
                request_prompt=PROMPTS["general"],
            )
        )
        as_tier2 = replace(
            r,
            evidence_tier=EvidenceTier.BOUND_AND_SIGNED,
            provider_attestation_digest=inserted.digest(),
        )
        over = replace(r, output_token_count=n_quote.max_output_tokens + 1)
        over = replace(
            over,
            price_micro_trlusd=total_cost(n_quote, over.input_token_count, over.output_token_count),
        )
        wrong_profile = replace(r, execution_profile_digest=l_profile.digest())
        billed = replace(r, price_micro_trlusd=r.price_micro_trlusd + MicroTrlusd(1))
        attacks = {
            "A_attestation_inserted": {
                "gateway_boundary": outcome_summary(boundary).get("kind"),
                "verifier": a_verifier,
            },
            "B_tier1_presented_as_tier2": check(
                replace(
                    evidence,
                    receipt=as_tier2,
                    gateway_signature=sign(as_tier2, GATEWAY_KEY),
                    provider_attestation=inserted,
                    provider_signature=None,
                )
            ),
            "C_swapped_response_bytes": check(evidence, b"Synthetic swapped response bytes."),
            "D_wrong_profile_digest": check(
                replace(
                    evidence,
                    receipt=wrong_profile,
                    gateway_signature=sign(wrong_profile, GATEWAY_KEY),
                )
            ),
            "E_token_counts_above_caps": check(
                replace(evidence, receipt=over, gateway_signature=sign(over, GATEWAY_KEY))
            ),
            "F_altered_billed_amount": check(
                replace(evidence, receipt=billed, gateway_signature=sign(billed, GATEWAY_KEY))
            ),
            "G_invalid_gateway_signature": check(
                replace(evidence, gateway_signature=sign(r, ATTACKER_KEY))
            ),
            "control_untouched": check(evidence),
        }

        # 3. Market scenario B, CONTROLLED. Local only; the guard refuses any Nebius call.
        scenario_b = market.purchase_best(
            candidates(f_controlled, controlled, PROMPTS["controlled"], 20)
        )

    common = {
        "kind": "runtime-observation",
        "regenerable": False,
        "observed_at": _now(),
        "nebius_model_id": NEBIUS_MODEL,
        "nebius_endpoint": f"POST {NEBIUS_BASE_URL}/chat/completions",
        "registry": [
            {
                "key_id": key_id_for(key.verify_key).value,
                "role": role,
                "subject": subject,
                "status": "current",
            }
            for key, role, subject in (
                (AGENT_KEY, "agent", BORROWER.value),
                (GATEWAY_KEY, "gateway", GATEWAY_SUBJECT),
                (qwen.provider_key, "provider", LOCAL_QWEN_PROVIDER_ID.value),
            )
        ],
        "location_provenance": {
            "nebius": "Nebius-declared service region eu-north1, country_code FI (catalog metadata; not a legal/corporate jurisdiction)",
            "local": "operator-supplied deployment fact: the execution machine physically operates in the United States",
        },
        "quote_provenance": "configured DEMO prices in micro-tRLUSD (tRLUSD is a test token); not Nebius's billed price",
    }
    tier1_record = {
        "id": "nebius-qwen-tier1-observation",
        **common,
        "prompt_text": PROMPTS["general"].decode(),
        "same_call_as": "qwen-market-live-observation.json scenario_a_general",
        "response_text": None,
        "http_status": tier1_status,
        "finish_reason": tier1_finish,
        "latency_ms": tier1_latency,
        "usage": tier1_usage,
        "token_count_mapping": {
            "input_token_count": "usage.prompt_tokens",
            "output_token_count": "usage.completion_tokens",
            "source": "provider_reported",
        },
        "objects": {
            "execution_profile": _c(n_profile),
            "service_class": _c(n_service),
            "quote": _c(n_quote),
            "procurement_class": _c(general),
            "facility": _c(f_general),
            "purchase_request": _c(purchase.request),
            "purchase_request_signature": _c(purchase.request_signature),
        },
        "delivery": outcome_summary(tier1),
        "attacks": attacks,
    }
    market_record = {
        "id": "qwen-market-live-observation",
        **common,
        "objects": {
            "nebius_execution_profile": _c(n_profile),
            "nebius_service_class": _c(n_service),
            "nebius_quote": _c(n_quote),
            "local_execution_profile": _c(l_profile),
            "local_service_class": _c(l_service),
            "local_quote": _c(l_quote),
            "general_procurement_class": _c(general),
            "controlled_procurement_class": _c(controlled),
            "general_facility": _c(f_general),
            "controlled_facility": _c(f_controlled),
        },
        "prompts": {k: v.decode() for k, v in PROMPTS.items()},
        "nebius_inference_calls": 1,
        "scenario_a_general": {
            **selection_summary(scenario_a),
            "nebius_usage": tier1_usage,
            "nebius_latency_ms": tier1_latency,
        },
        "scenario_b_controlled": selection_summary(scenario_b),
    }
    OUT.mkdir(exist_ok=True)
    for name, record in (
        ("nebius-qwen-tier1-observation.json", tier1_record),
        ("qwen-market-live-observation.json", market_record),
    ):
        (OUT / name).write_text(
            json.dumps(record, indent=2, ensure_ascii=True) + "\n", encoding="ascii"
        )
        print(f"wrote {name}")
    print(
        "nebius tier1 (scenario A delivery):",
        tier1_record["delivery"]["outcome"],
        "| A chosen:",
        market_record["scenario_a_general"]["chosen_provider_id"],
        "| B chosen:",
        market_record["scenario_b_controlled"]["chosen_provider_id"],
    )


if __name__ == "__main__":
    main()
