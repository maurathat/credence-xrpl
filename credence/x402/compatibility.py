"""Compatibility of one observed x402 requirement with Credence's approved execution.

Three things are kept separate and recorded side by side:

1. the merchant's observed network and asset, exactly as advertised (RLUSD on XRPL Mainnet stays
   RLUSD on `xrpl:0`; it is never renamed to fit Credence's demo naming);
2. Credence's approved execution network and asset (the local synthetic tRLUSD test token on the
   configured local standalone network);
3. the decision and every reason for it.

`policy_evaluable` says Core's policy engine may be asked about the offer: the observed network and
asset are exactly the approved ones, and the network is not Mainnet (`ApprovedExecution` also
refuses every public network ID, so this holds even for a misconfigured approval). `executable` is always False under X402-P0: no payment network,
including Testnet, is authorized for submission. Mainnet challenges are displayable and assessable
read-only (`read_only`), never executable.

Facilitator facts come from a `FacilitatorProfile`: the kinds its `/supported` lists, and what is
known about multisigned payer transactions. Credence's agent account pays only with a 2-of-2
multisignature (ResearchBot + Credence policy signer, X1a), so a facilitator that does not accept a
multisigned `exact` blob is incompatible, and one where that is unknown is not assumed compatible.
For t54's hosted facilitators it is unknown: t54's buyer SDK only single-signs, every observed
settled payment is single-signed, and the FAQ's prose about SignerLists states no verifier rule.

Owner: x402 (X402-P0).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from credence.x402.challenge import XRPL_MAINNET, XrplPaymentRequirement, currency_label
from credence.x402.errors import X402Refusal, X402Refused
from credence.x402.procurement import ApprovedExecution


class MultisigSupport(Enum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class FacilitatorKind:
    x402_version: int
    scheme: str
    network: str


@dataclass(frozen=True)
class FacilitatorProfile:
    """What is known about one facilitator, and where each fact came from."""

    name: str
    base_url: str
    kinds: tuple[FacilitatorKind, ...]
    multisig: MultisigSupport
    multisig_basis: str

    def supports(self, version: int, scheme: str, network: str) -> bool:
        return FacilitatorKind(version, scheme, network) in self.kinds


def parse_supported(body: Any) -> tuple[FacilitatorKind, ...]:
    """Parse a facilitator `GET /supported` body (`{"kinds": [{x402Version, scheme, network}]}`)."""
    if not isinstance(body, dict) or not isinstance(body.get("kinds"), list):
        raise X402Refused(X402Refusal.MALFORMED_RESPONSE, "supported.kinds")
    kinds = []
    for kind in body["kinds"]:
        if not isinstance(kind, dict):
            raise X402Refused(X402Refusal.MALFORMED_RESPONSE, "supported.kinds[]")
        version, scheme, network = kind.get("x402Version"), kind.get("scheme"), kind.get("network")
        if isinstance(version, bool) or not isinstance(version, int):
            raise X402Refused(X402Refusal.MALFORMED_RESPONSE, "kind.x402Version")
        if not isinstance(scheme, str) or not isinstance(network, str):
            raise X402Refused(X402Refusal.MALFORMED_RESPONSE, "kind.scheme/network")
        kinds.append(FacilitatorKind(version, scheme, network))
    return tuple(kinds)


T54_MULTISIG_BASIS = (
    "UNVERIFIED: t54 buyer SDK x402-xrpl 0.3.4 single-signs only; observed settled Mainnet "
    "payments are single-signed; no published verifier rule for a Signers array"
)


def t54_profile(name: str, base_url: str, supported_body: Any) -> FacilitatorProfile:
    return FacilitatorProfile(
        name, base_url, parse_supported(supported_body), MultisigSupport.UNKNOWN, T54_MULTISIG_BASIS
    )


@dataclass(frozen=True)
class CompatibilityDecision:
    observed_network: str
    observed_currency: str
    observed_asset_label: str
    observed_issuer: str | None
    observed_amount: str
    approved_network: str
    approved_currency: str
    approved_asset_label: str
    approved_issuer: str
    policy_evaluable: bool
    read_only: bool
    reasons: tuple[X402Refusal, ...]

    @property
    def executable(self) -> bool:
        """Never under X402-P0."""
        return False


def assess(
    requirement: XrplPaymentRequirement,
    approved: ApprovedExecution,
    facilitator: FacilitatorProfile,
    *,
    version: int = 2,
) -> CompatibilityDecision:
    reasons: list[X402Refusal] = []
    network_ok = requirement.network == approved.network
    if requirement.network == XRPL_MAINNET:
        reasons.append(X402Refusal.MAINNET_READ_ONLY)
    elif not network_ok:
        reasons.append(X402Refusal.NETWORK_NOT_APPROVED)
    currency_ok = requirement.amount.currency == approved.currency
    issuer_ok = requirement.amount.issuer == approved.issuer
    if not currency_ok:
        reasons.append(X402Refusal.ASSET_NOT_APPROVED)
    elif not issuer_ok:
        reasons.append(X402Refusal.ISSUER_NOT_APPROVED)
    if requirement.cross_currency:
        reasons.append(X402Refusal.CROSS_CURRENCY_UNSUPPORTED)
    if not facilitator.supports(version, requirement.scheme, requirement.network):
        reasons.append(X402Refusal.FACILITATOR_KIND_UNSUPPORTED)
    if facilitator.multisig is MultisigSupport.UNSUPPORTED:
        reasons.append(X402Refusal.MULTISIGNATURE_UNSUPPORTED)
    elif facilitator.multisig is MultisigSupport.UNKNOWN:
        reasons.append(X402Refusal.MULTISIGNATURE_UNKNOWN)
    reasons.append(X402Refusal.INVOICE_BINDING_NOT_IN_X1_TEMPLATE)
    reasons.append(X402Refusal.SUBMISSION_NOT_AUTHORIZED)
    return CompatibilityDecision(
        observed_network=requirement.network,
        observed_currency=requirement.amount.currency,
        observed_asset_label=currency_label(requirement.amount.currency),
        observed_issuer=requirement.amount.issuer,
        observed_amount=requirement.amount.value,
        approved_network=approved.network,
        approved_currency=approved.currency,
        approved_asset_label=approved.label,
        approved_issuer=approved.issuer,
        policy_evaluable=network_ok
        and requirement.network != XRPL_MAINNET
        and currency_ok
        and issuer_ok
        and not requirement.cross_currency,
        read_only=requirement.network == XRPL_MAINNET,
        reasons=tuple(reasons),
    )
