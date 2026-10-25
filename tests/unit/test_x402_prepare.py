"""X402-P0/P2 end to end, offline: probe -> offer -> Core decision -> unsigned proposal -> lifecycle.

Covers brief tests 6-10, 12, 13 and 19 at the preparation boundary. A mock merchant on the
configured local standalone network asks for the local synthetic tRLUSD; Core's real policy engine
decides beside the Nebius and local Qwen candidates. Nothing is signed for payment, submitted or
paid.

X402-P2: the flow (tests/unit/x402_core_world.py) now hands prepare_proposal the Core-evaluated
purchase and the trusted context instead of a caller-chosen intent, decision and compatibility.
"""

from __future__ import annotations

import hashlib
import socket
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from x402_core_world import CHEAP, PAY_LEDGER, flow, propose, qualify, refusal, terms, x402_purchase
from x402_world import (
    APPROVED,
    INVOICE,
    MERCHANT_URL,
    NOW,
    PROBE_BODY,
    account,
    b64,
    challenge_doc,
    probe_request,
    proposal,
    request_auth,
    requirement_doc,
)

from credence.core.primitives import EvidenceTier, Timestamp
from credence.x402 import lifecycle as lc
from credence.x402.challenge import X402Challenge, parse_challenge
from credence.x402.errors import X402Refusal, X402Refused
from credence.x402.proposal import PaymentAuthorityBook, unsigned_digest, verify_binding
from credence.x402.transport import HttpRequest, filter_response


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield


def test_end_to_end_offline_proposal(tmp_path: Path) -> None:
    f = flow(tmp_path, CHEAP)
    assert f.qualification.qualified
    assert f.winner == f.merchant  # Core accepted all three; the cheapest maximum cost wins
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
    verify_binding(p, terms(f))
    assert p.agent_id == f.qualification.purchase.request_signature.key_id.value  # type: ignore[union-attr]
    assert p.source_account == f.facility.borrower_account.value

    record = lc.policy_accepted(lc.quoted(p.agent_id, NOW), NOW)
    record = request_auth(record, p, NOW)
    assert record.state is lc.PurchaseState.PAYMENT_AUTHORIZATION_PENDING


def test_same_inputs_give_the_same_proposal(tmp_path: Path) -> None:
    f = flow(tmp_path, CHEAP)
    assert propose(f) == propose(f)


# --- 6: excessive price --------------------------------------------------------------------


def test_policy_rejection_blocks_the_proposal(tmp_path: Path) -> None:
    f = flow(tmp_path, cap=499)  # merchant asks 500 micro-tRLUSD; the agent signed a 499 cap
    assert not f.qualification.qualified
    assert refusal(f) is X402Refusal.POLICY_REJECTED


# --- 7: expired quote ----------------------------------------------------------------------


def test_expired_challenge_blocks_the_proposal(tmp_path: Path) -> None:
    f = flow(tmp_path, CHEAP)
    assert refusal(f, now=Timestamp("2026-10-24T15:10:01Z")) is X402Refusal.CHALLENGE_EXPIRED


# --- 8: altered invoice binding ------------------------------------------------------------


def test_altered_invoice_binding_is_detected(tmp_path: Path) -> None:
    f = flow(tmp_path, CHEAP)
    p = propose(f)
    t = terms(f)
    other_invoice = replace(t, invoice=replace(t.invoice, invoice_id="SOME-OTHER-INVOICE"))
    with pytest.raises(X402Refused) as exc:
        verify_binding(p, other_invoice)
    assert exc.value.code is X402Refusal.BINDING_ALTERED

    tx = p.transaction()
    tx["InvoiceID"] = "0" * 64
    tampered = replace(p, unsigned_transaction=tuple(sorted(tx.items())))
    with pytest.raises(X402Refused) as exc:
        verify_binding(tampered, t)
    assert exc.value.code is X402Refusal.BINDING_ALTERED

    tx = p.transaction()
    tx["Memos"] = [{"Memo": {"MemoData": b"other".hex().upper()}}]
    tampered = replace(p, unsigned_transaction=tuple(sorted(tx.items())))
    with pytest.raises(X402Refused) as exc:
        verify_binding(tampered, t)
    assert exc.value.code is X402Refusal.BINDING_ALTERED

    tx = p.transaction()
    tx["Destination"] = account("thief")
    tampered = replace(p, unsigned_transaction=tuple(sorted(tx.items())))
    with pytest.raises(X402Refused) as exc:
        verify_binding(tampered, t)  # binding fields intact, digest no longer matches
    assert exc.value.code is X402Refusal.BINDING_ALTERED


def test_decision_for_other_terms_is_refused(tmp_path: Path) -> None:
    # P2: a Core rejection of another purchase (a 1-micro cap) cannot back this proposal.
    f = flow(tmp_path, CHEAP)
    other = qualify(f, [x402_purchase(f, 98, cap=1)]).qualifications[-1]
    assert refusal(f, qualification=other) is X402Refusal.POLICY_REJECTED


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
    ledger = replace(PAY_LEDGER, network_id=1)
    assert refusal(f, ledger=ledger) is X402Refusal.NETWORK_NOT_APPROVED


# --- 12, 13, 19: duplicate invoice, replay, cross-agent ------------------------------------


def test_duplicate_invoice_and_replay_end_to_end(tmp_path: Path) -> None:
    # P2: the second purchase of the same merchant invoice is a separately signed purchase request
    # (another nonce), since the agent identity now comes from the request signature.
    f = flow(tmp_path, CHEAP)
    book = PaymentAuthorityBook()
    first = propose(f)
    book.admit(first)
    with pytest.raises(X402Refused) as exc:
        book.admit(propose(f))
    assert exc.value.code is X402Refusal.PROPOSAL_REPLAYED
    second = qualify(f, [x402_purchase(f, 97)]).qualifications[-1]
    with pytest.raises(X402Refused) as exc:
        book.admit(propose(f, qualification=second))  # same merchant invoice
    assert exc.value.code is X402Refusal.DUPLICATE_INVOICE
    assert book.get(first.idempotency_key) == first


def test_proposals_for_two_agents_never_share_an_identity(tmp_path: Path) -> None:
    f = flow(tmp_path, CHEAP)
    mine = propose(f)
    theirs = proposal("agent-other")  # another agent's proposal (x402_world builder)
    assert mine.idempotency_key != theirs.idempotency_key
    assert mine.proposal_id != theirs.proposal_id
    record = lc.policy_accepted(lc.quoted(mine.agent_id, NOW), NOW)
    with pytest.raises(X402Refused) as exc:
        request_auth(record, theirs, NOW)
    assert exc.value.code is X402Refusal.AGENT_MISMATCH


def test_mainnet_terms_never_reach_a_proposal(tmp_path: Path) -> None:
    # P2: compatibility is recomputed; a Mainnet RLUSD requirement with a matching pin is refused.
    f = flow(tmp_path, CHEAP)
    rlusd = "524C555344000000000000000000000000000000"
    doc = challenge_doc(
        requirement_doc(
            network="xrpl:0",
            asset=rlusd,
            extra={"issuer": "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De", "invoiceId": "MAINNET-1"},
        )
    )
    response = filter_response(402, [("PAYMENT-REQUIRED", b64(doc))], b"", False)
    challenge = parse_challenge(probe_request(), response, NOW)
    assert isinstance(challenge, X402Challenge)
    req = challenge.requirement()
    pinned = replace(f.pinned, network="xrpl:0", pay_to=req.pay_to)
    code = refusal(f, challenge=challenge, requirement=req, pinned=pinned)
    assert code is X402Refusal.MAINNET_READ_ONLY
