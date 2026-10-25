"""X402-P0: offline payment-before-delivery lifecycle, proposal replay and duplicate-invoice guards.

Covers brief tests 12-17 and 19. Every payment fact here is a mock observation; nothing is signed,
submitted or delivered.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from x402_world import (
    APPROVED,
    LATER,
    LOCAL_NETWORK,
    NOW,
    OTHER_ACCOUNT,
    make_pending,
    observe,
    proposal,
    request_auth,
    validated,
)

from credence.core.primitives import EvidenceTier, Timestamp
from credence.x402 import lifecycle as lc
from credence.x402.errors import X402Refusal, X402Refused
from credence.x402.lifecycle import (
    DeliveryObservation,
    LedgerOutcome,
    PaymentObservation,
    PurchaseRecord,
    PurchaseState,
    SettlementClaim,
)
from credence.x402.proposal import PaymentAuthorityBook

TX = "B" * 64
AGENT = "agent-researchbot"
OTHER_AGENT = "agent-other"


def pending(agent: str = AGENT) -> PurchaseRecord:
    p = proposal(agent)
    record = lc.policy_accepted(lc.quoted(agent, NOW), NOW)
    record = request_auth(record, p, NOW)
    return make_pending(record)


def success(record: PurchaseRecord, **changes: object) -> PaymentObservation:
    """Trusted validated evidence for the record's expected transaction (x402_world.validated)."""
    return validated(record, **changes)


def timeout(record: PurchaseRecord) -> PaymentObservation:
    assert record.proposal is not None
    return PaymentObservation(
        record.agent_id, record.proposal.proposal_id, LedgerOutcome.TIMEOUT, LATER
    )


def confirmed() -> PurchaseRecord:
    record = pending()
    return observe(record, success(record))


def delivery(record: PurchaseRecord, **changes: object) -> DeliveryObservation:
    assert record.proposal is not None
    obs = DeliveryObservation(
        agent_id=record.agent_id,
        proposal_id=record.proposal.proposal_id,
        request_digest=record.proposal.request_digest,
        payment_transaction_hash=TX,
        response_digest="sha256:" + "c" * 64,
        http_status=200,
        evidence_tier=EvidenceTier.WITNESSED,
        observed_at=LATER,
    )
    return replace(obs, **changes)  # type: ignore[arg-type]


def test_happy_path_reaches_delivered_only_through_every_state() -> None:
    record = lc.delivery_started(confirmed(), LATER)
    record = lc.delivery_observed(record, delivery(record))
    assert record.state is PurchaseState.DELIVERED
    assert [t.to_state for t in record.history] == [
        PurchaseState.QUOTED,
        PurchaseState.POLICY_ELIGIBLE,
        PurchaseState.PAYMENT_AUTHORIZATION_PENDING,
        PurchaseState.PAYMENT_PENDING,
        PurchaseState.PAYMENT_CONFIRMED,
        PurchaseState.DELIVERY_PENDING,
        PurchaseState.DELIVERED,
    ]


def test_proposal_is_never_submittable() -> None:
    p = proposal()
    assert p.submission_authorized is False
    assert APPROVED.submission_authorized is False
    tx = p.transaction()
    assert tx["SigningPubKey"] == "" and "TxnSignature" not in tx and "Signers" not in tx


# --- 12, 13: duplicate invoice and replayed proposal ---------------------------------------


def test_duplicate_invoice_is_refused_across_agents() -> None:
    book = PaymentAuthorityBook()
    book.admit(proposal(AGENT, label="job-1"))
    with pytest.raises(X402Refused) as exc:
        book.admit(proposal(OTHER_AGENT, label="job-2"))  # same invoice ID, other agent
    assert exc.value.code is X402Refusal.DUPLICATE_INVOICE


def test_replayed_proposal_is_refused_and_the_original_is_kept() -> None:
    book = PaymentAuthorityBook()
    p = proposal()
    book.admit(p)
    with pytest.raises(X402Refused) as exc:
        book.admit(proposal())
    assert exc.value.code is X402Refusal.PROPOSAL_REPLAYED
    assert book.get(p.idempotency_key) == p


def test_proposal_digest_is_deterministic_and_binding() -> None:
    assert proposal() == proposal()
    assert proposal().proposal_id != proposal(value="0.26").proposal_id
    assert (
        proposal().unsigned_transaction_digest != proposal(value="0.26").unsigned_transaction_digest
    )


def test_record_cannot_reenter_payment_after_payment_may_exist() -> None:
    record = pending()
    assert not record.may_propose_payment
    with pytest.raises(X402Refused) as exc:
        request_auth(record, proposal(), LATER)
    assert exc.value.code is X402Refusal.ILLEGAL_TRANSITION


