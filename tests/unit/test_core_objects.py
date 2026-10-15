"""Domain objects: construction, refusal, explicit canonical representation, bytes and digests.

Representative objects are loaded from the committed C1 vectors; invalid variants are built with
dataclasses.replace, which re-runs validation.
"""

import dataclasses
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from credence.core import kessai
from credence.core.objects import (
    OBJECT_TYPES,
    CanonicalObject,
    DecisionRecord,
    DeliveryReceiptBody,
    ExecutionProfile,
    Facility,
    ProcurementClass,
    ProviderAttestationBody,
    PurchaseRequest,
    Quote,
    Reservation,
    SamplingPolicy,
    ServiceClass,
    SettlementRecord,
    SignedObject,
)
from credence.core.primitives import (
    CountryCode,
    DecisionOutcome,
    DecisionStage,
    DomainError,
    EvidenceTier,
    MicroTrlusd,
    ProviderId,
    Purpose,
    ReasonCode,
    SeedPolicy,
    Sha256Digest,
    Timestamp,
)

VECTORS = Path(__file__).resolve().parents[2] / "vectors" / "c1"


def load(vector_id: str) -> dict[str, Any]:
    value: dict[str, Any] = json.loads((VECTORS / f"{vector_id}.json").read_text())["input"]
    return value


PROFILE = ExecutionProfile.from_canonical(load("execution_profile-full"))
PROFILE_A = ExecutionProfile.from_canonical(load("execution_profile-not-asserted"))
SERVICE = ServiceClass.from_canonical(load("service_class-tier2"))
PROCUREMENT = ProcurementClass.from_canonical(load("procurement_class-basic"))
QUOTE = Quote.from_canonical(load("quote-basic"))
FACILITY = Facility.from_canonical(load("facility-active"))
REQUEST = PurchaseRequest.from_canonical(load("purchase_request-basic"))
RESERVATION = Reservation.from_canonical(load("reservation-held"))
ATTESTATION = ProviderAttestationBody.from_canonical(load("provider_attestation-basic"))
RECEIPT = DeliveryReceiptBody.from_canonical(load("delivery_receipt-tier2"))
SETTLEMENT = SettlementRecord.from_canonical(load("settlement_record-basic"))
ACCEPT = DecisionRecord.from_canonical(load("decision_record-accept"))
SIGNED = SignedObject.from_canonical(load("signed_object-basic"))

SAMPLES: list[CanonicalObject] = [
    PROFILE,
    SERVICE,
    PROCUREMENT,
    QUOTE,
    FACILITY,
    REQUEST,
    RESERVATION,
    ATTESTATION,
    RECEIPT,
    SETTLEMENT,
    ACCEPT,
    SIGNED,
]


def refused(obj: object, **changes: object) -> tuple[str, str]:
    with pytest.raises(DomainError) as exc:
        replace(obj, **changes)  # type: ignore[type-var]
    return exc.value.reason, exc.value.field


def test_samples_cover_every_object_type() -> None:
    assert {type(s) for s in SAMPLES} == set(OBJECT_TYPES)


@pytest.mark.parametrize("obj", SAMPLES, ids=lambda o: type(o).__name__)
def test_canonical_representation_is_explicit(obj: CanonicalObject) -> None:
    canonical = obj.to_canonical()
    assert list(canonical)[:2] == ["object_type", "schema_version"]
    assert canonical["object_type"] == obj.OBJECT_TYPE
    assert canonical["schema_version"] == "1"
    assert tuple(canonical)[2:] == obj.MEMBERS
    assert obj.MEMBERS == tuple(f.name for f in dataclasses.fields(obj))  # type: ignore[arg-type]


@pytest.mark.parametrize("obj", SAMPLES, ids=lambda o: type(o).__name__)
def test_bytes_digest_and_round_trip(obj: CanonicalObject) -> None:
    form = kessai.canonicalize(obj.to_canonical())
    assert obj.canonical_bytes() == form.data
    assert obj.digest() == Sha256Digest.from_hex(form.sha256_hex)
    assert type(obj).from_canonical(json.loads(obj.canonical_bytes())) == obj


