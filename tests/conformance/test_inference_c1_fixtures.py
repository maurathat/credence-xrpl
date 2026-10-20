"""Inference Checkpoint 1 contract fixtures (contracts/fixtures/inference_c1/).

Regeneration must be byte-identical, and each committed fixture must stand on its own: parsed from
its canonical JSON alone, its digests, signatures and verifier decision are recomputed through the
real Core code, without the gateway.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

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
from credence.core.primitives import DecisionOutcome, KeyId, Timestamp
from credence.core.signatures import (
    KeyRegistry,
    KeyRole,
    KeyStatus,
    RegisteredKey,
    policy_signature_facts,
    verifier_signature_facts,
)
from credence.core.verifier import VerifierInput, verify
from credence.inference.payload import payload_digest

FIXTURES = Path(__file__).resolve().parents[2] / "contracts" / "fixtures" / "inference_c1"
BUILDER = FIXTURES.parent / "build_inference_c1_fixtures.py"
NAMES = ["tier1-witnessed-delivery.json", "tier2-bound-and-signed-delivery.json"]


def load(name: str) -> dict[str, Any]:
    fixture: dict[str, Any] = json.loads((FIXTURES / name).read_text(encoding="ascii"))
    return fixture


def test_regeneration_is_deterministic_and_matches_committed_files() -> None:
    spec = importlib.util.spec_from_file_location("build_inference_c1_fixtures", BUILDER)
    assert spec is not None and spec.loader is not None
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    first, second = builder.render(), builder.render()
    assert first == second
    assert first == {p.name: p.read_text(encoding="ascii") for p in FIXTURES.glob("*.json")}


def test_manifest_matches_files() -> None:
    manifest = {name: sha for sha, name in json.loads((FIXTURES / "MANIFEST.json").read_text())}
    assert sorted(manifest) == NAMES
    for name, sha in manifest.items():
        assert hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() == sha


@pytest.mark.parametrize("name", NAMES)
def test_fixture_verifies_through_core_alone(name: str) -> None:
    fixture = load(name)
    o = fixture["objects"]
    assert fixture["synthetic"] is True
    registry = KeyRegistry(
        RegisteredKey(KeyId(k["key_id"]), KeyRole(k["role"]), k["subject"], KeyStatus(k["status"]))
        for k in fixture["registry"]
    )
    facility = Facility.from_canonical(o["facility"])
    procurement = ProcurementClass.from_canonical(o["procurement_class"])
    service = ServiceClass.from_canonical(o["service_class"])
    profile = ExecutionProfile.from_canonical(o["execution_profile"])
    quote = Quote.from_canonical(o["quote"])
    request = PurchaseRequest.from_canonical(o["purchase_request"])
    receipt = DeliveryReceiptBody.from_canonical(o["delivery_receipt"])
    Reservation.from_canonical(o["reservation"])
    attestation = (
        None
        if o["provider_attestation"] is None
        else ProviderAttestationBody.from_canonical(o["provider_attestation"])
    )
    provider_sig = (
        None
        if o["provider_attestation_signature"] is None
        else SignedObject.from_canonical(o["provider_attestation_signature"])
    )
    expected = fixture["expected"]

    prompt = fixture["prompt_text"].encode("utf-8")
    response = fixture["response_text"].encode("utf-8")
    assert payload_digest(prompt).value == expected["prompt_digest"] == request.prompt_digest.value
    assert payload_digest(response).value == expected["response_digest"]
    assert receipt.digest().value == expected["receipt_digest"]
    assert receipt.evidence_tier.value == expected["evidence_tier"]
    assert facility.allowed_procurement_classes == (procurement.digest(),)
    assert fixture["token_count_source"] == "provider_reported"

    request_facts = policy_signature_facts(
        request, SignedObject.from_canonical(o["purchase_request_signature"]), facility, registry
    )
    assert request_facts.request_signature_valid and request_facts.working_key_current

    facts = verifier_signature_facts(
        receipt,
        SignedObject.from_canonical(o["gateway_signature"]),
        attestation,
        provider_sig,
        registry,
    )
    recorded = DecisionRecord.from_canonical(o["verifier_decision"])
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
            delivered_response_digest=payload_digest(response),
            expected_previous_receipt_digest=None,
            receipt_already_settled=False,
            evaluated_at=Timestamp(recorded.decided_at.value),
        )
    )
    assert decision.record == recorded
    assert recorded.outcome is DecisionOutcome.ACCEPT
    assert DecisionRecord.from_canonical(o["policy_decision"]).outcome is DecisionOutcome.ACCEPT


@pytest.mark.parametrize("name", NAMES)
def test_canonical_objects_carry_no_plaintext(name: str) -> None:
    fixture = load(name)
    objects_text = json.dumps(fixture["objects"])
    assert fixture["prompt_text"] not in objects_text
    assert fixture["response_text"] not in objects_text
