"""Offline payment-before-delivery state model for one x402 purchase.

x402 inverts Credence's normal order. Core pays a provider after delivery and verification; an x402
merchant is paid before it serves. This module is an isolated orchestration model of that order. It
is not Core's settlement lifecycle, not the X1 payment journal, and it moves no money: every payment
fact it receives is an observation supplied by a caller (in X402-P0, a deterministic mock).

Happy path:

    QUOTED -> POLICY_ELIGIBLE -> PAYMENT_AUTHORIZATION_PENDING -> PAYMENT_PENDING
           -> PAYMENT_CONFIRMED -> DELIVERY_PENDING -> DELIVERED

Alternative outcomes: QUOTE_REFUSED, QUOTE_EXPIRED, PAYMENT_REJECTED, PAYMENT_STATUS_UNKNOWN,
VALIDATED_UNEXPECTED_DELIVERY, DELIVERY_FAILED, DELIVERY_EVIDENCE_INSUFFICIENT,
MANUAL_RECONCILIATION_REQUIRED.

Rules this model enforces:

- A payment timeout is PAYMENT_STATUS_UNKNOWN, never PAYMENT_REJECTED. Only validated ledger
  evidence of a failed transaction can reject a payment.
- A merchant's or facilitator's settlement response (x402 `PAYMENT-RESPONSE` / `X-PAYMENT-RESPONSE`)
  is the merchant's claim. It is recorded and cross-checked, but it never confirms a payment: only
  validated ledger evidence does (in the separately reviewed X1 path).
- Validated success whose delivered amount, asset, destination or network differs from the proposal
  is VALIDATED_UNEXPECTED_DELIVERY (the X1 term: the ledger delivered something other than what was
  authorized), not confirmation.
- PAYMENT_CONFIRMED never implies DELIVERED. Delivery needs its own observation, bound to the same
  proposal, request digest and payment transaction, with evidence at or above the required tier.
- Once a payment may exist (PAYMENT_PENDING onward), the record never returns to a state from which
  a new payment could be proposed. `may_propose_payment` is False for good, and a failed delivery
  keeps the payment and the missing-delivery facts and goes to manual reconciliation.
- Every observation names the agent and proposal it belongs to; one for another agent or proposal
  is refused (AGENT_MISMATCH / BINDING_ALTERED) without changing the record.

Owner: x402 (X402-P0).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from credence.core.primitives import EvidenceTier, Timestamp
from credence.x402.errors import X402Refusal, X402Refused
from credence.x402.proposal import PaymentAuthorizationProposal


class PurchaseState(Enum):
    QUOTED = "quoted"
    POLICY_ELIGIBLE = "policy_eligible"
    PAYMENT_AUTHORIZATION_PENDING = "payment_authorization_pending"
    PAYMENT_PENDING = "payment_pending"
    PAYMENT_CONFIRMED = "payment_confirmed"
    DELIVERY_PENDING = "delivery_pending"
    DELIVERED = "delivered"
    QUOTE_REFUSED = "quote_refused"
    QUOTE_EXPIRED = "quote_expired"
    PAYMENT_REJECTED = "payment_rejected"
    PAYMENT_STATUS_UNKNOWN = "payment_status_unknown"
    VALIDATED_UNEXPECTED_DELIVERY = "validated_unexpected_delivery"
    DELIVERY_FAILED = "delivery_failed"
    DELIVERY_EVIDENCE_INSUFFICIENT = "delivery_evidence_insufficient"
    MANUAL_RECONCILIATION_REQUIRED = "manual_reconciliation_required"


S = PurchaseState

# States in which a payment may exist on the ledger. None of them can lead to a new proposal.
PAYMENT_MAY_EXIST = frozenset(
    {
        S.PAYMENT_PENDING,
        S.PAYMENT_CONFIRMED,
        S.DELIVERY_PENDING,
        S.DELIVERED,
        S.PAYMENT_STATUS_UNKNOWN,
        S.VALIDATED_UNEXPECTED_DELIVERY,
        S.DELIVERY_FAILED,
        S.DELIVERY_EVIDENCE_INSUFFICIENT,
        S.MANUAL_RECONCILIATION_REQUIRED,
    }
)
TERMINAL = frozenset(
    {S.DELIVERED, S.QUOTE_REFUSED, S.QUOTE_EXPIRED, S.PAYMENT_REJECTED}
    | {S.MANUAL_RECONCILIATION_REQUIRED}
)


class LedgerOutcome(Enum):
    """What validated ledger evidence says about the proposed payment. TIMEOUT is not evidence."""

    VALIDATED_SUCCESS = "validated_success"
    VALIDATED_FAILURE = "validated_failure"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class PaymentObservation:
    """One payment fact, as the X1 result verifier would report it. In X402-P0 always a mock.

    For VALIDATED_SUCCESS, the delivered_* fields are what the validated ledger says was delivered.
    For VALIDATED_FAILURE, result_code is the validated engine result (for example tecPATH_DRY).
    For TIMEOUT, only observed_at is meaningful.
    """

    agent_id: str
    proposal_id: str
    outcome: LedgerOutcome
    observed_at: Timestamp
    transaction_hash: str | None = None
    delivered_amount: str | None = None
    delivered_currency: str | None = None
    delivered_issuer: str | None = None
    destination: str | None = None
    network: str | None = None
    result_code: str | None = None


@dataclass(frozen=True)
class SettlementClaim:
    """The merchant's or facilitator's settlement response header, decoded. A claim, not evidence."""

    success: bool
    transaction_hash: str | None
    network: str | None
    payer: str | None
    error_reason: str | None


@dataclass(frozen=True)
class DeliveryObservation:
    """One delivery fact. evidence_tier is the tier Credence actually verified for this response,
    not one the merchant claims."""

    agent_id: str
    proposal_id: str
    request_digest: str
    payment_transaction_hash: str
    response_digest: str | None
    http_status: int
    evidence_tier: EvidenceTier
    observed_at: Timestamp


@dataclass(frozen=True)
class Transition:
    from_state: PurchaseState
    to_state: PurchaseState
    event: str
    at: Timestamp
    detail: str = ""


@dataclass(frozen=True)
class PurchaseRecord:
    """The whole history of one x402 purchase. Immutable; every step returns a new record."""

    agent_id: str
    proposal: PaymentAuthorizationProposal | None
    state: PurchaseState
    history: tuple[Transition, ...]
    payment: PaymentObservation | None = None
    settlement_claim: SettlementClaim | None = None
    settlement_claim_mismatch: str | None = None
    delivery: DeliveryObservation | None = None
    refusal: str | None = None

    @property
    def may_propose_payment(self) -> bool:
        """Only before any payment could exist. Never true again once it is false."""
        return self.state not in PAYMENT_MAY_EXIST and self.state not in TERMINAL

    @property
    def payment_preserved(self) -> bool:
        return self.payment is not None


def _move(
    record: PurchaseRecord,
    allowed_from: frozenset[PurchaseState],
    to: PurchaseState,
    event: str,
    at: Timestamp,
    detail: str = "",
    **changes: object,
) -> PurchaseRecord:
    if record.state not in allowed_from:
        raise X402Refused(X402Refusal.ILLEGAL_TRANSITION, f"{event} from {record.state.value}")
    step = Transition(record.state, to, event, at, detail)
    return replace(record, state=to, history=(*record.history, step), **changes)  # type: ignore[arg-type]


def _bound(record: PurchaseRecord, agent_id: str, proposal_id: str) -> PaymentAuthorizationProposal:
    if agent_id != record.agent_id:
        raise X402Refused(X402Refusal.AGENT_MISMATCH, "observation is for another agent")
    proposal = record.proposal
    if proposal is None or proposal_id != proposal.proposal_id:
        raise X402Refused(X402Refusal.BINDING_ALTERED, "observation is for another proposal")
    return proposal


def quoted(agent_id: str, at: Timestamp) -> PurchaseRecord:
    return PurchaseRecord(
        agent_id=agent_id,
        proposal=None,
        state=S.QUOTED,
        history=(Transition(S.QUOTED, S.QUOTED, "quoted", at),),
    )


def policy_accepted(record: PurchaseRecord, at: Timestamp) -> PurchaseRecord:
    return _move(record, frozenset({S.QUOTED}), S.POLICY_ELIGIBLE, "policy_accepted", at)


def quote_refused(record: PurchaseRecord, reason: str, at: Timestamp) -> PurchaseRecord:
    """Policy or compatibility refused the quote, or authorization was refused before payment."""
    return _move(
        record,
        frozenset({S.QUOTED, S.POLICY_ELIGIBLE, S.PAYMENT_AUTHORIZATION_PENDING}),
        S.QUOTE_REFUSED,
        "quote_refused",
        at,
        reason,
        refusal=reason,
    )


def quote_expired(record: PurchaseRecord, at: Timestamp) -> PurchaseRecord:
    """Only before payment could exist. After PAYMENT_PENDING an expiry changes nothing: the
    payment's own evidence decides."""
    return _move(
        record,
        frozenset({S.QUOTED, S.POLICY_ELIGIBLE, S.PAYMENT_AUTHORIZATION_PENDING}),
        S.QUOTE_EXPIRED,
        "quote_expired",
        at,
    )


