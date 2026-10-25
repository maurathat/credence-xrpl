"""X402-P0 end to end, offline: probe -> offer -> Core decision -> unsigned proposal -> lifecycle.

Covers brief tests 6-10, 12, 13 and 19 at the preparation boundary. A mock merchant on the
configured local standalone network asks for the local synthetic tRLUSD; Core's real policy engine
decides beside the Nebius and local Qwen candidates. Nothing is signed, submitted or paid.
"""

from __future__ import annotations

import hashlib
import itertools
import socket
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from inference_world import AGENT_KEY, Scenario, make_purchase, prompt
from market_world import GENERAL, GENERAL_FACILITY, MODEL_FAMILY, candidates, market_world
from x402_world import (
    APPROVED,
    INVOICE,
    LEDGER,
    LOCAL_FACILITATOR,
    MERCHANT_URL,
    NOW,
    PROBE_BODY,
    account,
    challenge_doc,
    client,
    endpoint,
    intent,
    probe_request,
    requirement_doc,
    route,
)

from credence.core.objects import Facility, ProcurementClass
from credence.core.policy_engine import PolicyDecision
from credence.core.primitives import (
    CountryCode,
    EvidenceTier,
    MicroTrlusd,
    ProviderId,
    QuoteId,
    Timestamp,
)
from credence.core.reservation_ledger import ReservationLedger
from credence.core.signatures import sign
from credence.inference.market import qualify_and_select
from credence.x402 import lifecycle as lc
from credence.x402.challenge import X402Challenge
from credence.x402.discovery import PinnedEndpoint, ServiceCandidate, probe
from credence.x402.errors import X402Refusal, X402Refused
from credence.x402.prepare import offer_from, prepare_proposal
from credence.x402.procurement import OperatorAssertions, project
from credence.x402.proposal import (
    PaymentAuthorizationProposal,
    ProposalBook,
    unsigned_digest,
    verify_binding,
)
from credence.x402.transport import HttpRequest

MERCHANT = ProviderId("x402-mock-merchant")
_LEDGERS = itertools.count()  # one fresh reservation ledger per flow
US = CountryCode("US")


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield


def widened() -> tuple[ProcurementClass, Facility]:
    providers = tuple(sorted((*GENERAL.allowed_providers, MERCHANT)))
    pc = replace(GENERAL, allowed_providers=providers)
    fac = replace(
        GENERAL_FACILITY, allowed_providers=providers, allowed_procurement_classes=(pc.digest(),)
    )
    return pc, fac


@dataclass
class Flow:
    candidate: ServiceCandidate
    challenge: X402Challenge
    pinned: PinnedEndpoint
    decision: PolicyDecision
    winner: ProviderId


def flow(tmp_path: Path, doc: dict[str, object] | None = None, cap: int | None = None) -> Flow:
    guarded, _ = client({("POST", MERCHANT_URL): route(doc or challenge_doc())})
    candidate = probe(
        endpoint(), guarded, observed_at=NOW, approved=APPROVED, facilitator=LOCAL_FACILITATOR
    )
    assert candidate.selectable and candidate.challenge is not None
    offer = offer_from(
        candidate,
        merchant_id=MERCHANT,
        model_family=MODEL_FAMILY,
        assertions=OperatorAssertions(US, (US,), 8192),
        now=NOW,
    )
    projection = project(offer, APPROVED, QuoteId("x402-quote-1"))
    pc, fac = widened()
    x402 = make_purchase(
        Scenario(fac, projection.quote, projection.service_class, projection.execution_profile, pc),
        99,
        request_prompt=prompt(1),
    )
    if cap is not None:
        capped = replace(x402.request, max_total_cost_micro_trlusd=MicroTrlusd(cap))
        x402 = replace(x402, request=capped, request_signature=sign(capped, AGENT_KEY))
    others = [
        make_purchase(
            Scenario(fac, c.quote, c.service_class, c.execution_profile, pc),
            10 + i,
            request_prompt=prompt(1),
        )
        for i, c in enumerate(candidates(fac, pc))
    ]
    selection = qualify_and_select(
        [*others, x402],
        registry=market_world().registry(),
        ledger=ReservationLedger(tmp_path / f"reservations-{next(_LEDGERS)}.sqlite3"),
        now=NOW,
    )
    assert selection.chosen is not None
    return Flow(
        candidate,
        candidate.challenge,
        candidate.pin(),
        selection.qualifications[-1].decision,
        selection.chosen.purchase.request.provider_id,
    )