@pytest.mark.parametrize("obj", SAMPLES, ids=lambda o: type(o).__name__)
def test_objects_are_immutable(obj: CanonicalObject) -> None:
    name = dataclasses.fields(obj)[0].name  # type: ignore[arg-type]
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(obj, name, None)


def test_object_types_are_distinct() -> None:
    assert len({cls.OBJECT_TYPE for cls in OBJECT_TYPES}) == len(OBJECT_TYPES)


def test_no_prompt_or_response_text_is_stored() -> None:
    for cls in OBJECT_TYPES:
        for member in cls.MEMBERS:
            assert member in {"prompt_digest", "response_digest"} or not member.startswith(
                ("prompt", "response")
            )


def test_any_member_change_changes_the_digest() -> None:
    cheaper = replace(QUOTE, input_price_per_million_micro_trlusd=MicroTrlusd(149_999))
    assert cheaper.digest() != QUOTE.digest()


# --- parsing refusals ----------------------------------------------------------------------


def parse_refused(cls: type[CanonicalObject], value: object) -> tuple[str, str]:
    with pytest.raises(DomainError) as exc:
        cls.from_canonical(value)
    return exc.value.reason, exc.value.field


def test_parse_refuses_shape_errors() -> None:
    base = QUOTE.to_canonical()
    assert parse_refused(Quote, [base])[0] == "NOT_AN_OBJECT"
    missing = {k: v for k, v in base.items() if k != "valid_until"}
    assert parse_refused(Quote, missing) == ("MISSING_MEMBER", "valid_until")
    assert parse_refused(Quote, {**base, "schema_version": "2"})[0] == "UNSUPPORTED_SCHEMA_VERSION"
    assert parse_refused(Quote, {**base, "schema_version": 1})[0] == "UNSUPPORTED_SCHEMA_VERSION"
    assert parse_refused(Quote, {**base, "object_type": "credence.facility"})[0] == (
        "WRONG_OBJECT_TYPE"
    )
    no_envelope = {k: v for k, v in base.items() if k != "object_type"}
    assert parse_refused(Quote, no_envelope) == ("MISSING_MEMBER", "object_type")
    procurement = PROCUREMENT.to_canonical()
    assert parse_refused(ProcurementClass, {**procurement, "allowed_providers": "provider-a"}) == (
        "NOT_A_LIST",
        "allowed_providers",
    )
    assert parse_refused(
        ProcurementClass, {**procurement, "allowed_providers": ["provider-a", 7]}
    ) == ("NOT_A_STRING", "allowed_providers[1]")


def test_parse_refuses_a_python_tuple_where_a_json_array_belongs() -> None:
    value = {**PROCUREMENT.to_canonical(), "allowed_providers": ("provider-a",)}
    assert parse_refused(ProcurementClass, value)[0] == "NOT_A_LIST"


# --- construction refusals -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"max_input_tokens": 1.0}, ("FLOAT_NOT_INTEGER", "max_input_tokens")),
        ({"max_input_tokens": True}, ("BOOL_NOT_INTEGER", "max_input_tokens")),
        ({"max_output_tokens": -1}, ("NEGATIVE_VALUE", "max_output_tokens")),
        ({"quote_id": "quote-0001"}, ("WRONG_TYPE", "quote_id")),
        (
            {"input_price_per_million_micro_trlusd": 150_000},
            ("WRONG_TYPE", "input_price_per_million_micro_trlusd"),
        ),
        ({"valid_until": "2026-10-24T15:05:00Z"}, ("WRONG_TYPE", "valid_until")),
    ],
)
def test_quote_refuses(changes: dict[str, object], expected: tuple[str, str]) -> None:
    assert refused(QUOTE, **changes) == expected