def authorization_requested(
    record: PurchaseRecord, proposal: PaymentAuthorizationProposal, at: Timestamp
) -> PurchaseRecord:
    if proposal.agent_id != record.agent_id:
        raise X402Refused(X402Refusal.AGENT_MISMATCH, "proposal is for another agent")
    if at > proposal.expires_at:
        raise X402Refused(X402Refusal.CHALLENGE_EXPIRED, "proposal expired")
    return _move(
        record,
        frozenset({S.POLICY_ELIGIBLE}),
        S.PAYMENT_AUTHORIZATION_PENDING,
        "authorization_requested",
        at,
        proposal=proposal,
    )


def payment_pending(record: PurchaseRecord, at: Timestamp) -> PurchaseRecord:
    """The separately authorized payment path reports that a payment may now exist. X402-P0 never
    submits; this transition is driven by tests only."""
    if record.proposal is None or at > record.proposal.expires_at:
        raise X402Refused(X402Refusal.CHALLENGE_EXPIRED, "no live proposal")
    return _move(
        record,
        frozenset({S.PAYMENT_AUTHORIZATION_PENDING}),
        S.PAYMENT_PENDING,
        "payment_pending",
        at,
    )


def _matches(proposal: PaymentAuthorizationProposal, obs: PaymentObservation) -> list[str]:
    expected = {
        "delivered_amount": proposal.amount.value,
        "delivered_currency": proposal.amount.currency,
        "delivered_issuer": proposal.amount.issuer,
        "destination": proposal.destination,
        "network": proposal.network,
    }
    return [name for name, value in expected.items() if getattr(obs, name) != value]


