"""Credence Core primitives: application money, digests, identifiers, timestamps and closed vocabularies.

Every constructor validates and fails closed with a DomainError carrying a reason code. Nothing is
coerced or normalized: a float, a bool, a non-NFC string or an upper-case digest is rejected, never
converted. Canonical serialization is the Kessai serializer's job (see credence.core.kessai).

Owner: Credence Core.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Self

from xrpl.core.addresscodec import is_valid_classic_address

# The Kessai serializer admits integers only within +/-(2**53 - 1) (canon.SAFE_INTEGER).
MAX_SAFE_INTEGER = 2**53 - 1

# Money rule (build plan, Domain model): 1 tRLUSD = 1,000,000 micro-units.
MICRO_TRLUSD_PER_TRLUSD = 1_000_000


class DomainError(ValueError):
    """A value that Credence Core refuses. `reason` is a stable code; `field` names the member."""

    def __init__(self, reason: str, field: str, detail: str = "") -> None:
        super().__init__(f"{reason} at {field}" + (f": {detail}" if detail else ""))
        self.reason = reason
        self.field = field


def strict_int(
    value: object, field: str, *, minimum: int = 0, maximum: int = MAX_SAFE_INTEGER
) -> int:
    """Return `value` if it is exactly an int within [minimum, maximum]; bool and float are refused."""
    if isinstance(value, bool):
        raise DomainError("BOOL_NOT_INTEGER", field)
    if isinstance(value, float):
        raise DomainError("FLOAT_NOT_INTEGER", field)
    if type(value) is not int:
        raise DomainError("NOT_AN_INTEGER", field, type(value).__name__)
    if value < 0 <= minimum:
        raise DomainError("NEGATIVE_VALUE", field, str(value))
    if not minimum <= value <= maximum:
        raise DomainError("OUT_OF_RANGE", field, f"{value} not in [{minimum}, {maximum}]")
    return value


def nfc_text(value: object, field: str) -> str:
    """Return `value` if it is a non-empty string of Unicode scalar values already in NFC.

    The serializer NFC-folds every string; refusing non-NFC input here keeps Python equality and
    digest equality the same thing, instead of normalizing silently.
    """
    if not isinstance(value, str):
        raise DomainError("NOT_A_STRING", field, type(value).__name__)
    if not value:
        raise DomainError("EMPTY_VALUE", field)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise DomainError("NON_SCALAR_UNICODE", field) from None
    if not unicodedata.is_normalized("NFC", value):
        raise DomainError("NOT_NFC", field)
    return value


def _pattern_string(value: object, field: str, pattern: re.Pattern[str], reason: str) -> None:
    if not isinstance(value, str):
        raise DomainError("NOT_A_STRING", field, type(value).__name__)
    if not value:
        raise DomainError("EMPTY_VALUE", field)
    if not pattern.fullmatch(value):
        raise DomainError(reason, field, value)


# --- money ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, order=True)
class MicroTrlusd:
    """Application money: a non-negative integer count of micro-tRLUSD (tRLUSD is a test token).

    This is Credence's accounting unit only. It is not an XRPL wire amount and has no conversion to
    or from XRPL decimal strings; that conversion is out of scope for C1.
    """

    value: int

    def __post_init__(self) -> None:
        strict_int(self.value, "micro_trlusd")

    def __add__(self, other: MicroTrlusd) -> MicroTrlusd:
        if not isinstance(other, MicroTrlusd):
            return NotImplemented
        return MicroTrlusd(self.value + other.value)


# --- digests, keys and ledger identifiers --------------------------------------------------

_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_KEY_ID = re.compile(r"ed25519:[0-9a-f]{64}")
_SIGNATURE = re.compile(r"[0-9a-f]{128}")
_XRPL_HASH = re.compile(r"[0-9A-F]{64}")
_NONCE = re.compile(r"[0-9a-f]{32}")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_COUNTRY = re.compile(r"[A-Z]{2}")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True, slots=True, order=True)
class Sha256Digest:
    """`sha256:` followed by 64 lower-case hex digits. Identifies bytes; it proves nothing more."""

    value: str

    def __post_init__(self) -> None:
        _pattern_string(self.value, "Sha256Digest", _SHA256, "MALFORMED_DIGEST")

    @classmethod
    def from_hex(cls, hex_digest: str) -> Self:
        _pattern_string(hex_digest, "Sha256Digest", _HEX64, "MALFORMED_DIGEST")
        return cls("sha256:" + hex_digest)

    @property
    def hex(self) -> str:
        return self.value.removeprefix("sha256:")


@dataclass(frozen=True, slots=True, order=True)
class KeyId:
    """A signer's public key identifier: `ed25519:` followed by the 32-byte public key in hex."""

    value: str

    def __post_init__(self) -> None:
        _pattern_string(self.value, "KeyId", _KEY_ID, "MALFORMED_KEY_ID")


