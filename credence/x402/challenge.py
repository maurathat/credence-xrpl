"""Version-aware, typed parser for HTTP 402 x402 challenges from XRPL merchants.

Supported wire protocol (X402-P0 research, contracts/X402-ADAPTER-CONTRACT.md, Sources):

- x402 version 2 only. t54's hosted facilitator fixes `x402Version` to 2 (its OpenAPI and
  `/supported`); no t54 source uses v1 (`X-PAYMENT`, `maxAmountRequired`). Any other version is an
  explicit `IncompatibleChallenge(UNSUPPORTED_VERSION)`, never a guess.
- Scheme `exact` only. t54 also advertises `upto` and `batch-settlement`; those are recognised
  and reported as UNSUPPORTED_SCHEME.
- XRPL networks as CAIP-2 `xrpl:{network_id}` (`xrpl:0` Mainnet, `xrpl:1` Testnet, `xrpl:2`
  Devnet). Entries for other chains are ignored and listed in `ignored`.
- The challenge may arrive in the `PAYMENT-REQUIRED` header (base64 of JSON), in the body, or both
  (both live merchants observed sent both, JSON-equal). A body challenge is recognized by its
  content, whatever the content type (application/json, absent, text/plain, +json types). If both
  are present they must be strictly equal JSON (same JSON types too); otherwise
  CONTRADICTORY_CHALLENGE. Ambiguous representations (a nested challenge, non-UTF-8 charset, a
  byte-order mark before JSON) are refused. Neither representation is ever preferred.

Each `accepts[]` entry is checked field by field: `amount` (integer drops for XRP, XRPL decimal
value string for an issued currency), `asset` ("XRP", a 3-character code or a 40-hex code),
`extra.issuer` (required for an issued currency, forbidden for XRP), `payTo` (classic address),
`maxTimeoutSeconds` (the payment validity window), `extra.invoiceId` (required: t54's scheme page
marks it required and its binding is the replay defence), optional `extra.sourceTag`,
`extra.destinationTag`, `extra.crossCurrency`, `extra.facilitator`. Every other `extra` key is kept
verbatim as merchant-advertised data and not interpreted. The exact entry is kept (`raw`) so a
later payment could echo it unchanged, as t54 merchants require.

What is advertised and what is observed are kept apart. `X402Challenge.observed` holds only facts
Credence saw itself (status, origin, request method and digest, where the challenge was carried,
header/body agreement, time). Everything in `requirements` and `resource` is the merchant's claim.

JSON is parsed strictly: duplicate keys, NaN/Infinity and non-object roots are refused.

Owner: x402 (X402-P0).
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from xrpl.core.addresscodec import is_valid_classic_address

from credence.core.kessai import JsonValue
from credence.core.primitives import Timestamp
from credence.x402.errors import X402Refusal, X402Refused
from credence.x402.lifecycle import SettlementClaim
from credence.x402.proposal import XrplAmount, digest_json
from credence.x402.transport import DUPLICATE_HEADER_MARKER, HttpRequest, HttpResponse, Origin

SUPPORTED_VERSIONS = (2,)
SUPPORTED_SCHEMES = ("exact",)
KNOWN_UNSUPPORTED_SCHEMES = ("upto", "batch-settlement")
XRPL_MAINNET = "xrpl:0"
XRPL_TESTNET = "xrpl:1"
XRPL_DEVNET = "xrpl:2"
MAX_VALIDITY_WINDOW_SECONDS = 3600
MAX_DROPS = 10**17  # total XRP supply in drops
MAX_IOU_SIGNIFICANT_DIGITS = 16
# XRPL issued amounts: 16-digit mantissa, exponent -96..80, so the leading digit's power of ten is
# within -81..95.
MIN_IOU_ADJUSTED_EXPONENT = -81
MAX_IOU_ADJUSTED_EXPONENT = 95

_XRPL_NETWORK = re.compile(r"xrpl:(0|[1-9][0-9]{0,9})")
_DROPS = re.compile(r"[1-9][0-9]{0,17}")
_DECIMAL = re.compile(r"(0|[1-9][0-9]*)(\.[0-9]+)?")
_HEX_CURRENCY = re.compile(r"[0-9A-F]{40}")
_ISO_CURRENCY = re.compile(r"[A-Za-z0-9]{3}")
_INVOICE_ID = re.compile(r"[\x21-\x7e]{1,128}")
_UINT32_MAX = 2**32 - 1


@dataclass(frozen=True)
class ObservedFacts:
    """What Credence itself observed. Not merchant claims."""

    origin: str
    request_method: str
    request_url: str
    request_digest: str
    http_status: int
    carried_in: str  # "header", "body" or "header+body"
    observed_at: Timestamp


@dataclass(frozen=True)
class XrplPaymentRequirement:
    """One merchant-advertised `exact` XRPL payment requirement, validated, unchanged in meaning."""

    scheme: str
    network: str
    network_id: int
    amount: XrplAmount
    pay_to: str
    max_timeout_seconds: int
    invoice_id: str
    source_tag: int | None
    destination_tag: int | None
    cross_currency: bool
    facilitator: tuple[str, str] | None
    advertised_extra: Mapping[str, Any]
    raw: Mapping[str, Any]

    @property
    def asset_label(self) -> str:
        """Display label decoded from the wire code (for example "RLUSD"), as the merchant names
        it. Display only; comparisons always use the exact wire code and issuer."""
        return currency_label(self.amount.currency)

    @property
    def requirement_digest(self) -> str:
        return digest_json(_json(self.raw))

    def expires_at(self, observed_at: Timestamp) -> Timestamp:
        return observed_at.plus_seconds(self.max_timeout_seconds)


@dataclass(frozen=True)
class X402Challenge:
    version: int
    resource_url: str
    resource_description: str | None
    resource_mime_type: str | None
    error: str | None
    extensions: Mapping[str, Any]
    requirements: tuple[XrplPaymentRequirement, ...]
    ignored: tuple[tuple[int, X402Refusal], ...]
    observed: ObservedFacts

    def requirement(self, index: int = 0) -> XrplPaymentRequirement:
        return self.requirements[index]


@dataclass(frozen=True)
class IncompatibleChallenge:
    """A well-formed 402 that this adapter does not speak. Reported, never guessed at."""

    code: X402Refusal
    detail: str
    observed: ObservedFacts


def currency_label(code: str) -> str:
    if code == "XRP" or len(code) == 3:
        return code
    raw = bytes.fromhex(code).rstrip(b"\x00")
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        return code
    return text if text.isprintable() and text else code


def _json(value: Any) -> JsonValue:
    return json.loads(json.dumps(value))  # type: ignore[no-any-return]


def _strict_json(raw: bytes, what: str) -> Any:
    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in pairs:
            if key in out:
                raise X402Refused(X402Refusal.CONTRADICTORY_CHALLENGE, f"duplicate key {key!r}")
            out[key] = value
        return out

    def no_constant(name: str) -> None:
        raise X402Refused(X402Refusal.MALFORMED_CHALLENGE, f"{what}: {name}")

    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=no_duplicates, parse_constant=no_constant
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise X402Refused(X402Refusal.MALFORMED_CHALLENGE, f"{what} is not JSON") from None
    return value


def _strict_object(raw: bytes, what: str) -> dict[str, Any]:
    value = _strict_json(raw, what)
    if not isinstance(value, dict):
        raise X402Refused(X402Refusal.MALFORMED_CHALLENGE, f"{what} is not an object")
    return value


def _decode_header(value: str) -> dict[str, Any]:
    if value == DUPLICATE_HEADER_MARKER:
        raise X402Refused(X402Refusal.CONTRADICTORY_CHALLENGE, "PAYMENT-REQUIRED repeated")
    try:
        raw = base64.b64decode(value.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise X402Refused(X402Refusal.MALFORMED_CHALLENGE, "header is not base64") from None
    return _strict_object(raw, "PAYMENT-REQUIRED header")


def request_digest(request: HttpRequest) -> str:
    """The original request's identity: method, exact URL, content type and body digest."""
    content_type = next((v for k, v in request.headers if k.lower() == "content-type"), None)
    return digest_json(
        {
            "method": request.method,
            "url": request.url,
            "content_type": content_type,
            "body_digest": request.body_digest,
        }
    )