def test_expired_proposal_cannot_be_requested_or_paid() -> None:
    record = lc.policy_accepted(lc.quoted(AGENT, NOW), NOW)
    with pytest.raises(X402Refused) as exc:
        request_auth(record, proposal(), Timestamp("2026-10-24T16:00:00Z"))
    assert exc.value.code is X402Refusal.CHALLENGE_EXPIRED


# --- 14: timeout and unknown outcome -------------------------------------------------------


def test_timeout_is_unknown_never_failed() -> None:
    record = pending()
    record = observe(record, timeout(record))
    assert record.state is PurchaseState.PAYMENT_STATUS_UNKNOWN
    record = observe(record, timeout(record))  # still unknown, still not failed
    assert record.state is PurchaseState.PAYMENT_STATUS_UNKNOWN
    assert record.payment is None


def test_unknown_resolves_only_by_validated_evidence() -> None:
    record = pending()
    record = observe(record, timeout(record))
    record = observe(record, success(record))
    assert record.state is PurchaseState.PAYMENT_CONFIRMED


def test_failure_needs_a_validated_result_code() -> None:
    record = pending()
    with pytest.raises(X402Refused):
        observe(record, success(record, outcome=LedgerOutcome.VALIDATED_FAILURE, result_code=None))
    rejected = observe(
        record,
        success(record, outcome=LedgerOutcome.VALIDATED_FAILURE, result_code="tecPATH_DRY"),
    )
    assert rejected.state is PurchaseState.PAYMENT_REJECTED


def test_unknown_can_go_to_manual_reconciliation_keeping_history() -> None:
    record = pending()
    record = observe(record, timeout(record))
    record = lc.manual_reconciliation(record, "operator", LATER)
    assert record.state is PurchaseState.MANUAL_RECONCILIATION_REQUIRED
    assert not record.may_propose_payment


def test_validated_success_with_wrong_amount_is_unexpected_delivery() -> None:
    record = pending()
    record = observe(record, success(record, delivered_amount="0.24"))
    assert record.state is PurchaseState.VALIDATED_UNEXPECTED_DELIVERY
    assert record.payment is not None and record.payment.transaction_hash == TX


@pytest.mark.parametrize(
    "change",
    [
        {"delivered_issuer": OTHER_ACCOUNT},
        {"delivered_currency": "USD"},
        {"destination": OTHER_ACCOUNT},
        {"network": "xrpl:0"},
    ],
)
def test_validated_success_to_anything_else_is_unexpected_delivery(change: dict[str, str]) -> None:
    record = pending()
    record = observe(record, success(record, **change))
    assert record.state is PurchaseState.VALIDATED_UNEXPECTED_DELIVERY


# --- 15: payment confirmed without delivery ------------------------------------------------


def test_payment_confirmation_is_not_delivery() -> None:
    record = confirmed()
    assert record.state is PurchaseState.PAYMENT_CONFIRMED
    assert record.delivery is None
    with pytest.raises(X402Refused) as exc:
        lc.delivery_observed(record, delivery(record))  # skipping DELIVERY_PENDING
    assert exc.value.code is X402Refusal.ILLEGAL_TRANSITION


def test_confirmed_payment_then_failed_delivery_keeps_payment_and_never_pays_again() -> None:
    record = lc.delivery_started(confirmed(), LATER)
    record = lc.delivery_failed(record, "connection reset", LATER)
    assert record.state is PurchaseState.DELIVERY_FAILED
    assert record.payment is not None and record.payment.transaction_hash == TX
    assert not record.may_propose_payment
    with pytest.raises(X402Refused):
        make_pending(record, LATER)
    record = lc.manual_reconciliation(record, "paid, not delivered", LATER)
    assert record.payment is not None and record.payment.transaction_hash == TX


def test_merchant_error_after_payment_is_failed_delivery() -> None:
    record = lc.delivery_started(confirmed(), LATER)
    record = lc.delivery_observed(record, delivery(record, http_status=402, response_digest=None))
    assert record.state is PurchaseState.DELIVERY_FAILED
    assert record.payment is not None


def test_delivery_before_payment_confirmation_is_refused() -> None:
    record = pending()
    with pytest.raises(X402Refused) as exc:
        lc.delivery_observed(record, delivery(record))
    assert exc.value.code is X402Refusal.ILLEGAL_TRANSITION


def test_expiry_after_payment_changes_nothing() -> None:
    with pytest.raises(X402Refused):
        lc.quote_expired(pending(), LATER)