@dataclass(frozen=True, slots=True)
class Ed25519Signature:
    """A 64-byte Ed25519 signature in lower-case hex. C1 carries it; it does not verify it."""

    value: str

    def __post_init__(self) -> None:
        _pattern_string(self.value, "Ed25519Signature", _SIGNATURE, "MALFORMED_SIGNATURE")


@dataclass(frozen=True, slots=True, order=True)
class XrplTransactionHash:
    """An XRPL transaction hash: 64 upper-case hex digits, as rippled reports it."""

    value: str

    def __post_init__(self) -> None:
        _pattern_string(self.value, "XrplTransactionHash", _XRPL_HASH, "MALFORMED_TX_HASH")


@dataclass(frozen=True, slots=True, order=True)
class XrplLedgerObjectId:
    """An XRPL ledger object ID (for example a Loan): 64 upper-case hex digits."""

    value: str

    def __post_init__(self) -> None:
        _pattern_string(self.value, "XrplLedgerObjectId", _XRPL_HASH, "MALFORMED_LEDGER_OBJECT_ID")


@dataclass(frozen=True, slots=True, order=True)
class XrplAccount:
    """An XRPL classic address, checksum-validated. Format only; no ledger lookup."""

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise DomainError("NOT_A_STRING", "XrplAccount", type(self.value).__name__)
        if not self.value:
            raise DomainError("EMPTY_VALUE", "XrplAccount")
        if not is_valid_classic_address(self.value):
            raise DomainError("MALFORMED_ACCOUNT", "XrplAccount", self.value)


@dataclass(frozen=True, slots=True, order=True)
class Nonce:
    """A purchase-request nonce: 128 bits as 32 lower-case hex digits."""

    value: str

    def __post_init__(self) -> None:
        _pattern_string(self.value, "Nonce", _NONCE, "MALFORMED_NONCE")


@dataclass(frozen=True, slots=True, order=True)
class CountryCode:
    """An ISO 3166-1 alpha-2 code: two upper-case ASCII letters (shape only, not the assigned list)."""

    value: str

    def __post_init__(self) -> None:
        _pattern_string(self.value, "CountryCode", _COUNTRY, "MALFORMED_COUNTRY_CODE")


@dataclass(frozen=True, slots=True, order=True)
class Timestamp:
    """A UTC instant as RFC 3339 `YYYY-MM-DDTHH:MM:SSZ`: whole seconds, literal Z, no offsets.

    The single accepted spelling sorts lexically in time order.
    """

    value: str

    def __post_init__(self) -> None:
        _pattern_string(self.value, "Timestamp", _TIMESTAMP, "MALFORMED_TIMESTAMP")
        try:
            datetime.strptime(self.value, _TIMESTAMP_FORMAT)
        except ValueError:
            raise DomainError("MALFORMED_TIMESTAMP", "Timestamp", self.value) from None

    def to_datetime(self) -> datetime:
        return datetime.strptime(self.value, _TIMESTAMP_FORMAT).replace(tzinfo=UTC)

    def plus_seconds(self, seconds: int) -> Timestamp:
        """This instant moved by a whole (possibly negative) number of seconds, same spelling."""
        offset = strict_int(seconds, "seconds", minimum=-MAX_SAFE_INTEGER)
        moved = self.to_datetime() + timedelta(seconds=offset)
        return Timestamp(moved.strftime(_TIMESTAMP_FORMAT))


# --- application identifiers ---------------------------------------------------------------


@dataclass(frozen=True, slots=True, order=True)
class _Identifier:
    """ASCII letters, digits and `._:-`, 1 to 128 characters, starting with a letter or digit."""

    value: str

    def __post_init__(self) -> None:
        _pattern_string(self.value, type(self).__name__, _IDENTIFIER, "MALFORMED_IDENTIFIER")


@dataclass(frozen=True, slots=True, order=True)
class FacilityId(_Identifier):
    pass


@dataclass(frozen=True, slots=True, order=True)
class QuoteId(_Identifier):
    pass


@dataclass(frozen=True, slots=True, order=True)
class ProviderId(_Identifier):
    pass


@dataclass(frozen=True, slots=True, order=True)
class ReservationId(_Identifier):
    pass


@dataclass(frozen=True, slots=True, order=True)
class EngineVersion(_Identifier):
    pass


# --- closed vocabularies -------------------------------------------------------------------