def _int(value: object, field: str, code: X402Refusal, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise X402Refused(code, field)
    return value


def _amount(entry: Mapping[str, Any], extra: Mapping[str, Any]) -> XrplAmount:
    asset, amount = entry.get("asset"), entry.get("amount")
    if not isinstance(asset, str) or not isinstance(amount, str):
        raise X402Refused(X402Refusal.MALFORMED_ASSET, "asset and amount must be strings")
    issuer = extra.get("issuer")
    if asset == "XRP":
        if issuer is not None:
            raise X402Refused(X402Refusal.CONTRADICTORY_CHALLENGE, "issuer given for XRP")
        if not _DROPS.fullmatch(amount) or int(amount) > MAX_DROPS:
            raise X402Refused(X402Refusal.MALFORMED_AMOUNT, "XRP amount must be integer drops")
        return XrplAmount("XRP", None, amount)
    if not (_HEX_CURRENCY.fullmatch(asset) or _ISO_CURRENCY.fullmatch(asset)):
        raise X402Refused(X402Refusal.MALFORMED_ASSET, "currency code")
    if _HEX_CURRENCY.fullmatch(asset) and asset.startswith("00"):
        raise X402Refused(X402Refusal.MALFORMED_ASSET, "reserved currency code")
    if not isinstance(issuer, str) or not is_valid_classic_address(issuer):
        raise X402Refused(X402Refusal.MALFORMED_ASSET, "issued currency needs a valid issuer")
    if not _DECIMAL.fullmatch(amount):
        raise X402Refused(X402Refusal.MALFORMED_AMOUNT, "issued amount must be a decimal")
    value = Decimal(amount)
    digits = len(value.normalize().as_tuple().digits)
    if value <= 0 or digits > MAX_IOU_SIGNIFICANT_DIGITS:
        raise X402Refused(X402Refusal.MALFORMED_AMOUNT, "issued amount out of range")
    if not MIN_IOU_ADJUSTED_EXPONENT <= value.adjusted() <= MAX_IOU_ADJUSTED_EXPONENT:
        raise X402Refused(X402Refusal.MALFORMED_AMOUNT, "issued amount not representable")
    return XrplAmount(asset, issuer, amount)


def _alias_contradictions(entry: Mapping[str, Any], extra: Mapping[str, Any]) -> None:
    """t54's SDK also reads `currency`, a top-level `issuer` and `invoiceId` as aliases. Present
    and different from the canonical field is a contradiction; v1's `maxAmountRequired` inside a
    v2 entry is a version contradiction."""
    if "maxAmountRequired" in entry:
        raise X402Refused(X402Refusal.CONTRADICTORY_CHALLENGE, "v1 field in a v2 requirement")
    for alias, canonical in (
        ("currency", entry.get("asset")),
        ("issuer", extra.get("issuer")),
        ("invoiceId", extra.get("invoiceId")),
    ):
        if alias in entry and entry[alias] != canonical:
            raise X402Refused(X402Refusal.CONTRADICTORY_CHALLENGE, f"alias {alias} disagrees")


def _requirement(entry: Mapping[str, Any]) -> XrplPaymentRequirement:
    network = entry["network"]
    extra = entry.get("extra", {})
    if extra is None:
        extra = {}
    if not isinstance(extra, dict):
        raise X402Refused(X402Refusal.MALFORMED_CHALLENGE, "extra must be an object")
    _alias_contradictions(entry, extra)
    amount = _amount(entry, extra)
    pay_to = entry.get("payTo")
    if not isinstance(pay_to, str) or not is_valid_classic_address(pay_to):
        raise X402Refused(X402Refusal.MALFORMED_DESTINATION, "payTo must be a classic address")
    timeout = _int(
        entry.get("maxTimeoutSeconds"),
        "maxTimeoutSeconds",
        X402Refusal.MALFORMED_CHALLENGE,
        1,
        MAX_VALIDITY_WINDOW_SECONDS,
    )
    invoice_id = extra.get("invoiceId")
    if not isinstance(invoice_id, str) or not _INVOICE_ID.fullmatch(invoice_id):
        raise X402Refused(X402Refusal.MALFORMED_INVOICE_BINDING, "extra.invoiceId")
    source_tag = extra.get("sourceTag")
    if source_tag is not None:
        source_tag = _int(
            source_tag, "extra.sourceTag", X402Refusal.MALFORMED_CHALLENGE, 0, _UINT32_MAX
        )
    destination_tag = extra.get("destinationTag")
    if destination_tag is not None:
        destination_tag = _int(
            destination_tag, "extra.destinationTag", X402Refusal.MALFORMED_CHALLENGE, 0, _UINT32_MAX
        )
    cross_currency = extra.get("crossCurrency", False)
    if not isinstance(cross_currency, bool):
        raise X402Refused(X402Refusal.MALFORMED_CHALLENGE, "extra.crossCurrency")
    facilitator = extra.get("facilitator")
    fac: tuple[str, str] | None = None
    if facilitator is not None:
        if (
            not isinstance(facilitator, dict)
            or not isinstance(facilitator.get("id"), str)
            or not isinstance(facilitator.get("name"), str)
        ):
            raise X402Refused(X402Refusal.MALFORMED_CHALLENGE, "extra.facilitator")
        fac = (facilitator["id"], facilitator["name"])
    known = {"issuer", "invoiceId", "sourceTag", "destinationTag", "crossCurrency", "facilitator"}
    return XrplPaymentRequirement(
        scheme=entry["scheme"],
        network=network,
        network_id=int(network.removeprefix("xrpl:")),
        amount=amount,
        pay_to=pay_to,
        max_timeout_seconds=timeout,
        invoice_id=invoice_id,
        source_tag=source_tag,
        destination_tag=destination_tag,
        cross_currency=cross_currency,
        facilitator=fac,
        advertised_extra={k: v for k, v in extra.items() if k not in known},
        raw=entry,
    )


_CHALLENGE_KEYS = ("x402Version", "accepts")
_JSON_MEDIA_TYPES = frozenset({"application/json", "text/json", "text/plain", ""})
_UTF8_BOM = b"\xef\xbb\xbf"
_OTHER_BOMS = (b"\xff\xfe", b"\xfe\xff", b"\x00\x00\xfe\xff")
_MAX_NESTING_SCAN = 64


def strictly_equal(a: Any, b: Any) -> bool:
    """JSON equality with exact types: 1 is not 1.0 or true, key sets and list order must match.
    Python's `==` treats 1 == 1.0 == True, which would let two representations disagree."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(strictly_equal(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(strictly_equal(x, y) for x, y in zip(a, b, strict=True))
    return bool(a == b)


def _holds_challenge(value: Any, depth: int = 0) -> bool:
    """Whether a challenge-shaped object (x402Version or accepts) appears anywhere inside."""
    if depth > _MAX_NESTING_SCAN:
        return True  # too deep to inspect: treat as ambiguous
    if isinstance(value, dict):
        if any(key in value for key in _CHALLENGE_KEYS):
            return True
        return any(_holds_challenge(v, depth + 1) for v in value.values())
    if isinstance(value, list):
        return any(_holds_challenge(v, depth + 1) for v in value)
    return False


def _media_type(response: HttpResponse) -> tuple[str, str | None]:
    raw = response.header("content-type") or ""
    if raw == DUPLICATE_HEADER_MARKER:
        raise X402Refused(X402Refusal.CONTRADICTORY_CHALLENGE, "content-type repeated")
    media, _, params = raw.partition(";")
    charset = None
    for param in params.split(";"):
        key, _, value = param.strip().partition("=")
        if key.strip().lower() == "charset":
            charset = value.strip().strip('"').lower()
    return media.strip().lower(), charset


def _body_challenge(response: HttpResponse) -> dict[str, Any] | None:
    """A challenge carried in the body, recognized by its content whatever the content type.

    - Empty body, or a body that is not JSON (an HTML or text error page): no body challenge.
    - A JSON object without x402Version/accepts and no challenge-shaped object inside it: a
      non-challenge error body, ignored as before.
    - A top-level JSON object with x402Version or accepts: the body challenge.
    - Anything ambiguous is refused rather than ignored: a challenge-shaped object nested inside
      another value, a body in a non-UTF-8 charset or with a UTF-16/32 byte-order mark, a UTF-8 BOM
      before JSON, JSON that fails strict parsing when it is declared JSON or starts like JSON.
    """
    body = response.body
    if not body:
        return None
    media, charset = _media_type(response)
    if charset is not None and charset not in ("utf-8", "utf8"):
        raise X402Refused(X402Refusal.MALFORMED_CHALLENGE, f"body charset {charset}")
    if body.startswith(_OTHER_BOMS):
        raise X402Refused(X402Refusal.MALFORMED_CHALLENGE, "body is not UTF-8")
    declared_json = media in _JSON_MEDIA_TYPES - {"text/plain", ""} or media.endswith("+json")
    stripped = body.lstrip(b" \t\r\n")
    if stripped.startswith(_UTF8_BOM):
        if stripped[len(_UTF8_BOM) :].lstrip(b" \t\r\n")[:1] in (b"{", b"["):
            raise X402Refused(X402Refusal.MALFORMED_CHALLENGE, "byte-order mark before JSON")
        return None
    looks_json = stripped[:1] in (b"{", b"[")
    if not (declared_json or looks_json):
        return None  # a non-JSON error body (for example an HTML page)
    value = _strict_json(body, "body")
    if isinstance(value, dict) and any(key in value for key in _CHALLENGE_KEYS):
        return value
    if _holds_challenge(value):
        raise X402Refused(X402Refusal.MALFORMED_CHALLENGE, "challenge nested in the body")
    return None  # a JSON error body that is not a challenge


def _carried(response: HttpResponse) -> tuple[dict[str, Any], str]:
    """The one challenge the response carries. If both the header and the body carry one, they must
    be strictly equal (same keys, values and JSON types); otherwise CONTRADICTORY_CHALLENGE. Neither
    representation is ever preferred over the other."""
    if response.body_truncated:
        raise X402Refused(X402Refusal.MALFORMED_CHALLENGE, "body larger than the limit")
    header = response.header("payment-required")
    decoded = None if header is None else _decode_header(header)
    body = _body_challenge(response)
    if decoded is not None and body is not None:
        if not strictly_equal(decoded, body):
            raise X402Refused(X402Refusal.CONTRADICTORY_CHALLENGE, "header and body differ")
        return decoded, "header+body"
    if decoded is not None:
        return decoded, "header"
    if body is not None:
        return body, "body"
    raise X402Refused(X402Refusal.MALFORMED_CHALLENGE, "402 without a payment challenge")


def parse_challenge(
    request: HttpRequest, response: HttpResponse, observed_at: Timestamp
) -> X402Challenge | IncompatibleChallenge:
    """Parse the merchant's answer to one non-paying request. Raises X402Refused for anything
    malformed or contradictory; returns IncompatibleChallenge for well-formed terms this adapter
    does not support."""
    if 300 <= response.status < 400:
        raise X402Refused(X402Refusal.REDIRECT_REFUSED, f"status {response.status}")
    if response.status != 402:
        raise X402Refused(X402Refusal.NOT_A_PAYMENT_CHALLENGE, f"status {response.status}")
    document, carried_in = _carried(response)
    observed = ObservedFacts(
        origin=str(Origin.of(request.url)),
        request_method=request.method,
        request_url=request.url,
        request_digest=request_digest(request),
        http_status=response.status,
        carried_in=carried_in,
        observed_at=observed_at,
    )
    version = document.get("x402Version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise X402Refused(X402Refusal.MALFORMED_CHALLENGE, "x402Version")
    if version not in SUPPORTED_VERSIONS:
        return IncompatibleChallenge(X402Refusal.UNSUPPORTED_VERSION, str(version), observed)

    resource = document.get("resource")
    if not isinstance(resource, dict) or not isinstance(resource.get("url"), str):
        raise X402Refused(X402Refusal.MALFORMED_CHALLENGE, "resource.url")
    if resource["url"] != request.url:
        raise X402Refused(X402Refusal.RESOURCE_MISMATCH, "challenge names another resource")

    accepts = document.get("accepts")
    if not isinstance(accepts, list) or not accepts:
        raise X402Refused(X402Refusal.MALFORMED_CHALLENGE, "accepts must be a non-empty list")
    requirements: list[XrplPaymentRequirement] = []
    ignored: list[tuple[int, X402Refusal]] = []
    for index, entry in enumerate(accepts):
        if not isinstance(entry, dict):
            raise X402Refused(X402Refusal.MALFORMED_CHALLENGE, f"accepts[{index}]")
        scheme, network = entry.get("scheme"), entry.get("network")
        if not isinstance(scheme, str) or not isinstance(network, str):
            raise X402Refused(X402Refusal.MALFORMED_CHALLENGE, f"accepts[{index}] scheme/network")
        if not _XRPL_NETWORK.fullmatch(network):
            ignored.append((index, X402Refusal.UNSUPPORTED_NETWORK))
        elif scheme not in SUPPORTED_SCHEMES:
            ignored.append((index, X402Refusal.UNSUPPORTED_SCHEME))
        else:
            requirements.append(_requirement(entry))
    if not requirements:
        code = (
            X402Refusal.UNSUPPORTED_SCHEME
            if any(c is X402Refusal.UNSUPPORTED_SCHEME for _, c in ignored)
            else X402Refusal.NO_XRPL_REQUIREMENT
        )
        return IncompatibleChallenge(code, "no exact XRPL requirement", observed)
    _check_consistent(requirements)

    extensions = document.get("extensions", {})
    if extensions is None:
        extensions = {}
    if not isinstance(extensions, dict):
        raise X402Refused(X402Refusal.MALFORMED_CHALLENGE, "extensions")
    error = document.get("error")
    description, mime = resource.get("description"), resource.get("mimeType")
    return X402Challenge(
        version=version,
        resource_url=resource["url"],
        resource_description=description if isinstance(description, str) else None,
        resource_mime_type=mime if isinstance(mime, str) else None,
        error=error if isinstance(error, str) else None,
        extensions=extensions,
        requirements=tuple(requirements),
        ignored=tuple(ignored),
        observed=observed,
    )


def _check_consistent(requirements: list[XrplPaymentRequirement]) -> None:
    """Two requirements on the same network and asset with different terms leave the price or
    destination ambiguous; two requirements sharing one invoice ID must not differ at all."""
    seen: dict[tuple[str, str, str | None], XrplPaymentRequirement] = {}
    invoices: dict[str, XrplPaymentRequirement] = {}
    for req in requirements:
        key = (req.network, req.amount.currency, req.amount.issuer)
        prior = seen.get(key)
        if prior is not None and (prior.amount != req.amount or prior.pay_to != req.pay_to):
            raise X402Refused(X402Refusal.CONTRADICTORY_CHALLENGE, "ambiguous terms for one asset")
        seen[key] = req
        other = invoices.get(req.invoice_id)
        if other is not None and other.raw != req.raw:
            raise X402Refused(X402Refusal.CONTRADICTORY_CHALLENGE, "one invoice, two terms")
        invoices[req.invoice_id] = req


def check_live(
    requirement: XrplPaymentRequirement, observed: ObservedFacts, now: Timestamp
) -> Timestamp:
    """The requirement's expiry (observation time + validity window), refusing it if past."""
    expires_at = requirement.expires_at(observed.observed_at)
    if now < observed.observed_at or now > expires_at:
        raise X402Refused(X402Refusal.CHALLENGE_EXPIRED, expires_at.value)
    return expires_at


_TX_HASH = re.compile(r"[0-9A-F]{64}")


def parse_settlement_response(value: str) -> SettlementClaim:
    """Decode a `PAYMENT-RESPONSE` (v2) header: the merchant's or facilitator's claim, not evidence.

    Strict decode: `success` must be a JSON boolean; a transaction hash must be 64 upper-case
    hex digits; a network must be `xrpl:N`. Anything else is SETTLEMENT_RESPONSE_MISMATCH."""
    if value == DUPLICATE_HEADER_MARKER:
        raise X402Refused(X402Refusal.SETTLEMENT_RESPONSE_MISMATCH, "header repeated")
    try:
        raw = base64.b64decode(value.strip(), validate=True)
        doc = _strict_object(raw, "PAYMENT-RESPONSE")
    except (binascii.Error, ValueError, X402Refused):
        raise X402Refused(X402Refusal.SETTLEMENT_RESPONSE_MISMATCH, "not base64 JSON") from None
    success = doc.get("success")
    if not isinstance(success, bool):
        raise X402Refused(X402Refusal.SETTLEMENT_RESPONSE_MISMATCH, "success")
    tx, network = doc.get("transaction"), doc.get("network")
    if tx is not None and (not isinstance(tx, str) or not _TX_HASH.fullmatch(tx)):
        raise X402Refused(X402Refusal.SETTLEMENT_RESPONSE_MISMATCH, "transaction")
    if network is not None and (
        not isinstance(network, str) or not _XRPL_NETWORK.fullmatch(network)
    ):
        raise X402Refused(X402Refusal.SETTLEMENT_RESPONSE_MISMATCH, "network")
    payer, error_reason = doc.get("payer"), doc.get("errorReason")
    if payer is not None and not (isinstance(payer, str) and is_valid_classic_address(payer)):
        raise X402Refused(X402Refusal.SETTLEMENT_RESPONSE_MISMATCH, "payer")
    if error_reason is not None and not isinstance(error_reason, str):
        raise X402Refused(X402Refusal.SETTLEMENT_RESPONSE_MISMATCH, "errorReason")
    if success and tx is None:
        raise X402Refused(X402Refusal.SETTLEMENT_RESPONSE_MISMATCH, "success without transaction")
    return SettlementClaim(success, tx, network, payer, error_reason)