def test_sampling_policy_rules() -> None:
    assert SamplingPolicy(700, SeedPolicy.FIXED, 0).seed == 0
    assert SamplingPolicy(None, SeedPolicy.NONE, None).temperature_milli is None
    for policy, seed in [
        (SeedPolicy.FIXED, None),
        (SeedPolicy.PROVIDER, 1),
        (SeedPolicy.NONE, 1),
    ]:
        with pytest.raises(DomainError) as exc:
            SamplingPolicy(700, policy, seed)
        assert exc.value.reason == "SEED_POLICY_MISMATCH"
    with pytest.raises(DomainError) as exc:
        SamplingPolicy(0.7, SeedPolicy.NONE, None)  # type: ignore[arg-type]
    assert exc.value.reason == "FLOAT_NOT_INTEGER"


NOT_ASSERTABLE = (
    "model_artifact_digest",
    "tokenizer_digest",
    "chat_template_digest",
    "runtime",
    "precision",
    "quantization",
)


def test_execution_profile_not_asserted_fields_are_null_not_wildcards() -> None:
    canonical = PROFILE_A.to_canonical()
    for name in NOT_ASSERTABLE:
        assert getattr(PROFILE_A, name) is None
        assert canonical[name] is None
    assert PROFILE_A.adapter_digest is None
    assert PROFILE.quantization == "none"  # an assertion of no quantization, unlike null


@pytest.mark.parametrize("name", NOT_ASSERTABLE)
def test_each_not_assertable_field_is_independently_nullable(name: str) -> None:
    profile = replace(PROFILE, **{name: None})
    assert profile.to_canonical()[name] is None
    assert profile.digest() != PROFILE.digest()
    assert ExecutionProfile.from_canonical(profile.to_canonical()) == profile


def test_model_identifier_is_not_nullable() -> None:
    assert refused(PROFILE, model_identifier=None) == ("NOT_A_STRING", "model_identifier")


def test_temperature_milli_is_a_single_configuration_value() -> None:
    assert PROFILE.sampling.temperature_milli == 700
    assert PROFILE.to_canonical()["sampling"] == {
        "temperature_milli": 700,
        "seed_policy": "fixed",
        "seed": 42,
    }


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"tokenizer_digest": "sha256:" + "0" * 64}, ("WRONG_TYPE", "tokenizer_digest")),
        ({"chat_template_digest": ""}, ("WRONG_TYPE", "chat_template_digest")),
        ({"model_identifier": ""}, ("EMPTY_VALUE", "model_identifier")),
        ({"runtime": ""}, ("EMPTY_VALUE", "runtime")),
        ({"precision": "bf16-e\u0301"}, ("NOT_NFC", "precision")),
        ({"context_min_tokens": 32768.0}, ("FLOAT_NOT_INTEGER", "context_min_tokens")),
    ],
)
def test_execution_profile_refuses(changes: dict[str, object], expected: tuple[str, str]) -> None:
    assert refused(PROFILE, **changes) == expected


def test_service_class_formats() -> None:
    assert replace(SERVICE, availability_bps=10_000).availability_bps == 10_000
    assert replace(SERVICE, data_residency=()).data_residency == ()
    assert refused(SERVICE, availability_bps=10_001) == ("OUT_OF_RANGE", "availability_bps")
    assert refused(SERVICE, latency_p95_ms=-1) == ("NEGATIVE_VALUE", "latency_p95_ms")
    assert refused(SERVICE, data_residency=(CountryCode("US"), CountryCode("DE"))) == (
        "UNSORTED_SET",
        "data_residency",
    )
    assert refused(SERVICE, data_residency=[CountryCode("DE")]) == ("WRONG_TYPE", "data_residency")
    assert refused(SERVICE, evidence_tier=2) == ("WRONG_TYPE", "evidence_tier")


