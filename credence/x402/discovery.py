"""Optional x402 provider discovery: directory listings in, verified candidates out.

A directory listing (for example xrpl-ai.org/directory) is *advertised metadata*. Listing a service
there is not evidence that it works, that it serves inference, or that its price is current.
Discovery therefore has two stages, recorded separately:

1. `parse_directory`: read the configured directory document into `DirectoryListing`s exactly as
   advertised. `claims_inference` is the directory's own category claim ("Inference & Agents"), not
   a Credence judgement; that category also holds data and media tools.
2. `probe`: for an operator-configured `CandidateEndpoint` (exact URL, method and a non-sensitive
   probe body) on an allow-listed origin, send one non-paying request and parse the answer. The
   result is a `ServiceCandidate` whose `status` says what was actually observed.

Only CHALLENGE_COMPATIBLE candidates are `selectable`. Every other listing stays visible with its
status (UNVERIFIED, NOT_ALLOWED, UNREACHABLE, NOT_X402, CHALLENGE_INVALID, CHALLENGE_INCOMPATIBLE,
ENDPOINT_CHANGED). No status here means "delivery works": X402-P0 never pays, so no x402 merchant
delivery is ever observed. Nebius and local Qwen are not discovered here and are unaffected.

A re-probe of a selected candidate must reproduce its pinned URL, origin, method, network and
payTo; any change is ENDPOINT_CHANGED and the candidate stops being selectable.

Owner: x402 (X402-P0).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any

from credence.core.primitives import Timestamp
from credence.x402.challenge import (
    IncompatibleChallenge,
    X402Challenge,
    XrplPaymentRequirement,
    parse_challenge,
)
from credence.x402.compatibility import CompatibilityDecision, FacilitatorProfile, assess
from credence.x402.errors import X402Refusal, X402Refused
from credence.x402.procurement import ApprovedExecution
from credence.x402.transport import GuardedClient, HttpRequest, Origin

INFERENCE_CATEGORY = "Inference & Agents"


@dataclass(frozen=True)
class DirectoryListing:
    """One directory entry, advertised. Nothing here has been checked by Credence."""

    source: str
    listing_id: str
    merchant_name: str | None
    merchant_address: str | None
    category: str | None
    name: str | None
    description: str | None
    url: str
    price_amount: str | None
    price_asset: str | None
    price_asset_issuer: str | None
    network: str | None

    @property
    def claims_inference(self) -> bool:
        return self.category == INFERENCE_CATEGORY


def _opt_str(entry: Mapping[str, Any], key: str) -> str | None:
    value = entry.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise X402Refused(X402Refusal.MALFORMED_DIRECTORY, key)
    return value


def parse_directory(document: Any, source: str) -> tuple[DirectoryListing, ...]:
    """xrpl-ai.org's embedded services array: id, merchantAddr, merchant.name, category, name,
    description, url, priceAmount, priceAsset, priceAssetIssuer, network. It lists no HTTP method
    and no model, so neither is invented here."""
    if not isinstance(document, dict) or not isinstance(document.get("services"), list):
        raise X402Refused(X402Refusal.MALFORMED_DIRECTORY, "services")
    listings = []
    for entry in document["services"]:
        if not isinstance(entry, dict):
            raise X402Refused(X402Refusal.MALFORMED_DIRECTORY, "services[]")
        url = _opt_str(entry, "url")
        listing_id = _opt_str(entry, "id")
        if url is None or listing_id is None:
            raise X402Refused(X402Refusal.MALFORMED_DIRECTORY, "id/url")
        merchant = entry.get("merchant")
        merchant_name = merchant.get("name") if isinstance(merchant, dict) else None
        listings.append(
            DirectoryListing(
                source=source,
                listing_id=listing_id,
                merchant_name=merchant_name if isinstance(merchant_name, str) else None,
                merchant_address=_opt_str(entry, "merchantAddr"),
                category=_opt_str(entry, "category"),
                name=_opt_str(entry, "name"),
                description=_opt_str(entry, "description"),
                url=url,
                price_amount=_opt_str(entry, "priceAmount") or None,
                price_asset=_opt_str(entry, "priceAsset"),
                price_asset_issuer=_opt_str(entry, "priceAssetIssuer"),
                network=_opt_str(entry, "network"),
            )
        )
    return tuple(listings)


class VerificationStatus(Enum):
    UNVERIFIED = "unverified"  # advertised only; not probed
    NOT_ALLOWED = "not_allowed"  # origin not on the operator allow-list; not contacted
    UNREACHABLE = "unreachable"
    NOT_X402 = "not_x402"  # answered without a 402 (for example 404, 200, a redirect)
    CHALLENGE_INVALID = "challenge_invalid"  # a 402 this adapter refuses as malformed
    CHALLENGE_INCOMPATIBLE = "challenge_incompatible"  # well formed, not executable by Credence
    CHALLENGE_COMPATIBLE = "challenge_compatible"  # policy-evaluable; still never executable in P0
    ENDPOINT_CHANGED = "endpoint_changed"


@dataclass(frozen=True)
class CandidateEndpoint:
    """Operator configuration for one probe. The probe body must not contain anything sensitive;
    it is a discovery request, not a user prompt."""

    url: str
    method: str
    probe_body: bytes = b""
    content_type: str | None = None

    def request(self) -> HttpRequest:
        headers = (("content-type", self.content_type),) if self.content_type else ()
        return HttpRequest(self.method, self.url, headers, self.probe_body)


@dataclass(frozen=True)
class PinnedEndpoint:
    """What a selected candidate was observed to be; a later probe must match it exactly."""

    url: str
    origin: str
    method: str
    network: str
    pay_to: str


@dataclass(frozen=True)
class ServiceCandidate:
    endpoint: CandidateEndpoint
    listing: DirectoryListing | None
    status: VerificationStatus
    observed_at: Timestamp | None
    http_status: int | None = None
    challenge: X402Challenge | None = None
    incompatible: IncompatibleChallenge | None = None
    compatibility: tuple[CompatibilityDecision, ...] = ()
    refusal: X402Refusal | None = None
    refusal_detail: str = ""

    @property
    def selectable(self) -> bool:
        return self.status is VerificationStatus.CHALLENGE_COMPATIBLE

    @property
    def advertised_model(self) -> str | None:
        """The model the merchant's challenge names (for example Heurist's
        `extra.execution.model`), if any. Merchant-advertised."""
        if self.challenge is None:
            return None
        for req in self.challenge.requirements:
            execution = req.advertised_extra.get("execution")
            if isinstance(execution, dict) and isinstance(execution.get("model"), str):
                return str(execution["model"])
        return None

    def compatible_requirement(self) -> XrplPaymentRequirement:
        """For display and pinning only. It reads the probe's stored decisions; anything that leads
        to a proposal (`prepare.offer_from`, `prepare.prepare_proposal`) recomputes compatibility."""
        if self.challenge is None or not self.selectable:
            raise X402Refused(X402Refusal.SERVICE_UNVERIFIED, self.status.value)
        for req, decision in zip(self.challenge.requirements, self.compatibility, strict=True):
            if decision.policy_evaluable:
                return req
        raise X402Refused(X402Refusal.SERVICE_UNVERIFIED, "no compatible requirement")

    def pin(self) -> PinnedEndpoint:
        req = self.compatible_requirement()
        assert self.challenge is not None
        return PinnedEndpoint(
            url=self.challenge.resource_url,
            origin=self.challenge.observed.origin,
            method=self.challenge.observed.request_method,
            network=req.network,
            pay_to=req.pay_to,
        )


def unprobed(
    listings: Sequence[DirectoryListing],
) -> tuple[ServiceCandidate, ...]:
    """Every listing as an UNVERIFIED candidate (GET assumed only as a placeholder; the directory
    lists no method). Nothing is contacted."""
    return tuple(
        ServiceCandidate(
            CandidateEndpoint(item.url, "GET"), item, VerificationStatus.UNVERIFIED, None
        )
        for item in listings
    )


def probe(
    endpoint: CandidateEndpoint,
    client: GuardedClient,
    *,
    observed_at: Timestamp,
    approved: ApprovedExecution,
    facilitator: FacilitatorProfile,
    listing: DirectoryListing | None = None,
) -> ServiceCandidate:
    """One non-paying request to one configured endpoint, and what it showed."""
    base = ServiceCandidate(endpoint, listing, VerificationStatus.UNVERIFIED, observed_at)
    request = endpoint.request()
    try:
        _, response = client.send(request)
    except X402Refused as refused:
        status = (
            VerificationStatus.NOT_ALLOWED
            if refused.code is X402Refusal.ORIGIN_NOT_ALLOWED
            else VerificationStatus.UNREACHABLE
        )
        return replace(base, status=status, refusal=refused.code, refusal_detail=refused.detail)
    try:
        parsed = parse_challenge(request, response, observed_at)
    except X402Refused as refused:
        status = (
            VerificationStatus.NOT_X402
            if refused.code in (X402Refusal.NOT_A_PAYMENT_CHALLENGE, X402Refusal.REDIRECT_REFUSED)
            else VerificationStatus.CHALLENGE_INVALID
        )
        return replace(
            base,
            status=status,
            http_status=response.status,
            refusal=refused.code,
            refusal_detail=refused.detail,
        )
    if isinstance(parsed, IncompatibleChallenge):
        return replace(
            base,
            status=VerificationStatus.CHALLENGE_INCOMPATIBLE,
            http_status=response.status,
            incompatible=parsed,
            refusal=parsed.code,
            refusal_detail=parsed.detail,
        )
    decisions = tuple(
        assess(req, approved, facilitator, version=parsed.version) for req in parsed.requirements
    )
    compatible = any(d.policy_evaluable for d in decisions)
    return replace(
        base,
        status=(
            VerificationStatus.CHALLENGE_COMPATIBLE
            if compatible
            else VerificationStatus.CHALLENGE_INCOMPATIBLE
        ),
        http_status=response.status,
        challenge=parsed,
        compatibility=decisions,
    )


def reprobe(
    pinned: PinnedEndpoint,
    endpoint: CandidateEndpoint,
    client: GuardedClient,
    *,
    observed_at: Timestamp,
    approved: ApprovedExecution,
    facilitator: FacilitatorProfile,
) -> ServiceCandidate:
    """Probe again and require the pinned identity. A changed URL, origin, method, network or
    payTo demotes the candidate to ENDPOINT_CHANGED."""
    if endpoint.url != pinned.url or endpoint.method != pinned.method:
        return ServiceCandidate(
            endpoint,
            None,
            VerificationStatus.ENDPOINT_CHANGED,
            observed_at,
            refusal=X402Refusal.ENDPOINT_CHANGED,
            refusal_detail="configured endpoint differs from the pinned one",
        )
    fresh = probe(
        endpoint, client, observed_at=observed_at, approved=approved, facilitator=facilitator
    )
    if not fresh.selectable:
        return fresh
    now_pinned = fresh.pin()
    if now_pinned != pinned or str(Origin.of(pinned.url)) != pinned.origin:
        changed = [
            name
            for name in ("url", "origin", "method", "network", "pay_to")
            if getattr(now_pinned, name) != getattr(pinned, name)
        ]
        return replace(
            fresh,
            status=VerificationStatus.ENDPOINT_CHANGED,
            refusal=X402Refusal.ENDPOINT_CHANGED,
            refusal_detail=",".join(changed),
        )
    return fresh