def payment_observed(record: PurchaseRecord, obs: PaymentObservation) -> PurchaseRecord:
    proposal = _bound(record, obs.agent_id, obs.proposal_id)
    waiting = frozenset({S.PAYMENT_PENDING, S.PAYMENT_STATUS_UNKNOWN})
    if obs.outcome is LedgerOutcome.TIMEOUT:
        return _move(
            record,
            waiting,
            S.PAYMENT_STATUS_UNKNOWN,
            "payment_timeout",
            obs.observed_at,
            "no validated evidence; not a failure",
        )
    if obs.transaction_hash is None:
        raise X402Refused(X402Refusal.PAYMENT_EVIDENCE_MISMATCH, "validated evidence without hash")
    if obs.outcome is LedgerOutcome.VALIDATED_FAILURE:
        if obs.result_code is None or obs.result_code == "tesSUCCESS":
            raise X402Refused(X402Refusal.PAYMENT_EVIDENCE_MISMATCH, "failure without result")
        return _move(
            record,
            waiting,
            S.PAYMENT_REJECTED,
            "payment_rejected",
            obs.observed_at,
            obs.result_code,
            payment=obs,
        )
    mismatched = _matches(proposal, obs)
    if mismatched:
        return _move(
            record,
            waiting,
            S.VALIDATED_UNEXPECTED_DELIVERY,
            "validated_unexpected_delivery",
            obs.observed_at,
            ",".join(mismatched),
            payment=obs,
        )
    return _move(
        record, waiting, S.PAYMENT_CONFIRMED, "payment_confirmed", obs.observed_at, payment=obs
    )