@pytest.mark.parametrize(
    "member",
    [
        "allowed_precisions",
        "allowed_quantizations",
        "allowed_jurisdictions",
        "allowed_data_residencies",
        "allowed_providers",
    ],
)
def test_procurement_sets_are_non_empty(member: str) -> None:
    assert refused(PROCUREMENT, **{member: ()}) == ("EMPTY_SET", member)


@pytest.mark.parametrize("member", ["allowed_precisions", "allowed_quantizations"])
def test_precision_and_quantization_may_be_unconstrained(member: str) -> None:
    unconstrained = replace(PROCUREMENT, **{member: None})
    assert unconstrained.to_canonical()[member] is None
    assert ProcurementClass.from_canonical(unconstrained.to_canonical()) == unconstrained
    assert unconstrained.digest() != PROCUREMENT.digest()


@pytest.mark.parametrize(
    "member", ["allowed_jurisdictions", "allowed_data_residencies", "allowed_providers"]
)
def test_other_procurement_sets_may_not_be_null(member: str) -> None:
    assert refused(PROCUREMENT, **{member: None}) == ("WRONG_TYPE", member)
    assert parse_refused(ProcurementClass, {**PROCUREMENT.to_canonical(), member: None}) == (
        "NOT_A_LIST",
        member,
    )


def test_procurement_sets_have_one_spelling() -> None:
    a, b = ProviderId("provider-a"), ProviderId("provider-b")
    assert refused(PROCUREMENT, allowed_providers=(b, a)) == ("UNSORTED_SET", "allowed_providers")
    assert refused(PROCUREMENT, allowed_providers=(a, a)) == (
        "DUPLICATE_ENTRY",
        "allowed_providers",
    )
    assert refused(PROCUREMENT, allowed_precisions=("BF16", "BF16")) == (
        "DUPLICATE_ENTRY",
        "allowed_precisions",
    )
    assert refused(PROCUREMENT, allowed_providers=("provider-a",)) == (
        "WRONG_TYPE",
        "allowed_providers[0]",
    )


def test_facility() -> None:
    pending = replace(FACILITY, xrpl_loan_id=None)
    assert pending.to_canonical()["xrpl_loan_id"] is None
    assert refused(FACILITY, term_seconds=1800.0) == ("FLOAT_NOT_INTEGER", "term_seconds")
    assert refused(FACILITY, spread_bps=-1) == ("NEGATIVE_VALUE", "spread_bps")
    assert refused(FACILITY, allowed_procurement_classes=()) == (
        "EMPTY_SET",
        "allowed_procurement_classes",
    )
    assert refused(FACILITY, status="active") == ("WRONG_TYPE", "status")
    assert refused(FACILITY, owner_approval_digest=None) == ("WRONG_TYPE", "owner_approval_digest")


def test_reservation_expiry_follows_creation() -> None:
    assert refused(RESERVATION, expires_at=RESERVATION.created_at) == (
        "TIMESTAMP_ORDER",
        "expires_at",
    )


def test_receipt_tier_and_attestation_agree() -> None:
    tier1 = replace(RECEIPT, evidence_tier=EvidenceTier.WITNESSED, provider_attestation_digest=None)
    assert tier1.provider_attestation_digest is None
    assert refused(RECEIPT, provider_attestation_digest=None) == (
        "TIER_ATTESTATION_MISMATCH",
        "provider_attestation_digest",
    )
    assert refused(tier1, provider_attestation_digest=ATTESTATION.digest()) == (
        "TIER_ATTESTATION_MISMATCH",
        "provider_attestation_digest",
    )
    for tier in (EvidenceTier.REQUESTED, EvidenceTier.MEASURED):
        assert refused(RECEIPT, evidence_tier=tier) == ("UNSUPPORTED_RECEIPT_TIER", "evidence_tier")
    assert refused(RECEIPT, completed_at=Timestamp("2026-10-24T15:00:00Z")) == (
        "TIMESTAMP_ORDER",
        "completed_at",
    )