class EvidenceTier(Enum):
    """Canonical evidence tiers (build plan, Evidence tiers). Serialized as the integer."""

    REQUESTED = 0
    WITNESSED = 1
    BOUND_AND_SIGNED = 2
    MEASURED = 3


class DeliveryMode(Enum):
    INTERACTIVE = "interactive"
    BATCH = "batch"
    RESERVED = "reserved"


class ServiceTier(Enum):
    FAST = "fast"
    BASE = "base"


class FacilityStatus(Enum):
    PENDING = "pending"
    ACTIVE = "active"
    FROZEN = "frozen"
    REPAID = "repaid"
    DEFAULTED = "defaulted"


class ReservationStatus(Enum):
    HELD = "held"
    SETTLED = "settled"
    RELEASED = "released"


class Purpose(Enum):
    INFERENCE = "inference"


class SeedPolicy(Enum):
    FIXED = "fixed"
    PROVIDER = "provider"
    NONE = "none"


class ResidencyRegion(Enum):
    EU = "EU"
    US = "US"


class DecisionOutcome(Enum):
    ACCEPT = "accept"
    REJECT = "reject"


class DecisionStage(Enum):
    POLICY = "policy"
    VERIFIER = "verifier"


class ReasonCode(Enum):
    """Policy engine and verifier reason codes.

    The build plan's codes, plus QUOTE_EXPIRED and BILLED_AMOUNT_MISMATCH added by the C2 amendment
    to the C1A contract. Declaration order is each engine's precedence order.
    """

    SIG_INVALID = "SIG_INVALID"
    KEY_REVOKED = "KEY_REVOKED"
    FACILITY_NOT_ACTIVE = "FACILITY_NOT_ACTIVE"
    FACILITY_EXPIRED = "FACILITY_EXPIRED"
    QUOTE_EXPIRED = "QUOTE_EXPIRED"
    PURPOSE_MISMATCH = "PURPOSE_MISMATCH"
    PROVIDER_NOT_ALLOWED = "PROVIDER_NOT_ALLOWED"
    PROFILE_NOT_ALLOWED = "PROFILE_NOT_ALLOWED"
    TIER_TOO_LOW = "TIER_TOO_LOW"
    PRICE_ABOVE_MAX = "PRICE_ABOVE_MAX"
    OVER_LIMIT = "OVER_LIMIT"
    NONCE_REUSED = "NONCE_REUSED"
    RECEIPT_SIG_INVALID = "RECEIPT_SIG_INVALID"
    BINDING_MISMATCH = "BINDING_MISMATCH"
    RESPONSE_DIGEST_MISMATCH = "RESPONSE_DIGEST_MISMATCH"
    TOKEN_COUNT_EXCEEDS_QUOTE = "TOKEN_COUNT_EXCEEDS_QUOTE"
    BILLED_AMOUNT_MISMATCH = "BILLED_AMOUNT_MISMATCH"
    CHAIN_BROKEN = "CHAIN_BROKEN"
    REPLAYED_RECEIPT = "REPLAYED_RECEIPT"


# Stage ownership: each engine may emit only its own codes, in this precedence order.
POLICY_REASON_CODES: tuple[ReasonCode, ...] = (
    ReasonCode.SIG_INVALID,
    ReasonCode.KEY_REVOKED,
    ReasonCode.FACILITY_NOT_ACTIVE,
    ReasonCode.FACILITY_EXPIRED,
    ReasonCode.QUOTE_EXPIRED,
    ReasonCode.PURPOSE_MISMATCH,
    ReasonCode.PROVIDER_NOT_ALLOWED,
    ReasonCode.PROFILE_NOT_ALLOWED,
    ReasonCode.TIER_TOO_LOW,
    ReasonCode.PRICE_ABOVE_MAX,
    ReasonCode.OVER_LIMIT,
    ReasonCode.NONCE_REUSED,
)
VERIFIER_REASON_CODES: tuple[ReasonCode, ...] = (
    ReasonCode.RECEIPT_SIG_INVALID,
    ReasonCode.BINDING_MISMATCH,
    ReasonCode.RESPONSE_DIGEST_MISMATCH,
    ReasonCode.TOKEN_COUNT_EXCEEDS_QUOTE,
    ReasonCode.BILLED_AMOUNT_MISMATCH,
    ReasonCode.CHAIN_BROKEN,
    ReasonCode.REPLAYED_RECEIPT,
)


def parse_enum[E: Enum](enum_type: type[E], value: object, field: str) -> E:
    """Return the member whose serialized value is exactly `value` (same type; bool is not int)."""
    for member in enum_type:
        if type(value) is type(member.value) and value == member.value:
            return member
    raise DomainError("INVALID_ENUM", field, repr(value))
