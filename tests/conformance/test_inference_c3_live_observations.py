"""Inference CP3 live Nebius + market runtime-observation evidence (contracts/fixtures/inference_c3/).

Written by contracts/fixtures/record_qwen_market_live.py from ONE real Nebius inference call (market
scenario A, whose delivery is also the Tier 1 evidence) and real local calls. Not regenerated:
everything checkable is re-checked here through Core alone, including every candidate's dry-run
policy decision. The response text is not committed, so the verifier's delivered-response digest
is the recorded one; that single check is not independent.

Skipped until the live files exist. CREDENCE_C3_OBSERVATION_DIR points the tests at another
directory (used to check these tests against an offline rehearsal).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from credence.core.objects import (
    DecisionRecord,
    DeliveryReceiptBody,
    ExecutionProfile,
    Facility,
    ProcurementClass,
    PurchaseRequest,
    Quote,
    ServiceClass,
    SignedObject,
)
from credence.core.policy_engine import PolicyInput, evaluate
from credence.core.primitives import DecisionOutcome, EvidenceTier, KeyId, MicroTrlusd
from credence.core.signatures import (
    KeyRegistry,
    KeyRole,
    KeyStatus,
    RegisteredKey,
    policy_signature_facts,
    verifier_signature_facts,
)
from credence.core.verifier import ProviderSignatureFact, VerifierInput, verify

DIR = Path(
    os.environ.get("CREDENCE_C3_OBSERVATION_DIR")
    or Path(__file__).resolve().parents[2] / "contracts" / "fixtures" / "inference_c3"
)
TIER1 = DIR / "nebius-qwen-tier1-observation.json"
MARKET = DIR / "qwen-market-live-observation.json"
pytestmark = pytest.mark.skipif(
    not (TIER1.exists() and MARKET.exists()), reason="live CP3B observations not recorded yet"
)


def load(path: Path) -> dict[str, Any]:
    record: dict[str, Any] = json.loads(path.read_text(encoding="ascii"))
    return record


def registry(record: dict[str, Any]) -> KeyRegistry:
    return KeyRegistry(
        RegisteredKey(KeyId(k["key_id"]), KeyRole(k["role"]), k["subject"], KeyStatus(k["status"]))
        for k in record["registry"]
    )


def test_labels_and_no_plaintext_response() -> None:
    tier1, market = load(TIER1), load(MARKET)
    for record in (tier1, market):
        assert record["kind"] == "runtime-observation" and record["regenerable"] is False
        assert record["nebius_model_id"] == "Qwen/Qwen3-30B-A3B-Instruct-2507"
        assert "DEMO" in record["quote_provenance"]
        assert "not a legal/corporate jurisdiction" in record["location_provenance"]["nebius"]
    assert tier1["response_text"] is None
    assert market["nebius_inference_calls"] == 1
    assert (
        tier1["delivery"]["receipt_digest"]
        == market["scenario_a_general"]["delivery"]["receipt_digest"]
    )


def test_nebius_profile_asserts_nothing_unsupported() -> None:
    profile = ExecutionProfile.from_canonical(load(TIER1)["objects"]["execution_profile"])
    assert profile.model_identifier == "Qwen/Qwen3-30B-A3B-Instruct-2507"
    for field in (
        "model_artifact_digest",
        "tokenizer_digest",
        "adapter_digest",
        "chat_template_digest",
        "runtime",
        "precision",
        "quantization",
    ):
        assert getattr(profile, field) is None, field
    assert profile.sampling.seed is None


def test_usage_mapping_is_unambiguous() -> None:
    tier1 = load(TIER1)
    usage, receipt = tier1["usage"], tier1["delivery"]["receipt"]
    assert receipt["input_token_count"] == usage["prompt_tokens"]
    assert receipt["output_token_count"] == usage["completion_tokens"]
    for group, field in (
        ("prompt_tokens_details", "cached_tokens"),
        ("completion_tokens_details", "reasoning_tokens"),
    ):
        details = usage.get(group)
        assert details is None or not details.get(field)
    assert tier1["token_count_mapping"]["source"] == "provider_reported"
    assert tier1["http_status"] == 200


def test_tier1_delivery_verifies_through_core_alone() -> None:
    tier1 = load(TIER1)
    objects, delivery = tier1["objects"], tier1["delivery"]
    reg = registry(tier1)
    facility = Facility.from_canonical(objects["facility"])
    service = ServiceClass.from_canonical(objects["service_class"])
    profile = ExecutionProfile.from_canonical(objects["execution_profile"])
    quote = Quote.from_canonical(objects["quote"])
    request = PurchaseRequest.from_canonical(objects["purchase_request"])
    receipt = DeliveryReceiptBody.from_canonical(delivery["receipt"])
    assert receipt.evidence_tier is EvidenceTier.WITNESSED
    assert (
        delivery["provider_attestation"] is None
        and delivery["provider_attestation_signature"] is None
    )
    assert service.jurisdiction is not None and service.jurisdiction.value == "FI"
    agent = policy_signature_facts(
        request, SignedObject.from_canonical(objects["purchase_request_signature"]), facility, reg
    )
    assert agent.request_signature_valid and agent.working_key_current
    facts = verifier_signature_facts(
        receipt, SignedObject.from_canonical(delivery["gateway_signature"]), None, None, reg
    )
    assert facts.gateway_signature_valid
    assert facts.provider_signature is ProviderSignatureFact.NOT_PRESENT
    recorded = DecisionRecord.from_canonical(delivery["verifier_decision"])
    decision = verify(
        VerifierInput(
            receipt=receipt,
            gateway_signature_valid=True,
            provider_signature=ProviderSignatureFact.NOT_PRESENT,
            provider_attestation=None,
            request=request,
            quote=quote,
            service_class=service,
            execution_profile=profile,
            delivered_response_digest=receipt.response_digest,  # recorded, not independent
            expected_previous_receipt_digest=None,
            receipt_already_settled=False,
            evaluated_at=recorded.decided_at,
        )
    )
    assert decision.record == recorded and recorded.outcome is DecisionOutcome.ACCEPT


def test_tier1_attacks_recorded_core_results() -> None:
    assert load(TIER1)["attacks"] == {
        "A_attestation_inserted": {
            "gateway_boundary": "PROVIDER_MALFORMED_RESPONSE",
            "verifier": "VerifierInputError",
        },
        "B_tier1_presented_as_tier2": "RECEIPT_SIG_INVALID",
        "C_swapped_response_bytes": "RESPONSE_DIGEST_MISMATCH",
        "D_wrong_profile_digest": "BINDING_MISMATCH",
        "E_token_counts_above_caps": "TOKEN_COUNT_EXCEEDS_QUOTE",
        "F_altered_billed_amount": "BILLED_AMOUNT_MISMATCH",
        "G_invalid_gateway_signature": "RECEIPT_SIG_INVALID",
        "control_untouched": None,
    }


@pytest.mark.parametrize(
    "scenario,facility_key,procurement_key",
    [
        ("scenario_a_general", "general_facility", "general_procurement_class"),
        ("scenario_b_controlled", "controlled_facility", "controlled_procurement_class"),
    ],
)
def test_dry_run_qualifications_recompute_through_core(
    scenario: str, facility_key: str, procurement_key: str
) -> None:
    market = load(MARKET)
    objects, reg = market["objects"], registry(market)
    facility = Facility.from_canonical(objects[facility_key])
    procurement = ProcurementClass.from_canonical(objects[procurement_key])
    offers = {
        "nebius-token-factory": (
            "nebius_quote",
            "nebius_service_class",
            "nebius_execution_profile",
        ),
        "local-qwen-provider": ("local_quote", "local_service_class", "local_execution_profile"),
    }
    for q in market[scenario]["qualifications"]:
        quote_key, service_key, profile_key = offers[q["provider_id"]]
        request = PurchaseRequest.from_canonical(q["purchase_request"])
        facts = policy_signature_facts(
            request, SignedObject.from_canonical(q["purchase_request_signature"]), facility, reg
        )
        recorded = DecisionRecord.from_canonical(q["dry_run_policy_decision"])
        decision = evaluate(
            PolicyInput(
                request=request,
                request_signature_valid=facts.request_signature_valid,
                working_key_current=facts.working_key_current,
                facility=facility,
                quote=Quote.from_canonical(objects[quote_key]),
                service_class=ServiceClass.from_canonical(objects[service_key]),
                execution_profile=ExecutionProfile.from_canonical(objects[profile_key]),
                procurement_classes=(procurement,),
                currently_reserved=MicroTrlusd(q["currently_reserved_micro_trlusd"]),
                nonce_already_used=q["nonce_already_used"],
                evaluated_at=recorded.decided_at,
            )
        )
        assert decision.record == recorded
        assert decision.max_total_cost_value == q["max_total_cost_micro_trlusd"]


def test_price_vs_evidence_outcomes() -> None:
    market = load(MARKET)
    a, b = market["scenario_a_general"], market["scenario_b_controlled"]
    a_by = {q["provider_id"]: q for q in a["qualifications"]}
    b_by = {q["provider_id"]: q for q in b["qualifications"]}
    nebius, local = "nebius-token-factory", "local-qwen-provider"
    # A: both qualify; Nebius is cheaper and chosen.
    assert a_by[nebius]["qualified"] and a_by[local]["qualified"]
    assert a_by[nebius]["max_total_cost_micro_trlusd"] < a_by[local]["max_total_cost_micro_trlusd"]
    assert a["chosen_provider_id"] == nebius and a["delivery"]["outcome"] == "DeliveryVerified"
    # B: Nebius is still cheaper but Core rejects it; local is chosen and delivers at Tier 2.
    assert not b_by[nebius]["qualified"] and b_by[nebius]["reason_code"] == "PROFILE_NOT_ALLOWED"
    assert b_by[nebius]["max_total_cost_micro_trlusd"] < b_by[local]["max_total_cost_micro_trlusd"]
    assert b_by[local]["qualified"] and b["chosen_provider_id"] == local
    assert b["delivery"]["outcome"] == "DeliveryVerified"
    assert b["delivery"]["receipt"]["evidence_tier"] == 2
    # Same provider evidence in both scenarios: only procurement and quotes differ.
    assert (
        a_by[nebius]["purchase_request"]["quote_id"] == b_by[nebius]["purchase_request"]["quote_id"]
    )


def test_scenario_b_local_tier2_delivery_verifies_through_core_alone() -> None:
    from credence.core.objects import ProviderAttestationBody

    market = load(MARKET)
    objects, reg, b = market["objects"], registry(market), market["scenario_b_controlled"]
    chosen = next(q for q in b["qualifications"] if q["provider_id"] == b["chosen_provider_id"])
    delivery = b["delivery"]
    receipt = DeliveryReceiptBody.from_canonical(delivery["receipt"])
    attestation = ProviderAttestationBody.from_canonical(delivery["provider_attestation"])
    assert receipt.evidence_tier is EvidenceTier.BOUND_AND_SIGNED
    assert receipt.provider_attestation_digest == attestation.digest()
    facts = verifier_signature_facts(
        receipt,
        SignedObject.from_canonical(delivery["gateway_signature"]),
        attestation,
        SignedObject.from_canonical(delivery["provider_attestation_signature"]),
        reg,
    )
    assert facts.gateway_signature_valid
    assert facts.provider_signature is ProviderSignatureFact.VALID
    recorded = DecisionRecord.from_canonical(delivery["verifier_decision"])
    decision = verify(
        VerifierInput(
            receipt=receipt,
            gateway_signature_valid=facts.gateway_signature_valid,
            provider_signature=facts.provider_signature,
            provider_attestation=attestation,
            request=PurchaseRequest.from_canonical(chosen["purchase_request"]),
            quote=Quote.from_canonical(objects["local_quote"]),
            service_class=ServiceClass.from_canonical(objects["local_service_class"]),
            execution_profile=ExecutionProfile.from_canonical(objects["local_execution_profile"]),
            delivered_response_digest=receipt.response_digest,  # recorded, not independent
            expected_previous_receipt_digest=None,
            receipt_already_settled=False,
            evaluated_at=recorded.decided_at,
        )
    )
    assert decision.record == recorded and recorded.outcome is DecisionOutcome.ACCEPT