def propose(
    f: Flow, *, agent: str = "agent-researchbot", label: str = "job-1", **changes: object
) -> PaymentAuthorizationProposal:
    kwargs: dict[str, object] = {
        "challenge": f.challenge,
        "requirement": f.challenge.requirement(),
        "decision": f.candidate.compatibility[0],
        "request": probe_request(),
        "pinned": f.pinned,
        "policy": f.decision,
        "intent": intent(agent, label),
        "merchant_id": MERCHANT.value,
        "ledger": LEDGER,
        "required_evidence_tier": EvidenceTier.WITNESSED,
        "now": NOW,
    }
    kwargs.update(changes)
    return prepare_proposal(**kwargs)  # type: ignore[arg-type]


def refusal(f: Flow, **changes: object) -> X402Refusal:
    with pytest.raises(X402Refused) as exc:
        propose(f, **changes)
    return exc.value.code


CHEAP = challenge_doc(requirement_doc(amount="0.00005"))  # 50 micro-tRLUSD: below Nebius's 86


def test_end_to_end_offline_proposal(tmp_path: Path) -> None:
    f = flow(tmp_path, CHEAP)
    assert f.decision.accepted
    assert f.winner == MERCHANT  # Core accepted all three; the cheapest maximum cost wins
    p = propose(f)
    assert p.submission_authorized is False
    assert p.amount.value == "0.00005" and p.destination == f.pinned.pay_to
    assert p.network == APPROVED.network and p.network_id == APPROVED.network_id
    assert p.request_method == "POST"
    assert p.request_digest == f.challenge.observed.request_digest
    assert p.requirement_digest == f.challenge.requirement().requirement_digest
    assert p.expires_at == Timestamp("2026-10-24T15:10:00Z")
    assert p.required_evidence_tier is EvidenceTier.WITNESSED and p.payment_before_delivery
    tx = p.transaction()
    assert tx["InvoiceID"] == hashlib.sha256(INVOICE.encode()).hexdigest().upper()
    assert tx["Memos"] == [{"Memo": {"MemoData": INVOICE.encode().hex().upper()}}]
    assert tx["SendMax"] == tx["Amount"]
    assert tx["SigningPubKey"] == "" and "TxnSignature" not in tx and "Signers" not in tx
    assert unsigned_digest(tx) == p.unsigned_transaction_digest
    assert "InvoiceID not in X1 template" in p.x1_template_gaps
    assert "Memos not in X1 template" in p.x1_template_gaps
    assert "SendMax not in X1 template" in p.x1_template_gaps
    assert not p.x1_template_compatible
    verify_binding(p, INVOICE)

    record = lc.policy_accepted(lc.quoted(p.agent_id, NOW), NOW)
    record = lc.authorization_requested(record, p, NOW)
    assert record.state is lc.PurchaseState.PAYMENT_AUTHORIZATION_PENDING
    ProposalBook().admit(p)


def test_same_inputs_give_the_same_proposal(tmp_path: Path) -> None:
    f = flow(tmp_path, CHEAP)
    assert propose(f) == propose(f)


# --- 6: excessive price --------------------------------------------------------------------


def test_policy_rejection_blocks_the_proposal(tmp_path: Path) -> None:
    f = flow(tmp_path, cap=499)  # merchant asks 500 micro-tRLUSD; the agent signed a 499 cap
    assert not f.decision.accepted
    assert refusal(f) is X402Refusal.POLICY_REJECTED


# --- 7: expired quote ----------------------------------------------------------------------


def test_expired_challenge_blocks_the_proposal(tmp_path: Path) -> None:
    f = flow(tmp_path, CHEAP)
    assert refusal(f, now=Timestamp("2026-10-24T15:10:01Z")) is X402Refusal.CHALLENGE_EXPIRED


# --- 8: altered invoice binding ------------------------------------------------------------


def test_altered_invoice_binding_is_detected(tmp_path: Path) -> None:
    p = propose(flow(tmp_path, CHEAP))
    with pytest.raises(X402Refused) as exc:
        verify_binding(p, "SOME-OTHER-INVOICE")
    assert exc.value.code is X402Refusal.BINDING_ALTERED

    tx = p.transaction()
    tx["InvoiceID"] = "0" * 64
    tampered = replace(p, unsigned_transaction=tuple(sorted(tx.items())))
    with pytest.raises(X402Refused) as exc:
        verify_binding(tampered, INVOICE)
    assert exc.value.code is X402Refusal.BINDING_ALTERED

    tx = p.transaction()
    tx["Memos"] = [{"Memo": {"MemoData": b"other".hex().upper()}}]
    tampered = replace(p, unsigned_transaction=tuple(sorted(tx.items())))
    with pytest.raises(X402Refused) as exc:
        verify_binding(tampered, INVOICE)
    assert exc.value.code is X402Refusal.BINDING_ALTERED

    tx = p.transaction()
    tx["Destination"] = account("thief")
    tampered = replace(p, unsigned_transaction=tuple(sorted(tx.items())))
    with pytest.raises(X402Refused) as exc:
        verify_binding(tampered, INVOICE)  # binding fields intact, digest no longer matches
    assert exc.value.code is X402Refusal.BINDING_ALTERED


