"""Money, digest, identifier, timestamp and vocabulary primitives: valid values and strict refusals."""

from datetime import UTC, datetime
from enum import Enum

import pytest

from credence.core.primitives import (
    MAX_SAFE_INTEGER,
    MICRO_TRLUSD_PER_TRLUSD,
    CountryCode,
    DecisionOutcome,
    DecisionStage,
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
    nfc_text,
    parse_enum,
    strict_int,
)

HEX64 = "0123456789abcdef" * 4


def reason(fn: object, *args: object) -> str:
    with pytest.raises(DomainError) as exc:
        fn(*args)  # type: ignore[operator]
    return exc.value.reason


# --- integers and money --------------------------------------------------------------------


def test_strict_int_accepts_exact_ints_in_range() -> None:
    assert strict_int(0, "f") == 0
    assert strict_int(MAX_SAFE_INTEGER, "f") == MAX_SAFE_INTEGER
    assert strict_int(-5, "f", minimum=-10) == -5


@pytest.mark.parametrize(
    ("value", "code"),
    [
        (True, "BOOL_NOT_INTEGER"),
        (False, "BOOL_NOT_INTEGER"),
        (1.0, "FLOAT_NOT_INTEGER"),
        ("1", "NOT_AN_INTEGER"),
        (None, "NOT_AN_INTEGER"),
        (-1, "NEGATIVE_VALUE"),
        (MAX_SAFE_INTEGER + 1, "OUT_OF_RANGE"),
    ],
)
def test_strict_int_refuses(value: object, code: str) -> None:
    assert reason(strict_int, value, "f") == code


def test_money_unit() -> None:
    assert MICRO_TRLUSD_PER_TRLUSD == 1_000_000
    assert MicroTrlusd(500 * MICRO_TRLUSD_PER_TRLUSD).value == 500_000_000


@pytest.mark.parametrize(
    ("value", "code"),
    [
        (1.0, "FLOAT_NOT_INTEGER"),
        (True, "BOOL_NOT_INTEGER"),
        (-1, "NEGATIVE_VALUE"),
        ("1000000", "NOT_AN_INTEGER"),
        ("1.000000", "NOT_AN_INTEGER"),
        (MAX_SAFE_INTEGER + 1, "OUT_OF_RANGE"),
    ],
)
def test_money_refuses(value: object, code: str) -> None:
    assert reason(MicroTrlusd, value) == code


def test_money_adds_and_orders_only_with_money() -> None:
    assert MicroTrlusd(2) + MicroTrlusd(3) == MicroTrlusd(5)
    assert MicroTrlusd(2) < MicroTrlusd(3)
    with pytest.raises(TypeError):
        MicroTrlusd(2) + 3  # type: ignore[operator]
    assert reason(lambda: MicroTrlusd(MAX_SAFE_INTEGER) + MicroTrlusd(1)) == "OUT_OF_RANGE"


# --- digests, keys, ledger identifiers -----------------------------------------------------


def test_sha256_digest() -> None:
    digest = Sha256Digest("sha256:" + HEX64)
    assert digest.hex == HEX64
    assert Sha256Digest.from_hex(HEX64) == digest


@pytest.mark.parametrize(
    "value",
    [
        HEX64,
        "sha256:" + HEX64.upper(),
        "sha256:" + HEX64[:-1],
        "sha256:" + HEX64 + "0",
        "sha256:" + HEX64 + "\n",
        "SHA256:" + HEX64,
        "sha512:" + HEX64,
    ],
)
def test_sha256_digest_refuses_malformed(value: str) -> None:
    assert reason(Sha256Digest, value) == "MALFORMED_DIGEST"


def test_sha256_digest_refuses_non_string_and_empty() -> None:
    assert reason(Sha256Digest, b"sha256:" + HEX64.encode()) == "NOT_A_STRING"
    assert reason(Sha256Digest, "") == "EMPTY_VALUE"
    assert reason(Sha256Digest.from_hex, HEX64.upper()) == "MALFORMED_DIGEST"


def test_key_id_signature_and_xrpl_identifiers() -> None:
    assert KeyId("ed25519:" + HEX64).value.startswith("ed25519:")
    assert reason(KeyId, HEX64) == "MALFORMED_KEY_ID"
    assert Ed25519Signature(HEX64 * 2).value == HEX64 * 2
    assert reason(Ed25519Signature, HEX64) == "MALFORMED_SIGNATURE"
    assert XrplTransactionHash(HEX64.upper()).value == HEX64.upper()
    assert reason(XrplTransactionHash, HEX64) == "MALFORMED_TX_HASH"
    assert XrplLedgerObjectId(HEX64.upper()).value == HEX64.upper()
    assert reason(XrplLedgerObjectId, HEX64) == "MALFORMED_LEDGER_OBJECT_ID"


def test_xrpl_account_checks_the_checksum() -> None:
    valid = "rrrrrrrrrrrrrrrrrrrrBZbvji"
    assert XrplAccount(valid).value == valid
    assert reason(XrplAccount, valid[:-1] + "k") == "MALFORMED_ACCOUNT"
    assert reason(XrplAccount, "") == "EMPTY_VALUE"


def test_nonce_and_country_code() -> None:
    assert Nonce(HEX64[:32]).value == HEX64[:32]
    assert reason(Nonce, HEX64) == "MALFORMED_NONCE"
    assert reason(Nonce, HEX64[:32].upper()) == "MALFORMED_NONCE"
    assert CountryCode("DE").value == "DE"
    for bad in ("de", "DEU", "D", "D1"):
        assert reason(CountryCode, bad) == "MALFORMED_COUNTRY_CODE"


