"""Agent-side client for the I2a marketplace: verify, sign and submit outside the browser and API.

The agent holds its own Ed25519 key. This module never sends, logs or stores it. For a purchase:

1. `prepare` sends the procurement and the prompt's SHA-256 (not the prompt) to the API.
2. `verify_preparation` independently re-derives and checks every field of the prepared request
   against what the agent intends (its facilities, providers, price ceiling, evidence floor, its
   own prompt digest and key, a short expiry), rebuilds the canonical request and its digest with
   the Kessai serializer (JCS (RFC 8785) + NFC), and recomputes the C3A statement it will sign.
   Any mismatch refuses before signing.
3. `credence.core.signatures.sign` signs the existing canonical request. No new signature format.
4. `submit` sends the prompt and the signature.

Retrieval and acknowledgment each send a fresh ResponseAuthorization (I1e) signed here.

Owner: Inference (ResearchBot / agent SDK). Written in the I2a integration; provisional: subject to
Inference and security review.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from nacl.signing import SigningKey

from credence.core.billing import max_total_cost_value
from credence.core.objects import ExecutionProfile, PurchaseRequest, Quote, ServiceClass
from credence.core.primitives import KeyId, Purpose, Timestamp
from credence.core.signatures import key_id_for, sign, signature_message
from credence.inference.payload import payload_digest
from credence.integration.authorization import ResponseAction, sign_response_authorization

JSON = dict[str, Any]
CLOCK_SKEW_SECONDS = 30
MAX_PREPARATION_LIFETIME_SECONDS = 300
AUTHORIZATION_LIFETIME_SECONDS = 120


class Http(Protocol):
    def __call__(
        self, method: str, path: str, body: Mapping[str, Any] | None, headers: Mapping[str, str]
    ) -> tuple[int, bytes]: ...


def urllib_http(base_url: str) -> Http:
    root = base_url.rstrip("/")

    def call(
        method: str, path: str, body: Mapping[str, Any] | None, headers: Mapping[str, str]
    ) -> tuple[int, bytes]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(root + path, data=data, method=method)
        for name, value in headers.items():
            request.add_header(name, value)
        if data is not None:
            request.add_header("content-type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
                return int(response.status), bytes(response.read())
        except urllib.error.HTTPError as exc:
            return int(exc.code), bytes(exc.read())

    return call


def utc_now() -> Timestamp:
    return Timestamp(datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))


class ApiRefused(RuntimeError):
    def __init__(self, status: int, code: str, detail: str) -> None:
        super().__init__(f"{status} {code}: {detail}")
        self.status = status
        self.code = code
        self.detail = detail


class VerificationFailed(ValueError):
    """The prepared request is not what the agent intends. Nothing was signed."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


@dataclass(frozen=True)
class Expectations:
    """What the agent is willing to sign for."""

    facility_ids: frozenset[str]
    allowed_providers: frozenset[str] | None = None
    max_total_cost_micro_trlusd: int | None = None
    minimum_evidence_tier: int = 0


def verify_preparation(
    prepared: Mapping[str, Any],
    *,
    key_id: KeyId,
    prompt: bytes,
    expect: Expectations,
    now: Timestamp,
) -> PurchaseRequest:
    """Check every field of a prepared purchase independently; return the request to sign."""

    def require(condition: bool, code: str, detail: str) -> None:
        if not condition:
            raise VerificationFailed(code, detail)

    try:
        request = PurchaseRequest.from_canonical(prepared["request"]["object"])
        quote = Quote.from_canonical(prepared["quote"]["object"])
        service = ServiceClass.from_canonical(prepared["service_class"]["object"])
        profile = ExecutionProfile.from_canonical(prepared["execution_profile"]["object"])
    except (KeyError, TypeError, ValueError) as exc:
        raise VerificationFailed("MALFORMED_PREPARATION", str(exc)) from None
    require(
        request.digest().value == prepared["request"]["digest"],
        "DIGEST_MISMATCH",
        "the request does not hash to the stated digest",
    )
    require(
        request.canonical_bytes().decode("utf-8") == prepared["request"]["canonical_json"],
        "CANONICAL_MISMATCH",
        "the stated canonical JSON is not the request's canonical form",
    )
    require(
        signature_message(request).hex() == prepared["signing"]["statement_hex"],
        "STATEMENT_MISMATCH",
        "the stated signing statement is not this request's C3A statement",
    )
    require(prepared["agent_key_id"] == key_id.value, "WRONG_AGENT", "prepared for another key")
    require(
        request.prompt_digest == payload_digest(prompt),
        "PROMPT_MISMATCH",
        "the request does not name this agent's prompt",
    )
    require(
        request.facility_id.value in expect.facility_ids,
        "FACILITY_NOT_EXPECTED",
        request.facility_id.value,
    )
    require(request.purpose is Purpose.INFERENCE, "PURPOSE_MISMATCH", request.purpose.value)
    require(
        expect.allowed_providers is None or request.provider_id.value in expect.allowed_providers,
        "PROVIDER_NOT_ALLOWED",
        request.provider_id.value,
    )
    require(
        quote.quote_id == request.quote_id and quote.provider_id == request.provider_id,
        "QUOTE_MISMATCH",
        "the quote is not the requested quote",
    )
    require(
        max_total_cost_value(quote) == request.max_total_cost_micro_trlusd.value,
        "PRICE_MISMATCH",
        "the request's maximum cost is not the quote's maximum",
    )
    require(
        expect.max_total_cost_micro_trlusd is None
        or request.max_total_cost_micro_trlusd.value <= expect.max_total_cost_micro_trlusd,
        "PRICE_ABOVE_AGENT_CEILING",
        str(request.max_total_cost_micro_trlusd.value),
    )
    require(
        quote.service_class_digest == service.digest()
        and service.execution_profile_digest == profile.digest(),
        "OFFER_MISMATCH",
        "quote, service class and execution profile do not refer to each other",
    )
    require(
        service.evidence_tier.value >= expect.minimum_evidence_tier,
        "TIER_BELOW_AGENT_FLOOR",
        str(service.evidence_tier.value),
    )
    require(now.value < request.expires_at.value, "EXPIRED", request.expires_at.value)
    latest = now.plus_seconds(MAX_PREPARATION_LIFETIME_SECONDS + CLOCK_SKEW_SECONDS)
    require(request.expires_at.value <= latest.value, "EXPIRY_TOO_LONG", request.expires_at.value)
    require(
        (prepared["provider_id"], prepared["nonce"], prepared["expires_at"])
        == (request.provider_id.value, request.nonce.value, request.expires_at.value),
        "SUMMARY_MISMATCH",
        "the displayed fields differ from the request",
    )
    return request


