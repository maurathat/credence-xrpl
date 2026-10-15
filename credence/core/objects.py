"""Typed objects.

Tech: Python

Responsibility (Domain model, build plan):
Six typed objects carry the whole product. Each is serialized with the Kessai serializer and identified by its digest, so any party can recompute and check it.

C1 representation (docs/decisions/0002-credence-core-c1-domain-model.md):
- Every object is a frozen dataclass that validates on construction and fails closed with a
  DomainError. Nothing is coerced or normalized.
- `to_canonical()` spells out every serialized member explicitly; no model library's JSON encoder
  defines identity. Each canonical object carries `object_type` and `schema_version`.
- Unsigned bodies carry identity. Signatures live only in SignedObject wrappers, never inside the
  body they sign. Payment hashes live only in SettlementRecord.
- Collections with no order (allowed_*, data_residency) must be given strictly ascending by their
  string value, so duplicates and alternative orders are refused rather than repaired.
  DecisionRecord.input_digests is ordered as given.
- Null execution fields mean NOT ASSERTED: never a wildcard, never satisfying a requirement.

No policy, verification, signing or XRPL behaviour lives here.

Owner: Credence Core.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import ClassVar, Protocol, Self

from credence.core import kessai
from credence.core.kessai import JsonValue
from credence.core.primitives import (
    MAX_SAFE_INTEGER,
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

SCHEMA_VERSION = "1"
AVAILABILITY_BPS_MAX = 10_000  # 10000 = 100.00%

type _Check = Callable[[object, str], object]
type _Parser[T] = Callable[[object, str], T]


class _Valued(Protocol):
    @property
    def value(self) -> str: ...


# --- construction-time checks --------------------------------------------------------------


def _is(cls: type) -> _Check:
    def check(value: object, field: str) -> object:
        if not isinstance(value, cls):
            raise DomainError(
                "WRONG_TYPE", field, f"expected {cls.__name__}, got {type(value).__name__}"
            )
        return value

    return check


def _optional(check: _Check) -> _Check:
    def optional(value: object, field: str) -> object:
        return None if value is None else check(value, field)

    return optional


def _int(minimum: int = 0, maximum: int = MAX_SAFE_INTEGER) -> _Check:
    def check(value: object, field: str) -> object:
        return strict_int(value, field, minimum=minimum, maximum=maximum)

    return check


def _sort_key(item: str | _Valued) -> str:
    return item if isinstance(item, str) else item.value


def _ordered(values: object, field: str, check: _Check, *, non_empty: bool) -> None:
    if not isinstance(values, tuple):
        raise DomainError("WRONG_TYPE", field, f"expected tuple, got {type(values).__name__}")
    for index, item in enumerate(values):
        check(item, f"{field}[{index}]")
    if non_empty and not values:
        raise DomainError("EMPTY_SET", field)


def _set(values: object, field: str, check: _Check, *, non_empty: bool) -> None:
    """A set: a tuple given strictly ascending by string value, so it has one spelling."""
    _ordered(values, field, check, non_empty=non_empty)
    assert isinstance(values, tuple)
    keys = [_sort_key(item) for item in values]
    if len(set(keys)) != len(keys):
        raise DomainError("DUPLICATE_ENTRY", field)
    if keys != sorted(keys):
        raise DomainError("UNSORTED_SET", field)


_text: _Check = nfc_text


# --- canonical members ---------------------------------------------------------------------


def _v(item: _Valued | None) -> JsonValue:
    return None if item is None else item.value


def _e(member: Enum | None) -> JsonValue:
    if member is None:
        return None
    value = member.value
    assert isinstance(value, (int, str))
    return value


def _vs(items: tuple[str | _Valued, ...]) -> list[JsonValue]:
    return [_sort_key(item) for item in items]


class CanonicalObject:
    """An object with an explicit canonical form, canonical bytes and a SHA-256 digest."""

    OBJECT_TYPE: ClassVar[str]
    MEMBERS: ClassVar[tuple[str, ...]]

    def _canonical_members(self) -> dict[str, JsonValue]:
        raise NotImplementedError

    @classmethod
    def from_canonical(cls, value: object) -> Self:
        """Rebuild the object from its canonical form, refusing anything it would not produce."""
        raise NotImplementedError

    def to_canonical(self) -> dict[str, JsonValue]:
        members = self._canonical_members()
        if tuple(members) != self.MEMBERS:
            raise RuntimeError(f"{self.OBJECT_TYPE}: canonical members drifted from MEMBERS")
        return {"object_type": self.OBJECT_TYPE, "schema_version": SCHEMA_VERSION, **members}

    def canonical_form(self) -> kessai.CanonicalForm:
        return kessai.canonicalize(self.to_canonical())

    def canonical_bytes(self) -> bytes:
        return self.canonical_form().data

    def digest(self) -> Sha256Digest:
        return Sha256Digest.from_hex(self.canonical_form().sha256_hex)


# --- parsing from canonical form -----------------------------------------------------------


class _Members:
    """Reads members of one JSON object, refusing missing, unknown and mistyped members."""

    def __init__(
        self, value: object, members: tuple[str, ...], *, object_type: str | None, path: str = ""
    ) -> None:
        if not isinstance(value, dict):
            raise DomainError("NOT_AN_OBJECT", path or str(object_type))
        expected = set(members) | ({"object_type", "schema_version"} if object_type else set())
        unknown = sorted(str(key) for key in value if key not in expected)
        if unknown:
            raise DomainError("UNKNOWN_MEMBER", path + unknown[0])
        missing = sorted(name for name in expected if name not in value)
        if missing:
            raise DomainError("MISSING_MEMBER", path + missing[0])
        if object_type is not None:
            if value["object_type"] != object_type:
                raise DomainError("WRONG_OBJECT_TYPE", "object_type", repr(value["object_type"]))
            if value["schema_version"] != SCHEMA_VERSION:
                raise DomainError(
                    "UNSUPPORTED_SCHEMA_VERSION", "schema_version", repr(value["schema_version"])
                )
        self._value: Mapping[str, object] = value
        self._path = path

    def __call__[T](self, name: str, parse: _Parser[T]) -> T:
        return parse(self._value[name], self._path + name)


def _p_int(minimum: int = 0, maximum: int = MAX_SAFE_INTEGER) -> _Parser[int]:
    def parse(value: object, field: str) -> int:
        return strict_int(value, field, minimum=minimum, maximum=maximum)

    return parse


def _p_money(value: object, field: str) -> MicroTrlusd:
    return MicroTrlusd(strict_int(value, field))


def _p_text(value: object, field: str) -> str:
    return nfc_text(value, field)


def _p_wrapped[T](cls: Callable[[str], T]) -> _Parser[T]:
    def parse(value: object, field: str) -> T:
        try:
            return cls(value)  # type: ignore[arg-type]  # the constructor validates the type
        except DomainError as exc:
            raise DomainError(exc.reason, field) from None

    return parse


def _p_enum[E: Enum](cls: type[E]) -> _Parser[E]:
    def parse(value: object, field: str) -> E:
        return parse_enum(cls, value, field)

    return parse


def _p_null[T](parse: _Parser[T]) -> _Parser[T | None]:
    def nullable(value: object, field: str) -> T | None:
        return None if value is None else parse(value, field)

    return nullable


def _p_tuple[T](parse: _Parser[T]) -> _Parser[tuple[T, ...]]:
    def items(value: object, field: str) -> tuple[T, ...]:
        if not isinstance(value, list):
            raise DomainError("NOT_A_LIST", field, type(value).__name__)
        return tuple(parse(item, f"{field}[{index}]") for index, item in enumerate(value))

    return items


def _p_must_be_null(value: object, field: str) -> None:
    if value is not None:
        raise DomainError("MUST_BE_NULL_IN_C1", field)


_digest = _p_wrapped(Sha256Digest)
_country = _p_wrapped(CountryCode)
_provider = _p_wrapped(ProviderId)
_timestamp = _p_wrapped(Timestamp)


# --- execution profile ---------------------------------------------------------------------


@dataclass(frozen=True)
class SamplingPolicy:
    """Sampling terms of an execution profile. No floats: 700 milli means temperature 0.700.

    temperature_milli is the actual execution/request temperature, not an allowed range; ranges
    belong to later service/procurement constraints. Null means not asserted. `fixed` requires a
    seed; `provider` and `none` require seed null.
    """

    temperature_milli: int | None
    seed_policy: SeedPolicy
    seed: int | None

    MEMBERS: ClassVar[tuple[str, ...]] = ("temperature_milli", "seed_policy", "seed")

    def __post_init__(self) -> None:
        _optional(_int())(self.temperature_milli, "temperature_milli")
        _is(SeedPolicy)(self.seed_policy, "seed_policy")
        _optional(_int())(self.seed, "seed")
        if (self.seed_policy is SeedPolicy.FIXED) != (self.seed is not None):
            raise DomainError("SEED_POLICY_MISMATCH", "seed", self.seed_policy.value)

    def to_canonical(self) -> dict[str, JsonValue]:
        return {
            "temperature_milli": self.temperature_milli,
            "seed_policy": _e(self.seed_policy),
            "seed": self.seed,
        }

    @classmethod
    def parse(cls, value: object, field: str) -> Self:
        m = _Members(value, cls.MEMBERS, object_type=None, path=field + ".")
        temperature_milli = m("temperature_milli", _p_null(_p_int()))
        seed_policy = m("seed_policy", _p_enum(SeedPolicy))
        seed = m("seed", _p_null(_p_int()))
        try:
            return cls(temperature_milli=temperature_milli, seed_policy=seed_policy, seed=seed)
        except DomainError as exc:
            raise DomainError(exc.reason, f"{field}.{exc.field}") from None


@dataclass(frozen=True)
class ExecutionProfile(CanonicalObject):
    """What inference is being bought: technical identity only, no price, no delivery terms.

    Null in model_artifact_digest, tokenizer_digest, chat_template_digest, runtime, precision or
    quantization means NOT ASSERTED: it is never a wildcard and never satisfies a procurement
    requirement. A provider whose model name merely maps
    to public weights must leave model_artifact_digest null. quantization "none" is an assertion
    (no quantization); null is not.
    """

    model_identifier: str
    model_artifact_digest: Sha256Digest | None
    tokenizer_digest: Sha256Digest | None
    adapter_digest: Sha256Digest | None
    chat_template_digest: Sha256Digest | None
    runtime: str | None
    precision: str | None
    quantization: str | None
    context_min_tokens: int
    sampling: SamplingPolicy

    OBJECT_TYPE: ClassVar[str] = "credence.execution_profile"
    MEMBERS: ClassVar[tuple[str, ...]] = (
        "model_identifier",
        "model_artifact_digest",
        "tokenizer_digest",
        "adapter_digest",
        "chat_template_digest",
        "runtime",
        "precision",
        "quantization",
        "context_min_tokens",
        "sampling",
    )

    def __post_init__(self) -> None:
        _text(self.model_identifier, "model_identifier")
        _optional(_is(Sha256Digest))(self.model_artifact_digest, "model_artifact_digest")
        _optional(_is(Sha256Digest))(self.tokenizer_digest, "tokenizer_digest")
        _optional(_is(Sha256Digest))(self.adapter_digest, "adapter_digest")
        _optional(_is(Sha256Digest))(self.chat_template_digest, "chat_template_digest")
        _optional(_text)(self.runtime, "runtime")
        _optional(_text)(self.precision, "precision")
        _optional(_text)(self.quantization, "quantization")
        _int()(self.context_min_tokens, "context_min_tokens")
        _is(SamplingPolicy)(self.sampling, "sampling")

    def _canonical_members(self) -> dict[str, JsonValue]:
        return {
            "model_identifier": self.model_identifier,
            "model_artifact_digest": _v(self.model_artifact_digest),
            "tokenizer_digest": _v(self.tokenizer_digest),
            "adapter_digest": _v(self.adapter_digest),
            "chat_template_digest": _v(self.chat_template_digest),
            "runtime": self.runtime,
            "precision": self.precision,
            "quantization": self.quantization,
            "context_min_tokens": self.context_min_tokens,
            "sampling": self.sampling.to_canonical(),
        }

    @classmethod
    def from_canonical(cls, value: object) -> Self:
        m = _Members(value, cls.MEMBERS, object_type=cls.OBJECT_TYPE)
        return cls(
            model_identifier=m("model_identifier", _p_text),
            model_artifact_digest=m("model_artifact_digest", _p_null(_digest)),
            tokenizer_digest=m("tokenizer_digest", _p_null(_digest)),
            adapter_digest=m("adapter_digest", _p_null(_digest)),
            chat_template_digest=m("chat_template_digest", _p_null(_digest)),
            runtime=m("runtime", _p_null(_p_text)),
            precision=m("precision", _p_null(_p_text)),
            quantization=m("quantization", _p_null(_p_text)),
            context_min_tokens=m("context_min_tokens", _p_int()),
            sampling=m("sampling", SamplingPolicy.parse),
        )


# --- service class, procurement class, quote -----------------------------------------------


@dataclass(frozen=True)
class ServiceClass(CanonicalObject):
    """What a provider promises to deliver around one execution profile. No price.

    data_residency is a set of ISO 3166-1 alpha-2 codes; empty means none asserted. residency_region
    (EU or US) is a C1-only field.
    """

    execution_profile_digest: Sha256Digest
    model_family: str
    delivery_mode: DeliveryMode
    service_tier: ServiceTier
    jurisdiction: CountryCode | None
    data_residency: tuple[CountryCode, ...]
    residency_region: ResidencyRegion | None
    evidence_tier: EvidenceTier
    latency_p95_ms: int | None
    availability_bps: int | None

    OBJECT_TYPE: ClassVar[str] = "credence.service_class"
    MEMBERS: ClassVar[tuple[str, ...]] = (
        "execution_profile_digest",
        "model_family",
        "delivery_mode",
        "service_tier",
        "jurisdiction",
        "data_residency",
        "residency_region",
        "evidence_tier",
        "latency_p95_ms",
        "availability_bps",
    )

    def __post_init__(self) -> None:
        _is(Sha256Digest)(self.execution_profile_digest, "execution_profile_digest")
        _text(self.model_family, "model_family")
        _is(DeliveryMode)(self.delivery_mode, "delivery_mode")
        _is(ServiceTier)(self.service_tier, "service_tier")
        _optional(_is(CountryCode))(self.jurisdiction, "jurisdiction")
        _set(self.data_residency, "data_residency", _is(CountryCode), non_empty=False)
        _optional(_is(ResidencyRegion))(self.residency_region, "residency_region")
        _is(EvidenceTier)(self.evidence_tier, "evidence_tier")
        _optional(_int())(self.latency_p95_ms, "latency_p95_ms")
        _optional(_int(maximum=AVAILABILITY_BPS_MAX))(self.availability_bps, "availability_bps")

    def _canonical_members(self) -> dict[str, JsonValue]:
        return {
            "execution_profile_digest": _v(self.execution_profile_digest),
            "model_family": self.model_family,
            "delivery_mode": _e(self.delivery_mode),
            "service_tier": _e(self.service_tier),
            "jurisdiction": _v(self.jurisdiction),
            "data_residency": _vs(self.data_residency),
            "residency_region": _e(self.residency_region),
            "evidence_tier": _e(self.evidence_tier),
            "latency_p95_ms": self.latency_p95_ms,
            "availability_bps": self.availability_bps,
        }

    @classmethod
    def from_canonical(cls, value: object) -> Self:
        m = _Members(value, cls.MEMBERS, object_type=cls.OBJECT_TYPE)
        return cls(
            execution_profile_digest=m("execution_profile_digest", _digest),
            model_family=m("model_family", _p_text),
            delivery_mode=m("delivery_mode", _p_enum(DeliveryMode)),
            service_tier=m("service_tier", _p_enum(ServiceTier)),
            jurisdiction=m("jurisdiction", _p_null(_country)),
            data_residency=m("data_residency", _p_tuple(_country)),
            residency_region=m("residency_region", _p_null(_p_enum(ResidencyRegion))),
            evidence_tier=m("evidence_tier", _p_enum(EvidenceTier)),
            latency_p95_ms=m("latency_p95_ms", _p_null(_p_int())),
            availability_bps=m("availability_bps", _p_null(_p_int(maximum=AVAILABILITY_BPS_MAX))),
        )


@dataclass(frozen=True)
class ProcurementClass(CanonicalObject):
    """What a buyer will accept as a substitute. It does not claim profiles are identical.

    Every allowed_* set is non-empty. There is no precision ranking: precision and quantization
    match by membership only, and a null (not asserted) provider value matches nothing.
    """

    model_family: str
    allowed_precisions: tuple[str, ...]
    allowed_quantizations: tuple[str, ...]
    minimum_evidence_tier: EvidenceTier
    delivery_mode: DeliveryMode
    allowed_jurisdictions: tuple[CountryCode, ...]
    allowed_data_residencies: tuple[CountryCode, ...]
    allowed_providers: tuple[ProviderId, ...]

    OBJECT_TYPE: ClassVar[str] = "credence.procurement_class"
    MEMBERS: ClassVar[tuple[str, ...]] = (
        "model_family",
        "allowed_precisions",
        "allowed_quantizations",
        "minimum_evidence_tier",
        "delivery_mode",
        "allowed_jurisdictions",
        "allowed_data_residencies",
        "allowed_providers",
    )

    def __post_init__(self) -> None:
        _text(self.model_family, "model_family")
        _set(self.allowed_precisions, "allowed_precisions", _text, non_empty=True)
        _set(self.allowed_quantizations, "allowed_quantizations", _text, non_empty=True)
        _is(EvidenceTier)(self.minimum_evidence_tier, "minimum_evidence_tier")
        _is(DeliveryMode)(self.delivery_mode, "delivery_mode")
        _set(self.allowed_jurisdictions, "allowed_jurisdictions", _is(CountryCode), non_empty=True)
        _set(
            self.allowed_data_residencies,
            "allowed_data_residencies",
            _is(CountryCode),
            non_empty=True,
        )
        _set(self.allowed_providers, "allowed_providers", _is(ProviderId), non_empty=True)

    def _canonical_members(self) -> dict[str, JsonValue]:
        return {
            "model_family": self.model_family,
            "allowed_precisions": _vs(self.allowed_precisions),
            "allowed_quantizations": _vs(self.allowed_quantizations),
            "minimum_evidence_tier": _e(self.minimum_evidence_tier),
            "delivery_mode": _e(self.delivery_mode),
            "allowed_jurisdictions": _vs(self.allowed_jurisdictions),
            "allowed_data_residencies": _vs(self.allowed_data_residencies),
            "allowed_providers": _vs(self.allowed_providers),
        }

    @classmethod
    def from_canonical(cls, value: object) -> Self:
        m = _Members(value, cls.MEMBERS, object_type=cls.OBJECT_TYPE)
        return cls(
            model_family=m("model_family", _p_text),
            allowed_precisions=m("allowed_precisions", _p_tuple(_p_text)),
            allowed_quantizations=m("allowed_quantizations", _p_tuple(_p_text)),
            minimum_evidence_tier=m("minimum_evidence_tier", _p_enum(EvidenceTier)),
            delivery_mode=m("delivery_mode", _p_enum(DeliveryMode)),
            allowed_jurisdictions=m("allowed_jurisdictions", _p_tuple(_country)),
            allowed_data_residencies=m("allowed_data_residencies", _p_tuple(_country)),
            allowed_providers=m("allowed_providers", _p_tuple(_provider)),
        )


@dataclass(frozen=True)
class Quote(CanonicalObject):
    """A time-limited price for one service class, with token caps. Billing: credence.core.billing."""

    quote_id: QuoteId
    provider_id: ProviderId
    service_class_digest: Sha256Digest
    input_price_per_million_micro_trlusd: MicroTrlusd
    output_price_per_million_micro_trlusd: MicroTrlusd
    max_input_tokens: int
    max_output_tokens: int
    valid_until: Timestamp

    OBJECT_TYPE: ClassVar[str] = "credence.quote"
    MEMBERS: ClassVar[tuple[str, ...]] = (
        "quote_id",
        "provider_id",
        "service_class_digest",
        "input_price_per_million_micro_trlusd",
        "output_price_per_million_micro_trlusd",
        "max_input_tokens",
        "max_output_tokens",
        "valid_until",
    )

    def __post_init__(self) -> None:
        _is(QuoteId)(self.quote_id, "quote_id")
        _is(ProviderId)(self.provider_id, "provider_id")
        _is(Sha256Digest)(self.service_class_digest, "service_class_digest")
        _is(MicroTrlusd)(
            self.input_price_per_million_micro_trlusd, "input_price_per_million_micro_trlusd"
        )
        _is(MicroTrlusd)(
            self.output_price_per_million_micro_trlusd, "output_price_per_million_micro_trlusd"
        )
        _int()(self.max_input_tokens, "max_input_tokens")
        _int()(self.max_output_tokens, "max_output_tokens")
        _is(Timestamp)(self.valid_until, "valid_until")

    def _canonical_members(self) -> dict[str, JsonValue]:
        return {
            "quote_id": _v(self.quote_id),
            "provider_id": _v(self.provider_id),
            "service_class_digest": _v(self.service_class_digest),
            "input_price_per_million_micro_trlusd": self.input_price_per_million_micro_trlusd.value,
            "output_price_per_million_micro_trlusd": self.output_price_per_million_micro_trlusd.value,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "valid_until": _v(self.valid_until),
        }

    @classmethod
    def from_canonical(cls, value: object) -> Self:
        m = _Members(value, cls.MEMBERS, object_type=cls.OBJECT_TYPE)
        return cls(
            quote_id=m("quote_id", _p_wrapped(QuoteId)),
            provider_id=m("provider_id", _provider),
            service_class_digest=m("service_class_digest", _digest),
            input_price_per_million_micro_trlusd=m(
                "input_price_per_million_micro_trlusd", _p_money
            ),
            output_price_per_million_micro_trlusd=m(
                "output_price_per_million_micro_trlusd", _p_money
            ),
            max_input_tokens=m("max_input_tokens", _p_int()),
            max_output_tokens=m("max_output_tokens", _p_int()),
            valid_until=m("valid_until", _timestamp),
        )


# --- facility, purchase request, reservation -----------------------------------------------


@dataclass(frozen=True)
class Facility(CanonicalObject):
    """A purpose-bound inference credit facility. xrpl_loan_id is null before origination.

    The repayment schedule is not modelled in C1.
    """

    facility_id: FacilityId
    borrower_account: XrplAccount
    xrpl_loan_id: XrplLedgerObjectId | None
    limit_micro_trlusd: MicroTrlusd
    drawn_micro_trlusd: MicroTrlusd
    term_seconds: int
    maturity_at: Timestamp
    spread_bps: int
    allowed_providers: tuple[ProviderId, ...]
    allowed_procurement_classes: tuple[Sha256Digest, ...]
    minimum_evidence_tier: EvidenceTier
    purpose: Purpose
    owner_approval_digest: Sha256Digest
    status: FacilityStatus

    OBJECT_TYPE: ClassVar[str] = "credence.facility"
    MEMBERS: ClassVar[tuple[str, ...]] = (
        "facility_id",
        "borrower_account",
        "xrpl_loan_id",
        "limit_micro_trlusd",
        "drawn_micro_trlusd",
        "term_seconds",
        "maturity_at",
        "spread_bps",
        "allowed_providers",
        "allowed_procurement_classes",
        "minimum_evidence_tier",
        "purpose",
        "owner_approval_digest",
        "status",
    )

    def __post_init__(self) -> None:
        _is(FacilityId)(self.facility_id, "facility_id")
        _is(XrplAccount)(self.borrower_account, "borrower_account")
        _optional(_is(XrplLedgerObjectId))(self.xrpl_loan_id, "xrpl_loan_id")
        _is(MicroTrlusd)(self.limit_micro_trlusd, "limit_micro_trlusd")
        _is(MicroTrlusd)(self.drawn_micro_trlusd, "drawn_micro_trlusd")
        _int()(self.term_seconds, "term_seconds")
        _is(Timestamp)(self.maturity_at, "maturity_at")
        _int()(self.spread_bps, "spread_bps")
        _set(self.allowed_providers, "allowed_providers", _is(ProviderId), non_empty=True)
        _set(
            self.allowed_procurement_classes,
            "allowed_procurement_classes",
            _is(Sha256Digest),
            non_empty=True,
        )
        _is(EvidenceTier)(self.minimum_evidence_tier, "minimum_evidence_tier")
        _is(Purpose)(self.purpose, "purpose")
        _is(Sha256Digest)(self.owner_approval_digest, "owner_approval_digest")
        _is(FacilityStatus)(self.status, "status")

    def _canonical_members(self) -> dict[str, JsonValue]:
        return {
            "facility_id": _v(self.facility_id),
            "borrower_account": _v(self.borrower_account),
            "xrpl_loan_id": _v(self.xrpl_loan_id),
            "limit_micro_trlusd": self.limit_micro_trlusd.value,
            "drawn_micro_trlusd": self.drawn_micro_trlusd.value,
            "term_seconds": self.term_seconds,
            "maturity_at": _v(self.maturity_at),
            "spread_bps": self.spread_bps,
            "allowed_providers": _vs(self.allowed_providers),
            "allowed_procurement_classes": _vs(self.allowed_procurement_classes),
            "minimum_evidence_tier": _e(self.minimum_evidence_tier),
            "purpose": _e(self.purpose),
            "owner_approval_digest": _v(self.owner_approval_digest),
            "status": _e(self.status),
        }

    @classmethod
    def from_canonical(cls, value: object) -> Self:
        m = _Members(value, cls.MEMBERS, object_type=cls.OBJECT_TYPE)
        return cls(
            facility_id=m("facility_id", _p_wrapped(FacilityId)),
            borrower_account=m("borrower_account", _p_wrapped(XrplAccount)),
            xrpl_loan_id=m("xrpl_loan_id", _p_null(_p_wrapped(XrplLedgerObjectId))),
            limit_micro_trlusd=m("limit_micro_trlusd", _p_money),
            drawn_micro_trlusd=m("drawn_micro_trlusd", _p_money),
            term_seconds=m("term_seconds", _p_int()),
            maturity_at=m("maturity_at", _timestamp),
            spread_bps=m("spread_bps", _p_int()),
            allowed_providers=m("allowed_providers", _p_tuple(_provider)),
            allowed_procurement_classes=m("allowed_procurement_classes", _p_tuple(_digest)),
            minimum_evidence_tier=m("minimum_evidence_tier", _p_enum(EvidenceTier)),
            purpose=m("purpose", _p_enum(Purpose)),
            owner_approval_digest=m("owner_approval_digest", _digest),
            status=m("status", _p_enum(FacilityStatus)),
        )


@dataclass(frozen=True)
class PurchaseRequest(CanonicalObject):
    """The agent's request to buy under a quote. The prompt itself is never stored, only its digest.

    purpose is part of its identity; `inference` is the only value. The agent's signature is a
    SignedObject over this object's digest, not a member of it.
    """

    facility_id: FacilityId
    provider_id: ProviderId
    quote_id: QuoteId
    purpose: Purpose
    max_total_cost_micro_trlusd: MicroTrlusd
    prompt_digest: Sha256Digest
    nonce: Nonce
    expires_at: Timestamp

    OBJECT_TYPE: ClassVar[str] = "credence.purchase_request"
    MEMBERS: ClassVar[tuple[str, ...]] = (
        "facility_id",
        "provider_id",
        "quote_id",
        "purpose",
        "max_total_cost_micro_trlusd",
        "prompt_digest",
        "nonce",
        "expires_at",
    )

    def __post_init__(self) -> None:
        _is(FacilityId)(self.facility_id, "facility_id")
        _is(ProviderId)(self.provider_id, "provider_id")
        _is(QuoteId)(self.quote_id, "quote_id")
        _is(Purpose)(self.purpose, "purpose")
        _is(MicroTrlusd)(self.max_total_cost_micro_trlusd, "max_total_cost_micro_trlusd")
        _is(Sha256Digest)(self.prompt_digest, "prompt_digest")
        _is(Nonce)(self.nonce, "nonce")
        _is(Timestamp)(self.expires_at, "expires_at")

    def _canonical_members(self) -> dict[str, JsonValue]:
        return {
            "facility_id": _v(self.facility_id),
            "provider_id": _v(self.provider_id),
            "quote_id": _v(self.quote_id),
            "purpose": _e(self.purpose),
            "max_total_cost_micro_trlusd": self.max_total_cost_micro_trlusd.value,
            "prompt_digest": _v(self.prompt_digest),
            "nonce": _v(self.nonce),
            "expires_at": _v(self.expires_at),
        }

    @classmethod
    def from_canonical(cls, value: object) -> Self:
        m = _Members(value, cls.MEMBERS, object_type=cls.OBJECT_TYPE)
        return cls(
            facility_id=m("facility_id", _p_wrapped(FacilityId)),
            provider_id=m("provider_id", _provider),
            quote_id=m("quote_id", _p_wrapped(QuoteId)),
            purpose=m("purpose", _p_enum(Purpose)),
            max_total_cost_micro_trlusd=m("max_total_cost_micro_trlusd", _p_money),
            prompt_digest=m("prompt_digest", _digest),
            nonce=m("nonce", _p_wrapped(Nonce)),
            expires_at=m("expires_at", _timestamp),
        )


@dataclass(frozen=True)
class Reservation(CanonicalObject):
    """An internal accounting hold against a facility for one pending purchase. Not funds on XRPL."""

    reservation_id: ReservationId
    facility_id: FacilityId
    quote_id: QuoteId
    maximum_amount_micro_trlusd: MicroTrlusd
    created_at: Timestamp
    expires_at: Timestamp
    status: ReservationStatus

    OBJECT_TYPE: ClassVar[str] = "credence.reservation"
    MEMBERS: ClassVar[tuple[str, ...]] = (
        "reservation_id",
        "facility_id",
        "quote_id",
        "maximum_amount_micro_trlusd",
        "created_at",
        "expires_at",
        "status",
    )

    def __post_init__(self) -> None:
        _is(ReservationId)(self.reservation_id, "reservation_id")
        _is(FacilityId)(self.facility_id, "facility_id")
        _is(QuoteId)(self.quote_id, "quote_id")
        _is(MicroTrlusd)(self.maximum_amount_micro_trlusd, "maximum_amount_micro_trlusd")
        _is(Timestamp)(self.created_at, "created_at")
        _is(Timestamp)(self.expires_at, "expires_at")
        _is(ReservationStatus)(self.status, "status")
        if not self.expires_at > self.created_at:
            raise DomainError("TIMESTAMP_ORDER", "expires_at", "must be after created_at")

    def _canonical_members(self) -> dict[str, JsonValue]:
        return {
            "reservation_id": _v(self.reservation_id),
            "facility_id": _v(self.facility_id),
            "quote_id": _v(self.quote_id),
            "maximum_amount_micro_trlusd": self.maximum_amount_micro_trlusd.value,
            "created_at": _v(self.created_at),
            "expires_at": _v(self.expires_at),
            "status": _e(self.status),
        }

    @classmethod
    def from_canonical(cls, value: object) -> Self:
        m = _Members(value, cls.MEMBERS, object_type=cls.OBJECT_TYPE)
        return cls(
            reservation_id=m("reservation_id", _p_wrapped(ReservationId)),
            facility_id=m("facility_id", _p_wrapped(FacilityId)),
            quote_id=m("quote_id", _p_wrapped(QuoteId)),
            maximum_amount_micro_trlusd=m("maximum_amount_micro_trlusd", _p_money),
            created_at=m("created_at", _timestamp),
            expires_at=m("expires_at", _timestamp),
            status=m("status", _p_enum(ReservationStatus)),
        )


# --- receipts and settlement ---------------------------------------------------------------


@dataclass(frozen=True)
class ProviderAttestationBody(CanonicalObject):
    """What a Tier 2 provider signs: exactly these five members. The provider signs its digest."""

    purchase_request_digest: Sha256Digest
    execution_profile_digest: Sha256Digest
    response_digest: Sha256Digest
    input_token_count: int
    output_token_count: int

    OBJECT_TYPE: ClassVar[str] = "credence.provider_attestation"
    MEMBERS: ClassVar[tuple[str, ...]] = (
        "purchase_request_digest",
        "execution_profile_digest",
        "response_digest",
        "input_token_count",
        "output_token_count",
    )

    def __post_init__(self) -> None:
        _is(Sha256Digest)(self.purchase_request_digest, "purchase_request_digest")
        _is(Sha256Digest)(self.execution_profile_digest, "execution_profile_digest")
        _is(Sha256Digest)(self.response_digest, "response_digest")
        _int()(self.input_token_count, "input_token_count")
        _int()(self.output_token_count, "output_token_count")

    def _canonical_members(self) -> dict[str, JsonValue]:
        return {
            "purchase_request_digest": _v(self.purchase_request_digest),
            "execution_profile_digest": _v(self.execution_profile_digest),
            "response_digest": _v(self.response_digest),
            "input_token_count": self.input_token_count,
            "output_token_count": self.output_token_count,
        }

    @classmethod
    def from_canonical(cls, value: object) -> Self:
        m = _Members(value, cls.MEMBERS, object_type=cls.OBJECT_TYPE)
        return cls(
            purchase_request_digest=m("purchase_request_digest", _digest),
            execution_profile_digest=m("execution_profile_digest", _digest),
            response_digest=m("response_digest", _digest),
            input_token_count=m("input_token_count", _p_int()),
            output_token_count=m("output_token_count", _p_int()),
        )


@dataclass(frozen=True)
class DeliveryReceiptBody(CanonicalObject):
    """The canonical receipt identity: its digest is the receipt-chain and replay identity.

    It excludes signatures (SignedObject) and the XRPL payment hash (SettlementRecord). The gateway
    signs this body. Tier 1 (witnessed) carries no provider attestation; Tier 2 (bound and signed)
    must carry the digest of the provider-signed ProviderAttestationBody. Tiers 0 and 3 are not
    receipt tiers in C1. The response itself is never stored, only its digest.
    """

    purchase_request_digest: Sha256Digest
    execution_profile_digest: Sha256Digest
    provider_id: ProviderId
    response_digest: Sha256Digest
    input_token_count: int
    output_token_count: int
    price_micro_trlusd: MicroTrlusd
    started_at: Timestamp
    completed_at: Timestamp
    nonce: Nonce
    evidence_tier: EvidenceTier
    provider_attestation_digest: Sha256Digest | None
    previous_receipt_digest: Sha256Digest | None

    OBJECT_TYPE: ClassVar[str] = "credence.delivery_receipt"
    MEMBERS: ClassVar[tuple[str, ...]] = (
        "purchase_request_digest",
        "execution_profile_digest",
        "provider_id",
        "response_digest",
        "input_token_count",
        "output_token_count",
        "price_micro_trlusd",
        "started_at",
        "completed_at",
        "nonce",
        "evidence_tier",
        "provider_attestation_digest",
        "previous_receipt_digest",
    )

    def __post_init__(self) -> None:
        _is(Sha256Digest)(self.purchase_request_digest, "purchase_request_digest")
        _is(Sha256Digest)(self.execution_profile_digest, "execution_profile_digest")
        _is(ProviderId)(self.provider_id, "provider_id")
        _is(Sha256Digest)(self.response_digest, "response_digest")
        _int()(self.input_token_count, "input_token_count")
        _int()(self.output_token_count, "output_token_count")
        _is(MicroTrlusd)(self.price_micro_trlusd, "price_micro_trlusd")
        _is(Timestamp)(self.started_at, "started_at")
        _is(Timestamp)(self.completed_at, "completed_at")
        _is(Nonce)(self.nonce, "nonce")
        _is(EvidenceTier)(self.evidence_tier, "evidence_tier")
        _optional(_is(Sha256Digest))(
            self.provider_attestation_digest, "provider_attestation_digest"
        )
        _optional(_is(Sha256Digest))(self.previous_receipt_digest, "previous_receipt_digest")
        if self.completed_at < self.started_at:
            raise DomainError("TIMESTAMP_ORDER", "completed_at", "must not precede started_at")
        if self.evidence_tier is EvidenceTier.WITNESSED:
            if self.provider_attestation_digest is not None:
                raise DomainError("TIER_ATTESTATION_MISMATCH", "provider_attestation_digest")
        elif self.evidence_tier is EvidenceTier.BOUND_AND_SIGNED:
            if self.provider_attestation_digest is None:
                raise DomainError("TIER_ATTESTATION_MISMATCH", "provider_attestation_digest")
        else:
            raise DomainError("UNSUPPORTED_RECEIPT_TIER", "evidence_tier", self.evidence_tier.name)

    def _canonical_members(self) -> dict[str, JsonValue]:
        return {
            "purchase_request_digest": _v(self.purchase_request_digest),
            "execution_profile_digest": _v(self.execution_profile_digest),
            "provider_id": _v(self.provider_id),
            "response_digest": _v(self.response_digest),
            "input_token_count": self.input_token_count,
            "output_token_count": self.output_token_count,
            "price_micro_trlusd": self.price_micro_trlusd.value,
            "started_at": _v(self.started_at),
            "completed_at": _v(self.completed_at),
            "nonce": _v(self.nonce),
            "evidence_tier": _e(self.evidence_tier),
            "provider_attestation_digest": _v(self.provider_attestation_digest),
            "previous_receipt_digest": _v(self.previous_receipt_digest),
        }

    @classmethod
    def from_canonical(cls, value: object) -> Self:
        m = _Members(value, cls.MEMBERS, object_type=cls.OBJECT_TYPE)
        return cls(
            purchase_request_digest=m("purchase_request_digest", _digest),
            execution_profile_digest=m("execution_profile_digest", _digest),
            provider_id=m("provider_id", _provider),
            response_digest=m("response_digest", _digest),
            input_token_count=m("input_token_count", _p_int()),
            output_token_count=m("output_token_count", _p_int()),
            price_micro_trlusd=m("price_micro_trlusd", _p_money),
            started_at=m("started_at", _timestamp),
            completed_at=m("completed_at", _timestamp),
            nonce=m("nonce", _p_wrapped(Nonce)),
            evidence_tier=m("evidence_tier", _p_enum(EvidenceTier)),
            provider_attestation_digest=m("provider_attestation_digest", _p_null(_digest)),
            previous_receipt_digest=m("previous_receipt_digest", _p_null(_digest)),
        )


@dataclass(frozen=True)
class SettlementRecord(CanonicalObject):
    """Links a receipt and the decision that permitted its payment to the XRPL payment hash."""

    receipt_digest: Sha256Digest
    decision_record_digest: Sha256Digest
    xrpl_transaction_hash: XrplTransactionHash
    settled_amount: MicroTrlusd
    settlement_timestamp: Timestamp

    OBJECT_TYPE: ClassVar[str] = "credence.settlement_record"
    MEMBERS: ClassVar[tuple[str, ...]] = (
        "receipt_digest",
        "decision_record_digest",
        "xrpl_transaction_hash",
        "settled_amount",
        "settlement_timestamp",
    )

    def __post_init__(self) -> None:
        _is(Sha256Digest)(self.receipt_digest, "receipt_digest")
        _is(Sha256Digest)(self.decision_record_digest, "decision_record_digest")
        _is(XrplTransactionHash)(self.xrpl_transaction_hash, "xrpl_transaction_hash")
        _is(MicroTrlusd)(self.settled_amount, "settled_amount")
        _is(Timestamp)(self.settlement_timestamp, "settlement_timestamp")

    def _canonical_members(self) -> dict[str, JsonValue]:
        return {
            "receipt_digest": _v(self.receipt_digest),
            "decision_record_digest": _v(self.decision_record_digest),
            "xrpl_transaction_hash": _v(self.xrpl_transaction_hash),
            "settled_amount": self.settled_amount.value,
            "settlement_timestamp": _v(self.settlement_timestamp),
        }

    @classmethod
    def from_canonical(cls, value: object) -> Self:
        m = _Members(value, cls.MEMBERS, object_type=cls.OBJECT_TYPE)
        return cls(
            receipt_digest=m("receipt_digest", _digest),
            decision_record_digest=m("decision_record_digest", _digest),
            xrpl_transaction_hash=m("xrpl_transaction_hash", _p_wrapped(XrplTransactionHash)),
            settled_amount=m("settled_amount", _p_money),
            settlement_timestamp=m("settlement_timestamp", _timestamp),
        )


# --- decisions and signatures --------------------------------------------------------------


@dataclass(frozen=True)
class DecisionRecord(CanonicalObject):
    """An accept or reject decision with the digests of its inputs, in the order given.

    decision_stage (`policy` or `verifier`) is part of its identity. accept carries no reason code; reject carries exactly one. The authorized-transaction fields
    exist but must be null in C1: the transaction digest scheme is chosen at the XRPL
    authorization/settlement checkpoint, not here.
    """

    decision_stage: DecisionStage
    input_digests: tuple[Sha256Digest, ...]
    outcome: DecisionOutcome
    reason_code: ReasonCode | None
    engine_version: EngineVersion
    decided_at: Timestamp
    authorized_transaction_digest: None = None
    authorized_transaction_digest_scheme: None = None

    OBJECT_TYPE: ClassVar[str] = "credence.decision_record"
    MEMBERS: ClassVar[tuple[str, ...]] = (
        "decision_stage",
        "input_digests",
        "outcome",
        "reason_code",
        "engine_version",
        "decided_at",
        "authorized_transaction_digest",
        "authorized_transaction_digest_scheme",
    )

    def __post_init__(self) -> None:
        _is(DecisionStage)(self.decision_stage, "decision_stage")
        _ordered(self.input_digests, "input_digests", _is(Sha256Digest), non_empty=True)
        _is(DecisionOutcome)(self.outcome, "outcome")
        _optional(_is(ReasonCode))(self.reason_code, "reason_code")
        _is(EngineVersion)(self.engine_version, "engine_version")
        _is(Timestamp)(self.decided_at, "decided_at")
        _p_must_be_null(self.authorized_transaction_digest, "authorized_transaction_digest")
        _p_must_be_null(
            self.authorized_transaction_digest_scheme, "authorized_transaction_digest_scheme"
        )
        if (self.outcome is DecisionOutcome.REJECT) != (self.reason_code is not None):
            raise DomainError("DECISION_REASON_MISMATCH", "reason_code", self.outcome.value)

    def _canonical_members(self) -> dict[str, JsonValue]:
        return {
            "decision_stage": _e(self.decision_stage),
            "input_digests": _vs(self.input_digests),
            "outcome": _e(self.outcome),
            "reason_code": _e(self.reason_code),
            "engine_version": _v(self.engine_version),
            "decided_at": _v(self.decided_at),
            "authorized_transaction_digest": None,
            "authorized_transaction_digest_scheme": None,
        }

    @classmethod
    def from_canonical(cls, value: object) -> Self:
        m = _Members(value, cls.MEMBERS, object_type=cls.OBJECT_TYPE)
        m("authorized_transaction_digest", _p_must_be_null)
        m("authorized_transaction_digest_scheme", _p_must_be_null)
        return cls(
            decision_stage=m("decision_stage", _p_enum(DecisionStage)),
            input_digests=m("input_digests", _p_tuple(_digest)),
            outcome=m("outcome", _p_enum(DecisionOutcome)),
            reason_code=m("reason_code", _p_null(_p_enum(ReasonCode))),
            engine_version=m("engine_version", _p_wrapped(EngineVersion)),
            decided_at=m("decided_at", _timestamp),
        )


@dataclass(frozen=True)
class SignedObject(CanonicalObject):
    """A signature over an object's digest, kept outside the body it signs. Not verified in C1."""

    object_digest: Sha256Digest
    key_id: KeyId
    signature: Ed25519Signature

    OBJECT_TYPE: ClassVar[str] = "credence.signed_object"
    MEMBERS: ClassVar[tuple[str, ...]] = ("object_digest", "key_id", "signature")

    def __post_init__(self) -> None:
        _is(Sha256Digest)(self.object_digest, "object_digest")
        _is(KeyId)(self.key_id, "key_id")
        _is(Ed25519Signature)(self.signature, "signature")

    def _canonical_members(self) -> dict[str, JsonValue]:
        return {
            "object_digest": _v(self.object_digest),
            "key_id": _v(self.key_id),
            "signature": _v(self.signature),
        }

    @classmethod
    def from_canonical(cls, value: object) -> Self:
        m = _Members(value, cls.MEMBERS, object_type=cls.OBJECT_TYPE)
        return cls(
            object_digest=m("object_digest", _digest),
            key_id=m("key_id", _p_wrapped(KeyId)),
            signature=m("signature", _p_wrapped(Ed25519Signature)),
        )


OBJECT_TYPES: tuple[type[CanonicalObject], ...] = (
    ExecutionProfile,
    ServiceClass,
    ProcurementClass,
    Quote,
    Facility,
    PurchaseRequest,
    Reservation,
    ProviderAttestationBody,
    DeliveryReceiptBody,
    SettlementRecord,
    DecisionRecord,
    SignedObject,
)