# --- timestamps ----------------------------------------------------------------------------


def test_timestamp_round_trips_and_orders() -> None:
    ts = Timestamp("2026-10-24T15:05:00Z")
    assert ts.to_datetime() == datetime(2026, 10, 24, 15, 5, tzinfo=UTC)
    assert Timestamp("2026-10-24T15:05:00Z") < Timestamp("2026-10-24T15:05:01Z")


@pytest.mark.parametrize(
    "value",
    [
        "2026-10-24T15:05:00+00:00",
        "2026-10-24T15:05:00.000Z",
        "2026-10-24T15:05:00z",
        "2026-10-24 15:05:00Z",
        "2026-10-24T15:05:00",
        "2026-1-24T15:05:00Z",
        "2026-02-30T00:00:00Z",
        "2026-10-24T24:00:00Z",
        "2026-12-31T23:59:60Z",
    ],
)
def test_timestamp_refuses_other_shapes(value: str) -> None:
    assert reason(Timestamp, value) == "MALFORMED_TIMESTAMP"


# --- identifiers and text ------------------------------------------------------------------


def test_identifiers() -> None:
    for cls in (FacilityId, QuoteId, ProviderId, ReservationId, EngineVersion):
        assert cls("a.b:c-d_0").value == "a.b:c-d_0"
        assert reason(cls, "") == "EMPTY_VALUE"
        for bad in ("-a", "a b", "caf\u00e9", "a" * 129, "a/b"):
            assert reason(cls, bad) == "MALFORMED_IDENTIFIER"
    assert FacilityId("x") != QuoteId("x")


def test_nfc_text() -> None:
    assert nfc_text("mod\u00e8le", "f") == "mod\u00e8le"
    assert reason(nfc_text, "mode\u0300le", "f") == "NOT_NFC"
    assert reason(nfc_text, "", "f") == "EMPTY_VALUE"
    assert reason(nfc_text, 1, "f") == "NOT_A_STRING"
    assert reason(nfc_text, "\ud800", "f") == "NON_SCALAR_UNICODE"


# --- closed vocabularies -------------------------------------------------------------------

SPELLINGS: list[tuple[type[Enum], dict[str, object]]] = [
    (
        EvidenceTier,
        {"REQUESTED": 0, "WITNESSED": 1, "BOUND_AND_SIGNED": 2, "MEASURED": 3},
    ),
    (DeliveryMode, {"INTERACTIVE": "interactive", "BATCH": "batch", "RESERVED": "reserved"}),
    (ServiceTier, {"FAST": "fast", "BASE": "base"}),
    (
        FacilityStatus,
        {
            "PENDING": "pending",
            "ACTIVE": "active",
            "FROZEN": "frozen",
            "REPAID": "repaid",
            "DEFAULTED": "defaulted",
        },
    ),
    (ReservationStatus, {"HELD": "held", "SETTLED": "settled", "RELEASED": "released"}),
    (Purpose, {"INFERENCE": "inference"}),
    (SeedPolicy, {"FIXED": "fixed", "PROVIDER": "provider", "NONE": "none"}),
    (ResidencyRegion, {"EU": "EU", "US": "US"}),
    (DecisionOutcome, {"ACCEPT": "accept", "REJECT": "reject"}),
    (DecisionStage, {"POLICY": "policy", "VERIFIER": "verifier"}),
]


@pytest.mark.parametrize(("enum_type", "spellings"), SPELLINGS)
def test_exact_serialized_spellings(enum_type: type[Enum], spellings: dict[str, object]) -> None:
    assert {m.name: m.value for m in enum_type} == spellings


def test_reason_codes_are_exactly_the_build_plan_codes() -> None:
    assert [m.value for m in ReasonCode] == [
        "SIG_INVALID",
        "KEY_REVOKED",
        "FACILITY_NOT_ACTIVE",
        "FACILITY_EXPIRED",
        "PURPOSE_MISMATCH",
        "PROVIDER_NOT_ALLOWED",
        "PROFILE_NOT_ALLOWED",
        "TIER_TOO_LOW",
        "PRICE_ABOVE_MAX",
        "OVER_LIMIT",
        "NONCE_REUSED",
        "RECEIPT_SIG_INVALID",
        "BINDING_MISMATCH",
        "RESPONSE_DIGEST_MISMATCH",
        "TOKEN_COUNT_EXCEEDS_QUOTE",
        "CHAIN_BROKEN",
        "REPLAYED_RECEIPT",
    ]


@pytest.mark.parametrize(
    ("enum_type", "value", "expected"),
    [
        (EvidenceTier, 2, EvidenceTier.BOUND_AND_SIGNED),
        (DeliveryMode, "interactive", DeliveryMode.INTERACTIVE),
        (ResidencyRegion, "EU", ResidencyRegion.EU),
    ],
)
def test_parse_enum(enum_type: type[Enum], value: object, expected: Enum) -> None:
    assert parse_enum(enum_type, value, "f") is expected


@pytest.mark.parametrize(
    ("enum_type", "value"),
    [
        (EvidenceTier, True),
        (EvidenceTier, 1.0),
        (EvidenceTier, "1"),
        (EvidenceTier, 4),
        (EvidenceTier, "WITNESSED"),
        (DeliveryMode, "Interactive"),
        (ResidencyRegion, "eu"),
        (SeedPolicy, None),
    ],
)
def test_parse_enum_refuses(enum_type: type[Enum], value: object) -> None:
    assert reason(parse_enum, enum_type, value, "f") == "INVALID_ENUM"