class AgentClient:
    def __init__(
        self,
        http: Http,
        token: str,
        signing_key: SigningKey,
        *,
        clock: Callable[[], Timestamp] = utc_now,
    ) -> None:
        self._http = http
        self._headers = {"authorization": f"Bearer {token}"}
        self.__key = signing_key
        self._clock = clock

    @property
    def key_id(self) -> KeyId:
        return key_id_for(self.__key.verify_key)

    def _call(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> bytes:
        status, data = self._http(method, path, body, self._headers)
        if not 200 <= status < 300:
            try:
                error = json.loads(data)
                raise ApiRefused(status, str(error.get("error")), str(error.get("detail")))
            except (json.JSONDecodeError, AttributeError):
                raise ApiRefused(status, "HTTP_ERROR", data.decode("utf-8", "replace")) from None
        return data

    def _json(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> JSON:
        result = json.loads(self._call(method, path, body))
        assert isinstance(result, dict)
        return result

    def session(self) -> JSON:
        return self._json("GET", "/v0/live/session")

    def prepare(
        self,
        procurement: Mapping[str, Any],
        prompt: bytes,
        *,
        facility_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> JSON:
        body = {
            **{k: v for k, v in procurement.items() if k not in _FORM_ONLY},
            "prompt_digest": payload_digest(prompt).value,
            "facility_id": facility_id,
            "idempotency_key": idempotency_key,
        }
        return self._json("POST", "/v0/live/purchases/prepare", body)

    def preparation(self, preparation_id: str) -> JSON:
        return self._json("GET", f"/v0/live/purchases/preparations/{preparation_id}")

    def sign_preparation(
        self, prepared: Mapping[str, Any], prompt: bytes, expect: Expectations
    ) -> JSON:
        """Verify, then sign the request's C3A statement. Returns the SignedObject JSON."""
        request = verify_preparation(
            prepared, key_id=self.key_id, prompt=prompt, expect=expect, now=self._clock()
        )
        return sign(request, self.__key).to_canonical()

    def submit(self, preparation_id: str, prompt: bytes, signature: Mapping[str, Any]) -> JSON:
        body = {
            "preparation_id": preparation_id,
            "prompt": prompt.decode("utf-8"),
            "signature": dict(signature),
        }
        return self._json("POST", "/v0/live/purchases/submit", body)

    def purchase(
        self,
        procurement: Mapping[str, Any],
        prompt: bytes,
        expect: Expectations,
        *,
        facility_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> JSON:
        prepared = self.prepare(
            procurement, prompt, facility_id=facility_id, idempotency_key=idempotency_key
        )
        signature = self.sign_preparation(prepared, prompt, expect)
        return self.submit(prepared["preparation_id"], prompt, signature)

    def authorization(self, operation_id: str, action: ResponseAction) -> JSON:
        """A fresh, single-use ResponseAuthorization for one action on one operation."""
        auth = sign_response_authorization(
            self.__key,
            operation_id,
            action,
            self._clock().plus_seconds(AUTHORIZATION_LIFETIME_SECONDS),
        )
        return {
            "operation_id": auth.operation_id,
            "action": auth.action.value,
            "agent_key_id": auth.agent_key_id.value,
            "nonce": auth.nonce,
            "expires_at": auth.expires_at.value,
            "signature": auth.signature.value,
        }

    def retrieve(self, operation_id: str) -> bytes:
        body = self.authorization(operation_id, ResponseAction.RETRIEVE)
        return self._call("POST", f"/v0/live/operations/{operation_id}/response", body)

    def acknowledge(self, operation_id: str) -> None:
        body = self.authorization(operation_id, ResponseAction.ACKNOWLEDGE)
        self._call("POST", f"/v0/live/operations/{operation_id}/response", body)


# Dry-run form members that are not part of a purchase preparation.
_FORM_ONLY = frozenset({"prompt", "facility_limit_micro_trlusd", "facility_drawn_micro_trlusd"})