# --- 16: delivery with inadequate evidence -------------------------------------------------


def test_delivery_below_required_tier_is_insufficient() -> None:
    p = proposal(tier=EvidenceTier.BOUND_AND_SIGNED)
    record = request_auth(lc.policy_accepted(lc.quoted(AGENT, NOW), NOW), p, NOW)
    record = make_pending(record)
    record = observe(record, success(record))
    record = lc.delivery_started(record, LATER)
    record = lc.delivery_observed(record, delivery(record))  # Tier 1 witnessed only
    assert record.state is PurchaseState.DELIVERY_EVIDENCE_INSUFFICIENT
    assert record.payment is not None


@pytest.mark.parametrize("field", ["request_digest", "payment_transaction_hash"])
def test_delivery_not_bound_to_this_purchase_is_refused(field: str) -> None:
    record = lc.delivery_started(confirmed(), LATER)
    other = "sha256:" + "d" * 64 if field == "request_digest" else "E" * 64
    with pytest.raises(X402Refused) as exc:
        lc.delivery_observed(record, delivery(record, **{field: other}))
    assert exc.value.code is X402Refusal.DELIVERY_UNBOUND
    assert record.state is PurchaseState.DELIVERY_PENDING


# --- 17: incorrect merchant settlement response --------------------------------------------


def test_merchant_success_claim_does_not_confirm_payment() -> None:
    record = pending()
    claim = SettlementClaim(True, TX, LOCAL_NETWORK, None, None)
    record = lc.settlement_claimed(record, claim, LATER)
    assert record.state is PurchaseState.PAYMENT_PENDING
    assert record.settlement_claim_mismatch is None


@pytest.mark.parametrize(
    ("claim", "expected"),
    [
        (SettlementClaim(True, "F" * 64, LOCAL_NETWORK, None, None), "transaction hash"),
        (SettlementClaim(True, TX, "xrpl:0", None, None), "network"),
        (SettlementClaim(True, TX, LOCAL_NETWORK, OTHER_ACCOUNT, None), "payer"),
        (SettlementClaim(False, None, LOCAL_NETWORK, None, "invalid"), "claims failure"),
    ],
)
def test_contradictory_settlement_claim_is_recorded_not_trusted(
    claim: SettlementClaim, expected: str
) -> None:
    record = confirmed()
    record = lc.settlement_claimed(record, claim, LATER)
    assert record.state is PurchaseState.PAYMENT_CONFIRMED  # neither confirms nor rejects
    assert record.settlement_claim_mismatch is not None
    assert expected in record.settlement_claim_mismatch


def test_merchant_failure_claim_does_not_reject_payment() -> None:
    record = pending()
    record = lc.settlement_claimed(record, SettlementClaim(False, None, None, None, "x"), LATER)
    assert record.state is PurchaseState.PAYMENT_PENDING


def test_settlement_claim_before_payment_is_refused() -> None:
    record = lc.policy_accepted(lc.quoted(AGENT, NOW), NOW)
    with pytest.raises(X402Refused):
        lc.settlement_claimed(record, SettlementClaim(True, TX, LOCAL_NETWORK, None, None), NOW)


# --- 19: cross-agent purchase confusion ----------------------------------------------------


def test_payment_observation_for_another_agent_is_refused() -> None:
    record = pending(AGENT)
    with pytest.raises(X402Refused) as exc:
        observe(record, success(record, agent_id=OTHER_AGENT))
    assert exc.value.code is X402Refusal.AGENT_MISMATCH
    assert record.state is PurchaseState.PAYMENT_PENDING


def test_observation_for_another_agents_proposal_is_refused() -> None:
    mine, theirs = pending(AGENT), pending(OTHER_AGENT)
    assert theirs.proposal is not None
    with pytest.raises(X402Refused) as exc:
        observe(mine, success(mine, proposal_id=theirs.proposal.proposal_id))
    assert exc.value.code is X402Refusal.BINDING_ALTERED


def test_delivery_for_another_agent_is_refused() -> None:
    record = lc.delivery_started(confirmed(), LATER)
    with pytest.raises(X402Refused) as exc:
        lc.delivery_observed(record, delivery(record, agent_id=OTHER_AGENT))
    assert exc.value.code is X402Refusal.AGENT_MISMATCH


def test_agent_cannot_attach_another_agents_proposal() -> None:
    record = lc.policy_accepted(lc.quoted(AGENT, NOW), NOW)
    with pytest.raises(X402Refused) as exc:
        request_auth(record, proposal(OTHER_AGENT), NOW)
    assert exc.value.code is X402Refusal.AGENT_MISMATCH