def settlement_claimed(
    record: PurchaseRecord, claim: SettlementClaim, at: Timestamp
) -> PurchaseRecord:
    """Record the merchant's settlement response without changing the state. A claim that
    contradicts the proposal or the validated payment is kept with the mismatch named, so the
    reconciliation view shows it; it never confirms or rejects a payment."""
    proposal = record.proposal
    if proposal is None or record.state not in PAYMENT_MAY_EXIST:
        raise X402Refused(X402Refusal.ILLEGAL_TRANSITION, "settlement claim before payment")
    problems: list[str] = []
    if not claim.success:
        problems.append("claims failure")
    if claim.network != proposal.network:
        problems.append("network")
    if claim.payer is not None and claim.payer != proposal.source_account:
        problems.append("payer")
    if claim.transaction_hash is None:
        problems.append("no transaction hash")
    elif record.payment is not None and claim.transaction_hash != record.payment.transaction_hash:
        problems.append("transaction hash")
    mismatch = ",".join(problems) or None
    step = Transition(record.state, record.state, "settlement_claimed", at, mismatch or "")
    return replace(
        record,
        settlement_claim=claim,
        settlement_claim_mismatch=mismatch,
        history=(*record.history, step),
    )


def delivery_started(record: PurchaseRecord, at: Timestamp) -> PurchaseRecord:
    return _move(
        record, frozenset({S.PAYMENT_CONFIRMED}), S.DELIVERY_PENDING, "delivery_started", at
    )


def delivery_observed(record: PurchaseRecord, obs: DeliveryObservation) -> PurchaseRecord:
    proposal = _bound(record, obs.agent_id, obs.proposal_id)
    pending = frozenset({S.DELIVERY_PENDING})
    payment = record.payment
    if record.state not in pending or payment is None:
        raise X402Refused(X402Refusal.ILLEGAL_TRANSITION, f"delivery in {record.state.value}")
    if obs.request_digest != proposal.request_digest:
        raise X402Refused(X402Refusal.DELIVERY_UNBOUND, "request digest")
    if obs.payment_transaction_hash != payment.transaction_hash:
        raise X402Refused(X402Refusal.DELIVERY_UNBOUND, "payment transaction")
    if obs.http_status != 200 or obs.response_digest is None:
        return _move(
            record,
            pending,
            S.DELIVERY_FAILED,
            "delivery_failed",
            obs.observed_at,
            f"http {obs.http_status}",
            delivery=obs,
        )
    if obs.evidence_tier.value < proposal.required_evidence_tier.value:
        return _move(
            record,
            pending,
            S.DELIVERY_EVIDENCE_INSUFFICIENT,
            "delivery_evidence_insufficient",
            obs.observed_at,
            f"tier {obs.evidence_tier.value} < {proposal.required_evidence_tier.value}",
            delivery=obs,
        )
    return _move(record, pending, S.DELIVERED, "delivered", obs.observed_at, delivery=obs)


def delivery_failed(record: PurchaseRecord, reason: str, at: Timestamp) -> PurchaseRecord:
    """No response arrived at all (timeout, connection reset). The payment record is kept."""
    return _move(
        record, frozenset({S.DELIVERY_PENDING}), S.DELIVERY_FAILED, "delivery_failed", at, reason
    )


def manual_reconciliation(record: PurchaseRecord, reason: str, at: Timestamp) -> PurchaseRecord:
    """Hand the purchase to an operator. Payment and delivery facts are kept unchanged."""
    return _move(
        record,
        frozenset(
            {
                S.PAYMENT_STATUS_UNKNOWN,
                S.VALIDATED_UNEXPECTED_DELIVERY,
                S.DELIVERY_FAILED,
                S.DELIVERY_EVIDENCE_INSUFFICIENT,
            }
        ),
        S.MANUAL_RECONCILIATION_REQUIRED,
        "manual_reconciliation",
        at,
        reason,
    )