def test_decision_for_other_terms_is_refused(tmp_path: Path) -> None:
    f = flow(tmp_path, CHEAP)
    other = replace(f.candidate.compatibility[0], observed_amount="0.0001")
    assert refusal(f, decision=other) is X402Refusal.BINDING_ALTERED


# --- 9: request/resource substitution ------------------------------------------------------


def test_requirement_from_another_challenge_is_refused(tmp_path: Path) -> None:
    f = flow(tmp_path, CHEAP)
    g = flow(
        tmp_path,
        challenge_doc(
            requirement_doc(
                amount="0.00004",
                extra={
                    "issuer": f.challenge.requirement().amount.issuer,
                    "invoiceId": "OTHER-INVOICE",
                },
            )
        ),
    )
    assert refusal(f, requirement=g.challenge.requirement()) is X402Refusal.BINDING_ALTERED


@pytest.mark.parametrize(
    "request_",
    [
        HttpRequest(
            "POST", MERCHANT_URL, (("content-type", "application/json"),), b'{"prompt":"x"}'
        ),
        HttpRequest("GET", MERCHANT_URL),
        HttpRequest("POST", MERCHANT_URL, (), PROBE_BODY),
    ],
)
def test_request_substitution_is_refused(tmp_path: Path, request_: HttpRequest) -> None:
    assert refusal(flow(tmp_path, CHEAP), request=request_) is X402Refusal.RESOURCE_MISMATCH


# --- 10: changed merchant endpoint ---------------------------------------------------------


@pytest.mark.parametrize("field", ["url", "origin", "method", "network", "pay_to"])
def test_challenge_not_matching_the_pinned_endpoint_is_refused(tmp_path: Path, field: str) -> None:
    f = flow(tmp_path, CHEAP)
    value = {
        "url": "https://merchant.example/v2/chat",
        "origin": "https://merchant.example:8443",
        "method": "GET",
        "network": "xrpl:1",
        "pay_to": account("other-pay-to"),
    }[field]
    pinned = replace(f.pinned, **{field: value})
    assert refusal(f, pinned=pinned) is X402Refusal.ENDPOINT_CHANGED


def test_ledger_context_on_another_network_is_refused(tmp_path: Path) -> None:
    f = flow(tmp_path, CHEAP)
    assert refusal(f, ledger=replace(LEDGER, network_id=1)) is X402Refusal.NETWORK_NOT_APPROVED


# --- 12, 13, 19: duplicate invoice, replay, cross-agent ------------------------------------


def test_duplicate_invoice_and_replay_end_to_end(tmp_path: Path) -> None:
    f = flow(tmp_path, CHEAP)
    book = ProposalBook()
    first = propose(f)
    book.admit(first)
    with pytest.raises(X402Refused) as exc:
        book.admit(propose(f))
    assert exc.value.code is X402Refusal.PROPOSAL_REPLAYED
    with pytest.raises(X402Refused) as exc:
        book.admit(propose(f, agent="agent-other", label="job-9"))  # same merchant invoice
    assert exc.value.code is X402Refusal.DUPLICATE_INVOICE
    assert book.get(first.idempotency_key) == first


def test_proposals_for_two_agents_never_share_an_identity(tmp_path: Path) -> None:
    f = flow(tmp_path, CHEAP)
    mine, theirs = propose(f), propose(f, agent="agent-other")
    assert mine.idempotency_key != theirs.idempotency_key
    assert mine.proposal_id != theirs.proposal_id
    record = lc.policy_accepted(lc.quoted("agent-researchbot", NOW), NOW)
    with pytest.raises(X402Refused) as exc:
        lc.authorization_requested(record, theirs, NOW)
    assert exc.value.code is X402Refusal.AGENT_MISMATCH


def test_mainnet_terms_never_reach_a_proposal(tmp_path: Path) -> None:
    f = flow(tmp_path, CHEAP)
    mainnet = replace(
        f.candidate.compatibility[0],
        policy_evaluable=False,
        reasons=(X402Refusal.MAINNET_READ_ONLY,),
    )
    assert refusal(f, decision=mainnet) is X402Refusal.MAINNET_READ_ONLY
