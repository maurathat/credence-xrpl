"""Inference Checkpoint 2 runtime-observation evidence (contracts/fixtures/inference_c2/).

The file records one real local Llama run, so it is not regenerated. Instead, everything in it that
is checkable is re-checked here through Core alone: canonical digests, the agent, provider and
gateway signatures, and the verifier decision. The response text is not committed, so the
verifier's delivered-response digest is the recorded one; that one check is not independent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from credence.core.objects import (
    DecisionRecord,
    DeliveryReceiptBody,
    ExecutionProfile,
    Facility,
    ProcurementClass,
    ProviderAttestationBody,
    PurchaseRequest,
    Quote,
    Reservation,
    ServiceClass,
    SignedObject,
)
from credence.core.primitives import DecisionOutcome, EvidenceTier, KeyId, ReservationStatus
from credence.core.signatures import (
    KeyRegistry,
    KeyRole,
    KeyStatus,
    RegisteredKey,
    policy_signature_facts,
    verifier_signature_facts,
)
from credence.core.verifier import ProviderSignatureFact, VerifierInput, verify
from credence.inference.local_llama import MODEL_LAYER, TEMPLATE_LAYER
from credence.inference.payload import payload_digest

PATH = (
    Path(__file__).resolve().parents[2]
    / "contracts"
    / "fixtures"
    / "inference_c2"
    / "local-llama-tier2-observation.json"
)
RECORD: dict[str, Any] = json.loads(PATH.read_text(encoding="ascii"))
OBJECTS = RECORD["objects"]


def registry() -> KeyRegistry:
    return KeyRegistry(
        RegisteredKey(KeyId(k["key_id"]), KeyRole(k["role"]), k["subject"], KeyStatus(k["status"]))
        for k in RECORD["registry"]
    )


def test_is_labelled_as_runtime_observation() -> None:
    assert RECORD["kind"] == "runtime-observation"
    assert RECORD["regenerable"] is False
    assert RECORD["response_text"] is None
    assert RECORD["artifact"]["models_dir_is_outside_repository"] is True


def test_profile_binds_the_recorded_artifact() -> None:
    profile = ExecutionProfile.from_canonical(OBJECTS["execution_profile"])
    artifact = RECORD["artifact"]
    layers = {layer["media_type"]: layer for layer in artifact["layers"]}
    assert profile.model_artifact_digest is not None
    assert profile.model_artifact_digest.value == artifact["model_file_sha256"]
    assert layers[MODEL_LAYER]["digest"] == artifact["model_file_sha256"]
    assert layers[MODEL_LAYER]["size"] == artifact["model_file_bytes"]
    assert profile.chat_template_digest is not None
    assert profile.chat_template_digest.value == artifact["chat_template_sha256"]
    assert layers[TEMPLATE_LAYER]["digest"] == artifact["chat_template_sha256"]
    assert profile.tokenizer_digest is None
    assert artifact["gguf_general_file_type"] == 1
    assert set(artifact["tensor_types"]) <= {"F16", "F32"}
    assert (profile.precision, profile.quantization) == ("F16", "none")
    assert profile.runtime == RECORD["environment"]["runtime"]


def test_evidence_verifies_through_core_alone() -> None:
    reg = registry()
    facility = Facility.from_canonical(OBJECTS["facility"])
    procurement = ProcurementClass.from_canonical(OBJECTS["procurement_class"])
    service = ServiceClass.from_canonical(OBJECTS["service_class"])
    profile = ExecutionProfile.from_canonical(OBJECTS["execution_profile"])
    quote = Quote.from_canonical(OBJECTS["quote"])
    request = PurchaseRequest.from_canonical(OBJECTS["purchase_request"])
    attestation = ProviderAttestationBody.from_canonical(OBJECTS["provider_attestation"])
    receipt = DeliveryReceiptBody.from_canonical(OBJECTS["delivery_receipt"])
    reservation = Reservation.from_canonical(OBJECTS["reservation"])

    assert facility.allowed_procurement_classes == (procurement.digest(),)
    assert quote.service_class_digest == service.digest()
    assert service.execution_profile_digest == profile.digest()
    assert request.prompt_digest == payload_digest(RECORD["prompt_text"].encode("utf-8"))
    assert receipt.digest().value == RECORD["observed"]["receipt_digest"]
    assert attestation.digest().value == RECORD["observed"]["provider_attestation_digest"]
    assert receipt.evidence_tier is EvidenceTier.BOUND_AND_SIGNED
    assert reservation.status is ReservationStatus.HELD

    agent = policy_signature_facts(
        request, SignedObject.from_canonical(OBJECTS["purchase_request_signature"]), facility, reg
    )
    assert agent.request_signature_valid and agent.working_key_current

    provider_signature = SignedObject.from_canonical(OBJECTS["provider_attestation_signature"])
    assert provider_signature.key_id.value == RECORD["provider_key_id"]
    facts = verifier_signature_facts(
        receipt,
        SignedObject.from_canonical(OBJECTS["gateway_signature"]),
        attestation,
        provider_signature,
        reg,
    )
    assert facts.gateway_signature_valid
    assert facts.provider_signature is ProviderSignatureFact.VALID

    recorded = DecisionRecord.from_canonical(OBJECTS["verifier_decision"])
    decision = verify(
        VerifierInput(
            receipt=receipt,
            gateway_signature_valid=facts.gateway_signature_valid,
            provider_signature=facts.provider_signature,
            provider_attestation=attestation,
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
    assert decision.record == recorded
    assert recorded.outcome is DecisionOutcome.ACCEPT
    assert (
        DecisionRecord.from_canonical(OBJECTS["policy_decision"]).outcome is DecisionOutcome.ACCEPT
    )


def test_no_plaintext_response_in_canonical_objects() -> None:
    assert RECORD["prompt_text"] not in json.dumps(OBJECTS)
