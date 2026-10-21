"""Plain-language text for codes and states that Core and Integration emit.

Display text only. Nothing here decides anything: every code and state shown with this text was
produced by Core or the Integration service. Kept in the API so every client shows the same words.

Owner: API/UI.
"""

from __future__ import annotations

from credence.core.primitives import EvidenceTier, ReasonCode
from credence.integration.journal import OperationState

REASON_TEXT: dict[ReasonCode, str] = {
    ReasonCode.SIG_INVALID: "The purchase request is not signed by a recognized agent key for "
    "this facility's borrower.",
    ReasonCode.KEY_REVOKED: "The agent key that signed the request has been revoked.",
    ReasonCode.FACILITY_NOT_ACTIVE: "The facility is not active.",
    ReasonCode.FACILITY_EXPIRED: "The facility has matured or the request has expired.",
    ReasonCode.QUOTE_EXPIRED: "The provider's quote is no longer valid.",
    ReasonCode.PURPOSE_MISMATCH: "The purchase is not for inference.",
    ReasonCode.PROVIDER_NOT_ALLOWED: "The facility does not allow this provider.",
    ReasonCode.PROFILE_NOT_ALLOWED: "The provider's service does not meet a structural "
    "requirement of any allowed procurement class.",
    ReasonCode.TIER_TOO_LOW: "The provider's evidence tier is below the required minimum.",
    ReasonCode.PRICE_ABOVE_MAX: "The quote's maximum cost is above the buyer's maximum "
    "acceptable price.",
    ReasonCode.OVER_LIMIT: "Drawn plus held plus this purchase's maximum cost would exceed the "
    "facility limit.",
    ReasonCode.NONCE_REUSED: "The request's nonce was already used on this facility.",
    ReasonCode.RECEIPT_SIG_INVALID: "The receipt's gateway signature, or a required provider "
    "signature, is missing or invalid.",
    ReasonCode.BINDING_MISMATCH: "The receipt does not bind to the approved request, profile or "
    "provider.",
    ReasonCode.RESPONSE_DIGEST_MISMATCH: "The delivered response does not match the receipt's "
    "response digest.",
    ReasonCode.TOKEN_COUNT_EXCEEDS_QUOTE: "Reported token counts exceed the quote's caps.",
    ReasonCode.BILLED_AMOUNT_MISMATCH: "The billed amount does not equal the quote's price for "
    "the reported tokens.",
    ReasonCode.CHAIN_BROKEN: "The receipt does not chain to the facility's last settled receipt.",
    ReasonCode.REPLAYED_RECEIPT: "The receipt was already settled.",
}

REQUIREMENT_TEXT: dict[str, str] = {
    "model_family": "model family differs from the requested model family",
    "precision": "precision is not asserted by the profile, or not in the allowed set",
    "quantization": "quantization is not asserted by the profile, or not in the allowed set",
    "delivery_mode": "delivery mode differs",
    "jurisdiction": "service jurisdiction is not asserted or not allowed",
    "data_residency": "data residency is not a subset of the allowed residencies",
    "provider": "provider is not in the procurement class's allowed providers",
}

# Canonical tier names (build plan, Evidence tiers), with the distinction the UI must make.
TIER_TEXT: dict[EvidenceTier, tuple[str, str]] = {
    EvidenceTier.REQUESTED: (
        "Requested",
        "Agent-declared: what the agent asked for, bound to a profile. Signed by the agent.",
    ),
    EvidenceTier.WITNESSED: (
        "Witnessed",
        "Gateway-witnessed: Credence's gateway captured the provider's response and declared "
        "model and signed the receipt. The provider signs nothing.",
    ),
    EvidenceTier.BOUND_AND_SIGNED: (
        "Bound and signed",
        "Provider-signed: the provider signed what it says it served (request, profile, "
        "response and token counts). Not proof that the exact weights ran.",
    ),
    EvidenceTier.MEASURED: (
        "Measured",
        "Measured execution: an attestation that the loaded weights match the profile. Roadmap "
        "only; not implemented.",
    ),
}

S = OperationState

STATE_TEXT: dict[OperationState, str] = {
    S.INTENT: "The chosen signed purchase is journaled. Core may not have reserved yet.",
    S.RESERVED: "Core holds the purchase's maximum cost against the facility.",
    S.EXECUTING: "The delivery attempt is about to begin or has begun.",
    S.DELIVERED: "Evidence and response bytes are durable; not yet accounting settled.",
    S.SETTLED: "Accounting settled: drawn includes the verified charge. The response is "
    "available to the authorized agent. No XRPL payment is implied.",
    S.RETRIEVED: "The agent has retrieved the response with a signed authorization.",
    S.ACKNOWLEDGED: "The agent acknowledged the response; its bytes were deleted.",
    S.NO_QUALIFYING_OFFER: "Core qualified no offer. Nothing was reserved; no provider was called.",
    S.POLICY_REJECTED: "Core's reservation step rejected the chosen purchase.",
    S.RELEASED_UNDELIVERED: "Released with no delivery attempt, with an audit record.",
    S.EXPIRED: "The hold lapsed before any delivery attempt.",
    S.ABANDONED: "Interrupted before Core reserved. Nothing is held.",
    S.UNKNOWN: "A delivery attempt began but no evidence was captured. A chargeable execution "
    "cannot be ruled out: never retried or released; the hold lapses at expiry.",
    S.DELIVERY_FAILED: "Core's verifier rejected authenticated evidence terminally; the hold "
    "was released and the evidence kept.",
    S.DISPUTED: "Settlement rejected or refused the evidence. The hold, the dispute and the "
    "evidence are kept.",
    S.LATE: "Evidence arrived at or after the hold's expiry. Kept as a late-delivery dispute; "
    "never settled.",
}

# The supported lifecycle, as the UI names its stages. `settled` is the journal state for the
# coordinator's `accounting_settled` outcome.
LIFECYCLE: tuple[tuple[OperationState, str], ...] = (
    (S.INTENT, "intent"),
    (S.RESERVED, "reserved"),
    (S.EXECUTING, "executing"),
    (S.DELIVERED, "delivered"),
    (S.SETTLED, "accounting_settled"),
    (S.RETRIEVED, "retrieved"),
    (S.ACKNOWLEDGED, "acknowledged"),
)

OUTCOME_CLASS: dict[OperationState, str] = {
    S.INTENT: "pending",
    S.RESERVED: "pending",
    S.EXECUTING: "pending",
    S.DELIVERED: "pending",
    S.SETTLED: "accounting_settled",
    S.RETRIEVED: "accounting_settled",
    S.ACKNOWLEDGED: "accounting_settled",
    S.NO_QUALIFYING_OFFER: "rejected",
    S.POLICY_REJECTED: "rejected",
    S.RELEASED_UNDELIVERED: "released",
    S.EXPIRED: "released",
    S.ABANDONED: "interrupted",
    S.UNKNOWN: "unknown",
    S.DELIVERY_FAILED: "disputed",
    S.DISPUTED: "disputed",
    S.LATE: "disputed",
}
