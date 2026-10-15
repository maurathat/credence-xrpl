"""Generate the Credence Core C1 conformance vectors in vectors/c1/.

Run from the repository root:  uv run python vectors/build_c1_vectors.py

Every expected canonical byte string and digest is produced here by the real pinned Kessai serializer
(credence.core.kessai over custody's canon.py); none is written by hand. The output is then frozen in
git and checked independently by tests/conformance/test_core_c1_vectors.py.

All values are synthetic and illustrative: digests are SHA-256 of labels, key IDs and signatures are
derived from labels and verify nothing, and the XRPL account is the classic address of a fixed
20-byte pattern with no key behind it.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from xrpl.core.addresscodec import encode_classic_address

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
    MAX_SAFE_INTEGER,
    CountryCode,
    DecisionOutcome,
    DeliveryMode,
    DomainError,
    Ed25519Signature,
    EngineVersion,
    EvidenceTier,
    FacilityId,
    FacilityStatus,
    KeyId,
    MicroTrlusd,
    Nonce,
    ProviderId,
    Purpose,
    QuoteId,
    ReasonCode,
    ReservationId,
    ReservationStatus,
    ResidencyRegion,
    SeedPolicy,
    ServiceTier,
    Sha256Digest,
    Timestamp,
    XrplAccount,
    XrplLedgerObjectId,
    XrplTransactionHash,
)

OUT = Path(__file__).resolve().parent / "c1"
CLASSES = {cls.__name__: cls for cls in OBJECT_TYPES}


def label_hex(label: str) -> str:
    return hashlib.sha256(f"credence-c1-vector:{label}".encode()).hexdigest()


def digest(label: str) -> Sha256Digest:
    return Sha256Digest.from_hex(label_hex(label))


# --- the representative objects ------------------------------------------------------------

PROVIDER_A, PROVIDER_B = ProviderId("provider-a"), ProviderId("provider-b")

PROFILE_B = ExecutionProfile(
    model_identifier="example-model-8b-instruct",
    model_artifact_digest=digest("weights-file"),
    tokenizer_digest=digest("tokenizer"),
    adapter_digest=digest("adapter"),
    chat_template_digest=digest("chat-template"),
    runtime="llama.cpp b4500",
    precision="BF16",
    quantization="none",
    context_min_tokens=32768,
    sampling=SamplingPolicy(temperature_milli=700, seed_policy=SeedPolicy.FIXED, seed=42),
)
PROFILE_A = ExecutionProfile(
    model_identifier="example-model-8b-instruct",
    model_artifact_digest=None,
    tokenizer_digest=digest("tokenizer"),
    adapter_digest=None,
    chat_template_digest=digest("chat-template"),
    runtime=None,
    precision=None,
    quantization=None,
    context_min_tokens=32768,
    sampling=SamplingPolicy(temperature_milli=None, seed_policy=SeedPolicy.PROVIDER, seed=None),
)
SERVICE_CLASS_B = ServiceClass(
    execution_profile_digest=PROFILE_B.digest(),
    model_family="example-model-8b",
    delivery_mode=DeliveryMode.INTERACTIVE,
    service_tier=ServiceTier.BASE,
    jurisdiction=CountryCode("US"),
    data_residency=(CountryCode("DE"), CountryCode("FR"), CountryCode("US")),
    residency_region=ResidencyRegion.US,
    evidence_tier=EvidenceTier.BOUND_AND_SIGNED,
    latency_p95_ms=1500,
    availability_bps=9990,
)
SERVICE_CLASS_NON_ASCII = ServiceClass(
    execution_profile_digest=PROFILE_A.digest(),
    model_family="mod\u00e8le-Gr\u00f6\u00dfe-8b",
    delivery_mode=DeliveryMode.INTERACTIVE,
    service_tier=ServiceTier.FAST,
    jurisdiction=None,
    data_residency=(),
    residency_region=None,
    evidence_tier=EvidenceTier.WITNESSED,
    latency_p95_ms=None,
    availability_bps=None,
)
PROCUREMENT_CLASS = ProcurementClass(
    model_family="example-model-8b",
    allowed_precisions=("BF16", "FP16"),
    allowed_quantizations=("Q8_0", "none"),
    minimum_evidence_tier=EvidenceTier.WITNESSED,
    delivery_mode=DeliveryMode.INTERACTIVE,
    allowed_jurisdictions=(CountryCode("DE"), CountryCode("US")),
    allowed_data_residencies=(CountryCode("DE"), CountryCode("FR"), CountryCode("US")),
    allowed_providers=(PROVIDER_A, PROVIDER_B),
)
QUOTE = Quote(
    quote_id=QuoteId("quote-0001"),
    provider_id=PROVIDER_B,
    service_class_digest=SERVICE_CLASS_B.digest(),
    input_price_per_million_micro_trlusd=MicroTrlusd(150_000),
    output_price_per_million_micro_trlusd=MicroTrlusd(600_000),
    max_input_tokens=8192,
    max_output_tokens=2048,
    valid_until=Timestamp("2026-10-24T15:05:00Z"),
)
QUOTE_LARGE = Quote(
    quote_id=QuoteId("quote-large"),
    provider_id=PROVIDER_B,
    service_class_digest=SERVICE_CLASS_B.digest(),
    input_price_per_million_micro_trlusd=MicroTrlusd(MAX_SAFE_INTEGER),
    output_price_per_million_micro_trlusd=MicroTrlusd(0),
    max_input_tokens=MAX_SAFE_INTEGER,
    max_output_tokens=0,
    valid_until=Timestamp("9999-12-31T23:59:59Z"),
)
BORROWER = XrplAccount(encode_classic_address(bytes(range(1, 21))))
FACILITY_PENDING = Facility(
    facility_id=FacilityId("facility-0001"),
    borrower_account=BORROWER,
    xrpl_loan_id=None,
    limit_micro_trlusd=MicroTrlusd(500_000_000),
    drawn_micro_trlusd=MicroTrlusd(0),
    term_seconds=1800,
    maturity_at=Timestamp("2026-10-24T15:30:00Z"),
    spread_bps=1200,
    allowed_providers=(PROVIDER_A, PROVIDER_B),
    allowed_procurement_classes=(PROCUREMENT_CLASS.digest(),),
    minimum_evidence_tier=EvidenceTier.WITNESSED,
    purpose=Purpose.INFERENCE,
    owner_approval_digest=digest("owner-approval"),
    status=FacilityStatus.PENDING,
)
FACILITY_ACTIVE = replace(
    FACILITY_PENDING,
    xrpl_loan_id=XrplLedgerObjectId(label_hex("loan-object").upper()),
    drawn_micro_trlusd=MicroTrlusd(187),
    status=FacilityStatus.ACTIVE,
)
PURCHASE_REQUEST = PurchaseRequest(
    facility_id=FacilityId("facility-0001"),
    provider_id=PROVIDER_B,
    quote_id=QuoteId("quote-0001"),
    max_total_cost_micro_trlusd=MicroTrlusd(2_457_600),
    prompt_digest=digest("prompt"),
    nonce=Nonce(label_hex("nonce-0001")[:32]),
    expires_at=Timestamp("2026-10-24T15:04:00Z"),
)
RESERVATION = Reservation(
    reservation_id=ReservationId("reservation-0001"),
    facility_id=FacilityId("facility-0001"),
    quote_id=QuoteId("quote-0001"),
    maximum_amount_micro_trlusd=MicroTrlusd(2_457_600),
    created_at=Timestamp("2026-10-24T15:00:00Z"),
    expires_at=Timestamp("2026-10-24T15:10:00Z"),
    status=ReservationStatus.HELD,
)
ATTESTATION = ProviderAttestationBody(
    purchase_request_digest=PURCHASE_REQUEST.digest(),
    execution_profile_digest=PROFILE_B.digest(),
    response_digest=digest("response"),
    input_token_count=1234,
    output_token_count=1,
)
RECEIPT_TIER2 = DeliveryReceiptBody(
    purchase_request_digest=PURCHASE_REQUEST.digest(),
    execution_profile_digest=PROFILE_B.digest(),
    provider_id=PROVIDER_B,
    response_digest=digest("response"),
    input_token_count=1234,
    output_token_count=1,
    price_micro_trlusd=MicroTrlusd(187),
    started_at=Timestamp("2026-10-24T15:00:01Z"),
    completed_at=Timestamp("2026-10-24T15:00:03Z"),
    nonce=PURCHASE_REQUEST.nonce,
    evidence_tier=EvidenceTier.BOUND_AND_SIGNED,
    provider_attestation_digest=ATTESTATION.digest(),
    previous_receipt_digest=digest("previous-receipt"),
)
RECEIPT_TIER1 = replace(
    RECEIPT_TIER2,
    provider_id=PROVIDER_A,
    execution_profile_digest=PROFILE_A.digest(),
    evidence_tier=EvidenceTier.WITNESSED,
    provider_attestation_digest=None,
    previous_receipt_digest=None,
)
DECISION_ACCEPT = DecisionRecord(
    input_digests=(PURCHASE_REQUEST.digest(), QUOTE.digest(), FACILITY_ACTIVE.digest()),
    outcome=DecisionOutcome.ACCEPT,
    reason_code=None,
    engine_version=EngineVersion("credence-core-0.0.0"),
    decided_at=Timestamp("2026-10-24T15:00:00Z"),
)
DECISION_REJECT = DecisionRecord(
    input_digests=(PURCHASE_REQUEST.digest(), QUOTE.digest(), FACILITY_ACTIVE.digest()),
    outcome=DecisionOutcome.REJECT,
    reason_code=ReasonCode.OVER_LIMIT,
    engine_version=EngineVersion("credence-core-0.0.0"),
    decided_at=Timestamp("2026-10-24T15:00:00Z"),
)
SETTLEMENT = SettlementRecord(
    receipt_digest=RECEIPT_TIER2.digest(),
    decision_record_digest=DECISION_ACCEPT.digest(),
    xrpl_transaction_hash=XrplTransactionHash(label_hex("payment-transaction").upper()),
    settled_amount=MicroTrlusd(187),
    settlement_timestamp=Timestamp("2026-10-24T15:00:05Z"),
)
SIGNED = SignedObject(
    object_digest=RECEIPT_TIER2.digest(),
    key_id=KeyId("ed25519:" + label_hex("gateway-public-key")),
    signature=Ed25519Signature(
        hashlib.sha512(b"credence-c1-vector:illustrative-signature").hexdigest()
    ),
)

OBJECT_VECTORS: list[tuple[str, str, CanonicalObject]] = [
    (
        "execution_profile-full",
        "Tier 2 style profile; adapter digest present; fixed seed",
        PROFILE_B,
    ),
    (
        "execution_profile-not-asserted",
        "Null artifact/runtime/precision/quantization (NOT ASSERTED); null adapter digest",
        PROFILE_A,
    ),
    ("service_class-tier2", "Residency set given in canonical order", SERVICE_CLASS_B),
    (
        "service_class-non-ascii-text",
        "Non-ASCII NFC model family; empty residency set; null optional commitments",
        SERVICE_CLASS_NON_ASCII,
    ),
    ("procurement_class-basic", "All allowed_* sets in canonical order", PROCUREMENT_CLASS),
    ("quote-basic", "Integer micro-tRLUSD prices per million tokens and token caps", QUOTE),
    ("quote-large-integers", "Largest admitted integer (2**53 - 1) and zero", QUOTE_LARGE),
    (
        "facility-pending",
        "Pending facility; XRPL loan ID null before origination",
        FACILITY_PENDING,
    ),
    ("facility-active", "Active facility with an XRPL loan ID", FACILITY_ACTIVE),
    ("purchase_request-basic", "Prompt held only as a digest", PURCHASE_REQUEST),
    ("reservation-held", "Held reservation", RESERVATION),
    ("provider_attestation-basic", "Exactly the five provider-signed members", ATTESTATION),
    ("delivery_receipt-tier2", "Tier 2 with provider attestation digest, chained", RECEIPT_TIER2),
    ("delivery_receipt-tier1", "Tier 1, no attestation, first on its facility", RECEIPT_TIER1),
    ("settlement_record-basic", "Payment hash outside the receipt identity", SETTLEMENT),
    (
        "decision_record-accept",
        "Ordered input digests, preserved as given; transaction digest fields null",
        DECISION_ACCEPT,
    ),
    ("decision_record-reject", "Reject with its reason code", DECISION_REJECT),
    ("signed_object-basic", "Signature wrapper outside the signed body", SIGNED),
]


def mutate(obj: CanonicalObject, edit: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    value: dict[str, Any] = copy.deepcopy(obj.to_canonical())
    edit(value)
    return value


def setter(name: str, new: Any) -> Callable[[dict[str, Any]], None]:
    def edit(value: dict[str, Any]) -> None:
        value[name] = new

    return edit


def nested(outer: str, name: str, new: Any) -> Callable[[dict[str, Any]], None]:
    def edit(value: dict[str, Any]) -> None:
        value[outer][name] = new

    return edit


DOMAIN_REJECT_VECTORS: list[tuple[str, str, CanonicalObject, Callable[[dict[str, Any]], None]]] = [
    (
        "reject-float-money",
        "Money given as 150000.0",
        QUOTE,
        setter("input_price_per_million_micro_trlusd", 150000.0),
    ),
    ("reject-bool-as-integer", "Token cap given as true", QUOTE, setter("max_input_tokens", True)),
    (
        "reject-string-money",
        "Money given as a decimal string",
        QUOTE,
        setter("output_price_per_million_micro_trlusd", "600000"),
    ),
    (
        "reject-negative-money",
        "Negative drawn amount",
        FACILITY_ACTIVE,
        setter("drawn_micro_trlusd", -1),
    ),
    (
        "reject-negative-token-count",
        "Negative token count",
        ATTESTATION,
        setter("output_token_count", -1),
    ),
    (
        "reject-out-of-range-integer",
        "2**53 exceeds the admitted range",
        QUOTE,
        setter("max_input_tokens", MAX_SAFE_INTEGER + 1),
    ),
    (
        "reject-non-nfc-text",
        "NFD text in a domain string",
        PROFILE_B,
        setter("model_identifier", "mode\u0301le"),
    ),
    (
        "reject-unsorted-set",
        "Set member out of canonical order",
        PROCUREMENT_CLASS,
        setter("allowed_providers", ["provider-b", "provider-a"]),
    ),
    (
        "reject-duplicate-provider",
        "Duplicate allowed provider",
        PROCUREMENT_CLASS,
        setter("allowed_providers", ["provider-a", "provider-a"]),
    ),
    (
        "reject-empty-allowed-set",
        "Empty allowed_* set",
        FACILITY_ACTIVE,
        setter("allowed_providers", []),
    ),
    (
        "reject-upper-case-digest",
        "Digest hex in upper case",
        QUOTE,
        setter("service_class_digest", "sha256:" + label_hex("x").upper()),
    ),
    (
        "reject-unprefixed-digest",
        "Digest without sha256: prefix",
        QUOTE,
        setter("service_class_digest", label_hex("x")),
    ),
    (
        "reject-evidence-tier-out-of-range",
        "Evidence tier 4",
        SERVICE_CLASS_B,
        setter("evidence_tier", 4),
    ),
    (
        "reject-evidence-tier-bool",
        "Evidence tier given as true",
        SERVICE_CLASS_B,
        setter("evidence_tier", True),
    ),
    (
        "reject-evidence-tier-name",
        "Evidence tier given by name",
        SERVICE_CLASS_B,
        setter("evidence_tier", "Witnessed"),
    ),
    ("reject-empty-identifier", "Empty quote ID", QUOTE, setter("quote_id", "")),
    (
        "reject-timestamp-offset",
        "valid_until with +00:00 instead of Z",
        QUOTE,
        setter("valid_until", "2026-10-24T15:05:00+00:00"),
    ),
    (
        "reject-timestamp-fraction",
        "valid_until with fractional seconds",
        QUOTE,
        setter("valid_until", "2026-10-24T15:05:00.000Z"),
    ),
    ("reject-unknown-member", "A member the object does not define", QUOTE, setter("discount", 0)),
    (
        "reject-seed-without-fixed",
        "fixed seed policy without a seed",
        PROFILE_B,
        nested("sampling", "seed", None),
    ),
    (
        "reject-temperature-float",
        "Temperature as a float",
        PROFILE_B,
        nested("sampling", "temperature_milli", 0.7),
    ),
    (
        "reject-tier2-without-attestation",
        "Tier 2 receipt with no attestation digest",
        RECEIPT_TIER2,
        setter("provider_attestation_digest", None),
    ),
    (
        "reject-authorized-transaction-digest",
        "Transaction digest must stay null in C1",
        DECISION_ACCEPT,
        setter("authorized_transaction_digest", "sha256:" + label_hex("tx")),
    ),
    (
        "reject-wrong-object-type",
        "object_type of another class",
        QUOTE,
        setter("object_type", "credence.facility"),
    ),
]

SERIALIZER_VECTORS: list[tuple[str, str, str]] = [
    ("serializer-nfc-value", "NFD string value folds to NFC", '{"text":"mode\\u0301le"}'),
    ("serializer-nfc-member-name", "NFD member name folds to NFC", '{"mode\\u0301le":1}'),
    (
        "serializer-non-ascii-key-order",
        "JCS orders member names by UTF-16 code units (U+1F600 sorts before U+FB01)",
        '{"\\ufb01":5,"z":1,"\\u00e9":2,"a":3,"\\u65e5":4,"\\ud83d\\ude00":6}',
    ),
    (
        "serializer-large-integers",
        "+/-(2**53 - 1) admitted",
        '{"max":9007199254740991,"min":-9007199254740991}',
    ),
    ("serializer-array-order", "Array order is preserved", '{"list":[3,1,2,"b","a"]}'),
    (
        "serializer-reencoded",
        "Whitespace, member order and \\u escapes do not matter",
        '{ "b" : [ 1 ,2 ] ,\n  "a" : "\\u0041" }',
    ),
    ("serializer-reject-non-integral", "1.5 refused", '{"m":1.5}'),
    ("serializer-reject-out-of-range", "2**53 refused", '{"m":9007199254740992}'),
    (
        "serializer-reject-nfc-collision",
        "Names equal after NFC refused",
        '{"\\u00e9":1,"e\\u0301":2}',
    ),
    ("serializer-reject-duplicate-member", "Duplicate member refused", '{"m":1,"m":2}'),
    ("serializer-reject-lone-surrogate", "Lone surrogate refused", '{"m":"\\ud800"}'),
    (
        "serializer-reject-address-member",
        "Top-level address refused by the adapter",
        '{"a":1,"address":"x"}',
    ),
    ("serializer-reject-non-object", "Top level must be an object", "[1,2]"),
]

VALUE_VECTORS: list[tuple[str, str, Any]] = [
    ("serializer-reject-python-float", "A float value is refused before encoding", {"m": 1.0}),
]


def expected_form(form: kessai.CanonicalForm) -> dict[str, str]:
    return {
        "canonical_text": form.data.decode("utf-8"),
        "canonical_hex": form.data.hex(),
        "digest": "sha256:" + form.sha256_hex,
    }


def build() -> list[dict[str, Any]]:
    vectors: list[dict[str, Any]] = []
    for vid, description, obj in OBJECT_VECTORS:
        vectors.append(
            {
                "id": vid,
                "description": description,
                "entry": f"objects.{type(obj).__name__}.from_canonical",
                "input": obj.to_canonical(),
                "expected": expected_form(obj.canonical_form()),
            }
        )
    for vid, description, obj, edit in DOMAIN_REJECT_VECTORS:
        value = mutate(obj, edit)
        try:
            type(obj).from_canonical(value)
        except DomainError as exc:
            outcome = {"reject": {"reason": exc.reason, "field": exc.field}}
        else:
            raise SystemExit(f"{vid}: expected a rejection")
        vectors.append(
            {
                "id": vid,
                "description": description,
                "entry": f"objects.{type(obj).__name__}.from_canonical",
                "input": value,
                "expected": outcome,
            }
        )
    for vid, description, text in SERIALIZER_VECTORS:
        try:
            result: dict[str, Any] = expected_form(kessai.canonicalize_json_bytes(text.encode()))
        except kessai.SerializationRejected as exc:
            result = {"reject": {"reason": exc.reason}}
        vectors.append(
            {
                "id": vid,
                "description": description,
                "entry": "kessai.canonicalize_json_bytes",
                "input_json_text": text,
                "expected": result,
            }
        )
    for vid, description, value in VALUE_VECTORS:
        try:
            result = expected_form(kessai.canonicalize(value))
        except kessai.SerializationRejected as exc:
            result = {"reject": {"reason": exc.reason}}
        vectors.append(
            {
                "id": vid,
                "description": description,
                "entry": "kessai.canonicalize",
                "input": value,
                "expected": result,
            }
        )
    return vectors


def main() -> int:
    vectors = build()
    covered = {v["entry"].split(".")[1] for v in vectors if v["entry"].startswith("objects.")}
    missing = set(CLASSES) - covered
    if missing:
        raise SystemExit(f"no vector for {sorted(missing)}")
    OUT.mkdir(exist_ok=True)
    manifest: list[list[str]] = []
    for vector in vectors:
        text = json.dumps(vector, ensure_ascii=True, indent=2) + "\n"
        name = f"{vector['id']}.json"
        (OUT / name).write_text(text, encoding="ascii")
        manifest.append([hashlib.sha256(text.encode("ascii")).hexdigest(), name])
    # [sha256, file name] pairs, sorted by name: digest first, like a SHA256SUMS line.
    manifest.sort(key=lambda pair: pair[1])
    (OUT / "MANIFEST.json").write_text(json.dumps(manifest, indent=1) + "\n", encoding="ascii")
    print(f"wrote {len(vectors)} vectors to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