def test_receipt_identity_excludes_signatures_and_payment_hash() -> None:
    members = set(DeliveryReceiptBody.MEMBERS)
    assert not members & {"provider_signature", "gateway_signature", "xrpl_transaction_hash"}
    assert set(ProviderAttestationBody.MEMBERS) == {
        "purchase_request_digest",
        "execution_profile_digest",
        "response_digest",
        "input_token_count",
        "output_token_count",
    }
    assert SETTLEMENT.receipt_digest == RECEIPT.digest()


def test_purchase_request_purpose() -> None:
    assert REQUEST.purpose is Purpose.INFERENCE
    assert REQUEST.to_canonical()["purpose"] == "inference"
    assert refused(REQUEST, purpose="inference") == ("WRONG_TYPE", "purpose")
    value = REQUEST.to_canonical()
    assert parse_refused(PurchaseRequest, {**value, "purpose": "training"}) == (
        "INVALID_ENUM",
        "purpose",
    )
    del value["purpose"]
    assert parse_refused(PurchaseRequest, value) == ("MISSING_MEMBER", "purpose")


def test_decision_stage() -> None:
    assert ACCEPT.decision_stage is DecisionStage.POLICY
    assert ACCEPT.to_canonical()["decision_stage"] == "policy"
    verifier = replace(ACCEPT, decision_stage=DecisionStage.VERIFIER)
    assert verifier.to_canonical()["decision_stage"] == "verifier"
    assert verifier.digest() != ACCEPT.digest()
    assert refused(ACCEPT, decision_stage="policy") == ("WRONG_TYPE", "decision_stage")
    assert parse_refused(DecisionRecord, {**ACCEPT.to_canonical(), "decision_stage": "both"}) == (
        "INVALID_ENUM",
        "decision_stage",
    )


def test_generic_evidence_tier_keeps_all_four_values() -> None:
    assert [(t.name, t.value) for t in EvidenceTier] == [
        ("REQUESTED", 0),
        ("WITNESSED", 1),
        ("BOUND_AND_SIGNED", 2),
        ("MEASURED", 3),
    ]
    for tier in EvidenceTier:
        assert replace(SERVICE, evidence_tier=tier).evidence_tier is tier
        assert replace(PROCUREMENT, minimum_evidence_tier=tier).minimum_evidence_tier is tier
        assert replace(FACILITY, minimum_evidence_tier=tier).minimum_evidence_tier is tier


def test_decision_record_rules() -> None:
    assert refused(ACCEPT, reason_code=ReasonCode.OVER_LIMIT) == (
        "DECISION_REASON_MISMATCH",
        "reason_code",
    )
    assert refused(ACCEPT, outcome=DecisionOutcome.REJECT) == (
        "DECISION_REASON_MISMATCH",
        "reason_code",
    )
    assert refused(ACCEPT, input_digests=()) == ("EMPTY_SET", "input_digests")
    assert refused(ACCEPT, authorized_transaction_digest=ATTESTATION.digest()) == (
        "MUST_BE_NULL_IN_C1",
        "authorized_transaction_digest",
    )
    assert refused(ACCEPT, authorized_transaction_digest_scheme="sha512half") == (
        "MUST_BE_NULL_IN_C1",
        "authorized_transaction_digest_scheme",
    )


def test_decision_record_input_order_is_meaningful_and_preserved() -> None:
    reordered = replace(ACCEPT, input_digests=tuple(reversed(ACCEPT.input_digests)))
    assert reordered.to_canonical()["input_digests"] == [
        d.value for d in reversed(ACCEPT.input_digests)
    ]
    assert reordered.digest() != ACCEPT.digest()


def test_signed_object_is_outside_the_body() -> None:
    assert SIGNED.object_digest == RECEIPT.digest()
    assert refused(SIGNED, key_id="ed25519:" + "0" * 64) == ("WRONG_TYPE", "key_id")
