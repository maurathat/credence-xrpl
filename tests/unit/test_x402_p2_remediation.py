"""X402-P2 regressions: every X402-P1 review finding (Skopje F1-F4, Ulaanbaatar E1-E4) reproduced
as an attack and required to be refused. Offline; the only sockets are a loopback test server in
the F4 redirect test. Nothing is signed for payment, submitted or paid.
"""

from __future__ import annotations

import http.server
import json
import socket
import threading
from collections.abc import Iterator
from dataclasses import fields, replace
from pathlib import Path
from typing import Any

import pytest
from x402_core_world import (
    CHEAP,
    PAY_LEDGER,
    flow,
    propose,
    qualify,
    refusal,
    terms,
    x402_purchase,
)
from x402_world import (
    APPROVED,
    INVOICE,
    LATER,
    LOCAL_FACILITATOR,
    MERCHANT_URL,
    NOW,
    TRLUSD_ISSUER,
    TX,
    account,
    b64,
    challenge_doc,
    make_pending,
    observe,
    probe_request,
    proposal,
    request_auth,
    requirement_doc,
    validated,
)

from credence.core.primitives import EvidenceTier, MicroTrlusd, ProviderId, Timestamp
from credence.inference.gateway import Purchase
from credence.inference.market import Qualification
from credence.inference.nebius import NEBIUS_PROVIDER_ID
from credence.x402 import lifecycle as lc
from credence.x402.challenge import IncompatibleChallenge, X402Challenge, parse_challenge
from credence.x402.compatibility import assess
from credence.x402.discovery import VerificationStatus
from credence.x402.errors import X402Refusal, X402Refused
from credence.x402.lifecycle import LedgerOutcome, NoTrustedEvidenceSource, PurchaseState
from credence.x402.prepare import offer_from, x402_provider_id
from credence.x402.procurement import ApprovedExecution, OperatorAssertions
from credence.x402.proposal import (
    InvoiceBinding,
    PaymentAuthorityBook,
    PaymentAuthorizationProposal,
    XrplAmount,
    unsigned_digest,
    verify_binding,
)
from credence.x402.transport import (
    GuardedClient,
    HttpRequest,
    OriginAllowList,
    StdlibTransport,
    filter_response,
)

RLUSD = "524C555344000000000000000000000000000000"
RLUSD_MAINNET_ISSUER = "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De"


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield


offline = pytest.mark.usefixtures("no_network")


def parse(headers: list[tuple[str, str]], body: bytes) -> X402Challenge | IncompatibleChallenge:
    return parse_challenge(probe_request(), filter_response(402, headers, body, False), NOW)


def refused_parse(headers: list[tuple[str, str]], body: bytes) -> X402Refusal:
    with pytest.raises(X402Refused) as exc:
        parse(headers, body)
    return exc.value.code


HEADER_CHEAP = challenge_doc(requirement_doc(amount="0.0005"))
BODY_COSTLY = challenge_doc(requirement_doc(amount="0.001"))


# --- F1: challenge consistency regardless of content type ---------------------------------


@offline
@pytest.mark.parametrize(
    "content_type",
    [
        None,
        "text/plain",
        "application/problem+json",
        "application/vnd.x402+json",
        "text/json",
        "application/json; charset=utf-8",
        "text/html",
        "application/octet-stream",
    ],
)
def test_f1_header_and_body_that_differ_are_refused_for_any_content_type(
    content_type: str | None,
) -> None:
    headers = [("PAYMENT-REQUIRED", b64(HEADER_CHEAP))]
    if content_type is not None:
        headers.append(("content-type", content_type))
    code = refused_parse(headers, json.dumps(BODY_COSTLY).encode())
    assert code is X402Refusal.CONTRADICTORY_CHALLENGE


@offline
@pytest.mark.parametrize("content_type", [None, "text/plain", "application/problem+json"])
def test_f1_equal_representations_still_parse(content_type: str | None) -> None:
    headers = [("PAYMENT-REQUIRED", b64(HEADER_CHEAP))]
    if content_type is not None:
        headers.append(("content-type", content_type))
    result = parse(headers, json.dumps(HEADER_CHEAP).encode())
    assert isinstance(result, X402Challenge) and result.observed.carried_in == "header+body"


@offline
def test_f1_body_only_challenge_without_json_content_type_is_recognized() -> None:
    result = parse([("content-type", "text/plain")], json.dumps(BODY_COSTLY).encode())
    assert isinstance(result, X402Challenge)
    assert result.observed.carried_in == "body"
    assert result.requirement().amount.value == "0.001"


@offline
@pytest.mark.parametrize(
    ("header_extra", "body_extra"),
    [
        ({"crossCurrency": False}, {"crossCurrency": 0}),
        ({"sourceTag": 7}, {"sourceTag": 7.0}),
        ({"sourceTag": 1}, {"sourceTag": True}),
    ],
)
def test_f1_json_type_confusion_is_contradictory(
    header_extra: dict[str, Any], body_extra: dict[str, Any]
) -> None:
    base = {"issuer": TRLUSD_ISSUER, "invoiceId": INVOICE}
    header = challenge_doc(requirement_doc(extra={**base, **header_extra}))
    body = challenge_doc(requirement_doc(extra={**base, **body_extra}))
    headers = [("PAYMENT-REQUIRED", b64(header)), ("content-type", "application/json")]
    assert refused_parse(headers, json.dumps(body).encode()) is X402Refusal.CONTRADICTORY_CHALLENGE


@offline
def test_f1_escaped_keys_are_still_recognized() -> None:
    body = json.dumps(BODY_COSTLY).replace('"x402Version"', '"\\u0078402Version"')
    body = body.replace('"accepts"', '"\\u0061ccepts"')
    headers = [("PAYMENT-REQUIRED", b64(HEADER_CHEAP)), ("content-type", "text/plain")]
    assert refused_parse(headers, body.encode()) is X402Refusal.CONTRADICTORY_CHALLENGE


@offline
@pytest.mark.parametrize(
    ("content_type", "body"),
    [
        ("text/plain", b'{"error": {"x402Version": 2, "accepts": []}}'),  # nested challenge
        ("text/plain", b'[{"x402Version": 2}]'),  # challenge inside an array
        ("application/json; charset=iso-8859-1", json.dumps(BODY_COSTLY).encode()),
        ("text/plain", "﻿".encode("utf-16") + json.dumps(BODY_COSTLY).encode("utf-16-le")),
        ("text/plain", b"\xef\xbb\xbf" + json.dumps(BODY_COSTLY).encode()),  # BOM before JSON
        ("text/plain", b'{"x402Version": 2, "accepts": [}'),  # looks like JSON, does not parse
        (None, b'{"x402Version": 2, "x402Version": 2, "accepts": []}'),  # duplicate keys
    ],
)
def test_f1_ambiguous_bodies_are_refused(content_type: str | None, body: bytes) -> None:
    headers = [("PAYMENT-REQUIRED", b64(HEADER_CHEAP))]
    if content_type is not None:
        headers.append(("content-type", content_type))
    assert refused_parse(headers, body) in (
        X402Refusal.MALFORMED_CHALLENGE,
        X402Refusal.CONTRADICTORY_CHALLENGE,
    )


@offline
@pytest.mark.parametrize(
    ("content_type", "body"),
    [
        ("text/html", b"<html><body>Payment required</body></html>"),
        ("application/json", b'{"error": "PAYMENT-SIGNATURE header is required"}'),
        ("text/plain", b"payment required"),
        (None, b""),
    ],
)
def test_f1_non_challenge_error_bodies_are_still_ignored(
    content_type: str | None, body: bytes
) -> None:
    headers = [("PAYMENT-REQUIRED", b64(HEADER_CHEAP))]
    if content_type is not None:
        headers.append(("content-type", content_type))
    result = parse(headers, body)
    assert isinstance(result, X402Challenge) and result.observed.carried_in == "header"
    assert result.requirement().amount.value == "0.0005"


@offline
def test_f1_size_limit_and_version_rules_are_preserved() -> None:
    response = filter_response(402, [("content-type", "text/plain")], b"{" * 10, True)
    with pytest.raises(X402Refused) as exc:
        parse_challenge(probe_request(), response, NOW)
    assert exc.value.code is X402Refusal.MALFORMED_CHALLENGE
    v1 = parse([("content-type", "text/plain")], json.dumps(challenge_doc(x402Version=1)).encode())
    assert isinstance(v1, IncompatibleChallenge) and v1.code is X402Refusal.UNSUPPORTED_VERSION


# --- F2: only authenticated validated tec results are terminal failures --------------------


def pending_record() -> lc.PurchaseRecord:
    p = proposal()
    record = request_auth(lc.policy_accepted(lc.quoted(p.agent_id, NOW), NOW), p, NOW)
    return make_pending(record)


@offline
@pytest.mark.parametrize("code", ["terQUEUED", "telINSUF_FEE_P", "tefPAST_SEQ", "temBAD_AMOUNT"])
def test_f2_non_tec_codes_are_not_validated_failures(code: str) -> None:
    record = pending_record()
    obs = validated(record, outcome=LedgerOutcome.VALIDATED_FAILURE, result_code=code)
    with pytest.raises(X402Refused) as exc:
        observe(record, obs)
    assert exc.value.code is X402Refusal.PAYMENT_EVIDENCE_MISMATCH
    assert record.state is PurchaseState.PAYMENT_PENDING


@offline
@pytest.mark.parametrize(
    "code", ["terQUEUED", "telINSUF_FEE_P", "tefPAST_SEQ", "temBAD_AMOUNT", "tesSUCCESS"]
)
def test_f2_provisional_results_leave_the_payment_unknown(code: str) -> None:
    record = pending_record()
    obs = lc.PaymentObservation(
        record.agent_id,
        record.proposal.proposal_id,  # type: ignore[union-attr]
        LedgerOutcome.PROVISIONAL,
        LATER,
        transaction_hash=TX,
        result_code=code,
    )
    after = observe(record, obs)
    assert after.state is PurchaseState.PAYMENT_STATUS_UNKNOWN
    assert after.payment is None and not after.may_propose_payment


@offline
def test_f2_past_seq_then_validated_success_confirms() -> None:
    record = pending_record()
    provisional = lc.PaymentObservation(
        record.agent_id,
        record.proposal.proposal_id,  # type: ignore[union-attr]
        LedgerOutcome.PROVISIONAL,
        LATER,
        transaction_hash=TX,
        result_code="tefPAST_SEQ",
    )
    record = observe(record, provisional)
    record = observe(record, validated(record))
    assert record.state is PurchaseState.PAYMENT_CONFIRMED


@offline
def test_f2_validated_tec_is_terminal_failure() -> None:
    record = pending_record()
    obs = validated(record, outcome=LedgerOutcome.VALIDATED_FAILURE, result_code="tecPATH_DRY")
    assert observe(record, obs).state is PurchaseState.PAYMENT_REJECTED


@offline
@pytest.mark.parametrize("code", [None, "", "tecpath_dry", "xyzFOO", "tes", "tecPATH DRY"])
def test_f2_missing_or_unsupported_codes_are_invalid(code: str | None) -> None:
    record = pending_record()
    obs = validated(record, outcome=LedgerOutcome.VALIDATED_FAILURE, result_code=code)
    with pytest.raises(X402Refused) as exc:
        observe(record, obs)
    assert exc.value.code is X402Refusal.PAYMENT_EVIDENCE_MISMATCH


@offline
def test_f2_success_claimed_as_failure_is_invalid() -> None:
    record = pending_record()
    obs = validated(record, outcome=LedgerOutcome.VALIDATED_FAILURE, result_code="tesSUCCESS")
    with pytest.raises(X402Refused):
        observe(record, obs)


@offline
def test_f2_unauthenticated_validated_evidence_is_refused() -> None:
    record = pending_record()
    forged = replace(validated(record), observed_at=LATER.plus_seconds(1))  # not issued by TRUSTED
    with pytest.raises(X402Refused) as exc:
        observe(record, forged)
    assert exc.value.code is X402Refusal.PAYMENT_EVIDENCE_UNAUTHENTICATED
    p = record.proposal
    assert p is not None
    with pytest.raises(X402Refused) as exc:  # the default: no trusted source configured
        lc.payment_observed(
            record,
            validated(record),
            verifier=NoTrustedEvidenceSource(),
            book=PaymentAuthorityBook(),
        )
    assert exc.value.code is X402Refusal.PAYMENT_EVIDENCE_UNAUTHENTICATED


@offline
@pytest.mark.parametrize(
    "change",
    [
        {"validated_ledger_index": None},
        {"validated_ledger_index": 0},
        {"validated_ledger_hash": None},
        {"validated_ledger_hash": "c" * 64},
    ],
)
def test_f2_validated_evidence_needs_a_validated_ledger(change: dict[str, object]) -> None:
    record = pending_record()
    with pytest.raises(X402Refused) as exc:
        observe(record, validated(record, **change))
    assert exc.value.code is X402Refusal.PAYMENT_EVIDENCE_MISMATCH


# --- F3 / E3: every material proposal field is bound --------------------------------------


def _mutations(p: PaymentAuthorizationProposal) -> dict[str, PaymentAuthorizationProposal]:
    tx = p.transaction()
    tx_dest = {**tx, "Destination": account("thief")}
    tx_digest = unsigned_digest(tx_dest)
    out: dict[str, PaymentAuthorizationProposal] = {
        "proposal_id": replace(p, proposal_id="sha256:" + "0" * 64),
        "intent.agent_id": replace(p, intent=replace(p.intent, agent_id="agent-other")),
        "intent.facility_id": replace(p, intent=replace(p.intent, facility_id="facility-x")),
        "intent.purchase_request_digest": replace(
            p, intent=replace(p.intent, purchase_request_digest="sha256:" + "1" * 64)
        ),
        "intent.prompt_digest": replace(
            p, intent=replace(p.intent, prompt_digest="sha256:" + "2" * 64)
        ),
        "merchant_id": replace(p, merchant_id=NEBIUS_PROVIDER_ID.value),
        "resource_url": replace(p, resource_url="https://merchant.example/v2/chat"),
        "request_method": replace(p, request_method="GET"),
        "request_digest": replace(p, request_digest="sha256:" + "3" * 64),
        "requirement_digest": replace(p, requirement_digest="sha256:" + "4" * 64),
        "invoice.invoice_id": replace(p, invoice=replace(p.invoice, invoice_id="OTHER")),
        "invoice.source_tag": replace(p, invoice=replace(p.invoice, source_tag=804681468)),
        "invoice.destination_tag": replace(p, invoice=replace(p.invoice, destination_tag=7)),
        "amount.value": replace(p, amount=replace(p.amount, value="99")),
        "amount.currency": replace(p, amount=replace(p.amount, currency=RLUSD)),
        "amount.issuer": replace(p, amount=replace(p.amount, issuer=account("fake-issuer"))),
        "destination (tx bytes unchanged)": replace(p, destination=account("thief")),
        "network": replace(p, network="xrpl:0"),
        "ledger.network_id": replace(p, ledger=replace(p.ledger, network_id=0)),
        "ledger.source_account": replace(p, ledger=replace(p.ledger, source_account=account("x"))),
        "ledger.fee_drops": replace(p, ledger=replace(p.ledger, fee_drops=9_999)),
        "ledger.sequence": replace(p, ledger=replace(p.ledger, sequence=8)),
        "ledger.last_ledger_sequence": replace(p, ledger=replace(p.ledger, last_ledger_sequence=9)),
        "expires_at": replace(p, expires_at=Timestamp("2027-01-01T00:00:00Z")),
        "unsigned_transaction": replace(p, unsigned_transaction=tuple(sorted(tx_dest.items()))),
        "unsigned_transaction + recomputed digest": replace(
            p,
            unsigned_transaction=tuple(sorted(tx_dest.items())),
            unsigned_transaction_digest=tx_digest,
        ),
        "unsigned_transaction_digest": replace(p, unsigned_transaction_digest="A" * 64),
        "unsigned_transaction_digest_scheme": replace(
            p, unsigned_transaction_digest_scheme="other"
        ),
        "idempotency_key": replace(p, idempotency_key="sha256:" + "5" * 64),
        "required_evidence_tier": replace(p, required_evidence_tier=EvidenceTier.REQUESTED),
        "payment_before_delivery": replace(p, payment_before_delivery=False),
        "x1_template_gaps": replace(p, x1_template_gaps=()),
        "submission_authorized": replace(p, submission_authorized=True),  # type: ignore[arg-type]
    }
    return out


@offline
def test_f3_every_proposal_field_is_covered_by_a_mutation(tmp_path: Path) -> None:
    p = propose(flow(tmp_path, CHEAP))
    covered = {name.split(".")[0].split(" ")[0] for name in _mutations(p)}
    assert covered == {f.name for f in fields(p)}


@offline
def test_f3_each_field_mutated_independently_is_refused(tmp_path: Path) -> None:
    f = flow(tmp_path, CHEAP)
    p, t = propose(f), terms(f)
    verify_binding(p, t)  # the genuine proposal passes
    for name, mutated in _mutations(p).items():
        with pytest.raises(X402Refused) as exc:
            verify_binding(mutated, t)
        assert exc.value.code is X402Refusal.BINDING_ALTERED, name


@offline
def test_f3_consistent_forgery_from_other_terms_is_refused(tmp_path: Path) -> None:
    """A proposal rebuilt self-consistently from altered terms (destination, amount, network or
    expiry) passes its own reconstruction but not the verified terms."""
    f = flow(tmp_path, CHEAP)
    t = terms(f)
    from credence.x402.proposal import build_proposal

    for altered in (
        replace(t, destination=account("thief")),
        replace(t, amount=replace(t.amount, value="0.001")),
        replace(t, invoice=InvoiceBinding("FORGED", None, None)),
        replace(t, expires_at=Timestamp("2027-01-01T00:00:00Z")),
        replace(t, merchant_id=NEBIUS_PROVIDER_ID.value),
    ):
        forged = build_proposal(**vars(altered))
        with pytest.raises(X402Refused) as exc:
            verify_binding(forged, t)
        assert exc.value.code is X402Refusal.BINDING_ALTERED


# --- F4: outbound header allow-list --------------------------------------------------------


@offline
@pytest.mark.parametrize(
    "headers",
    [
        (("content-type", "application/json"),),
        (("Content-Type", "application/json; charset=utf-8"),),
        (("accept", "application/json"),),
        (("ACCEPT", "*/*"), ("content-type", "application/json")),
    ],
)
def test_f4_allow_listed_headers_pass(headers: tuple[tuple[str, str], ...]) -> None:
    HttpRequest("POST", MERCHANT_URL, headers, b"{}")


@offline
@pytest.mark.parametrize(
    ("name", "value", "code"),
    [
        ("Authorization", "Bearer x", X402Refusal.PAYMENT_MATERIAL_REFUSED),
        ("Proxy-Authorization", "Basic x", X402Refusal.PAYMENT_MATERIAL_REFUSED),
        ("Cookie", "a=b", X402Refusal.PAYMENT_MATERIAL_REFUSED),
        ("X-Forwarded-Authorization", "x", X402Refusal.PAYMENT_MATERIAL_REFUSED),
        ("X-Session-Cookie", "x", X402Refusal.PAYMENT_MATERIAL_REFUSED),
        ("PAYMENT-SIGNATURE", "x", X402Refusal.PAYMENT_MATERIAL_REFUSED),
        ("X-PAYMENT", "x", X402Refusal.PAYMENT_MATERIAL_REFUSED),
        ("X-Payment-Secure", "x", X402Refusal.PAYMENT_MATERIAL_REFUSED),
        ("X-Custom-Token", "x", X402Refusal.HEADER_NOT_ALLOWED),
        ("User-Agent", "credence", X402Refusal.HEADER_NOT_ALLOWED),
        ("Host", "evil.example", X402Refusal.HEADER_NOT_ALLOWED),
        ("Authorization ", "x", X402Refusal.HEADER_NOT_ALLOWED),
        (" Cookie", "x", X402Refusal.HEADER_NOT_ALLOWED),
        ("Content-Type\t", "application/json", X402Refusal.HEADER_NOT_ALLOWED),
        ("X-Bad\tName", "x", X402Refusal.HEADER_NOT_ALLOWED),
        ("content type", "application/json", X402Refusal.HEADER_NOT_ALLOWED),
        ("", "x", X402Refusal.HEADER_NOT_ALLOWED),
        ("Accépt", "x", X402Refusal.HEADER_NOT_ALLOWED),
        ("accept", "a\r\nAuthorization: x", X402Refusal.HEADER_NOT_ALLOWED),
        ("accept", " application/json", X402Refusal.HEADER_NOT_ALLOWED),
        ("accept", "", X402Refusal.HEADER_NOT_ALLOWED),
        ("content-type", "text/plain", X402Refusal.HEADER_NOT_ALLOWED),
        ("content-type", "application/json; boundary=x", X402Refusal.HEADER_NOT_ALLOWED),
    ],
)
def test_f4_everything_else_is_refused(name: str, value: str, code: X402Refusal) -> None:
    with pytest.raises(X402Refused) as exc:
        HttpRequest("POST", MERCHANT_URL, ((name, value),))
    assert exc.value.code is code


@offline
def test_f4_repeated_and_non_text_headers_are_refused() -> None:
    with pytest.raises(X402Refused):
        HttpRequest("GET", MERCHANT_URL, (("accept", "a/b"), ("Accept", "c/d")))
    with pytest.raises(X402Refused):
        HttpRequest("GET", MERCHANT_URL, ((b"accept", "x"),))  # type: ignore[arg-type]


class _Recorder(http.server.BaseHTTPRequestHandler):
    requests: list[tuple[str, str, list[str]]] = []

    def _answer(self) -> None:
        type(self).requests.append(
            (self.command, self.path, sorted(k.lower() for k in self.headers.keys()))
        )
        if self.path == "/v1/chat":
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/collect")
            self.send_header("Set-Cookie", "session=hostile")
            self.end_headers()
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"collected")

    do_GET = _answer
    do_POST = _answer

    def log_message(self, format: str, *args: object) -> None:
        return


def test_f4_real_transport_never_follows_a_hostile_redirect() -> None:
    _Recorder.requests = []
    server = http.server.HTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/v1/chat"
        client = GuardedClient(StdlibTransport(timeout_seconds=5), OriginAllowList.of(url))
        request = HttpRequest("POST", url, (("content-type", "application/json"),), b"{}")
        _, response = client.send(request)
        with pytest.raises(X402Refused) as exc:
            parse_challenge(request, response, NOW)
        assert exc.value.code is X402Refusal.REDIRECT_REFUSED
    finally:
        server.shutdown()
        server.server_close()
    assert response.status == 302
    assert "set-cookie" not in response.headers
    assert [(m, p) for m, p, _ in _Recorder.requests] == [("POST", "/v1/chat")]  # no second request
    sent = set(_Recorder.requests[0][2])
    assert sent <= {"host", "accept-encoding", "content-length", "content-type"}


# --- E1: proposals bind real Core decisions ------------------------------------------------


@offline
def test_e1_fifty_micro_approval_cannot_back_a_thousand_micro_proposal(tmp_path: Path) -> None:
    cheap = flow(tmp_path, CHEAP)  # Core approved 50 micro-tRLUSD
    costly = flow(tmp_path, challenge_doc(requirement_doc(amount="0.001")))  # 1,000 micro
    assert cheap.qualification.decision.reservation_amount == MicroTrlusd(50)
    assert cheap.merchant == costly.merchant  # same endpoint, so the same x402 identity
    code = refusal(costly, qualification=cheap.qualification)
    assert code is X402Refusal.CORE_DECISION_MISMATCH


@offline
def test_e1_nebius_approval_cannot_be_reused_for_x402(tmp_path: Path) -> None:
    f = flow(tmp_path, CHEAP)
    nebius = f.selection.qualifications[0]
    assert nebius.purchase.request.provider_id == NEBIUS_PROVIDER_ID and nebius.qualified
    assert refusal(f, qualification=nebius) is X402Refusal.PROVIDER_NAMESPACE


@offline
def test_e1_mismatched_purchase_and_facility_identity_is_refused(tmp_path: Path) -> None:
    f = flow(tmp_path, CHEAP)
    q = f.qualification
    other = qualify(f, [x402_purchase(f, 98)]).qualifications[-1]
    swapped_decision = Qualification(q.purchase, other.decision, q.currently_reserved, False)
    assert refusal(f, qualification=swapped_decision) is X402Refusal.CORE_DECISION_MISMATCH
    other_facility = replace(q.purchase.facility, limit_micro_trlusd=MicroTrlusd(10**9))
    swapped_facility = replace(q, purchase=replace(q.purchase, facility=other_facility))
    assert refusal(f, qualification=swapped_facility) is X402Refusal.CORE_DECISION_MISMATCH


@offline
def test_e1_forged_acceptance_is_recomputed_and_refused(tmp_path: Path) -> None:
    f = flow(tmp_path, cap=499)  # Core rejected: PRICE_ABOVE_MAX
    q = f.qualification
    accepted = flow(tmp_path, CHEAP).qualification.decision
    forged = replace(q, decision=replace(accepted, record=replace(accepted.record)))
    assert refusal(f, qualification=forged) in (
        X402Refusal.CORE_DECISION_MISMATCH,
        X402Refusal.POLICY_REJECTED,
    )
    lie = replace(
        q.decision,
        record=replace(q.decision.record, outcome=accepted.record.outcome, reason_code=None),
        reservation_amount=MicroTrlusd(500),
    )
    assert refusal(f, qualification=replace(q, decision=lie)) is X402Refusal.CORE_DECISION_MISMATCH


@offline
def test_e1_reserved_headroom_that_would_reject_is_refused(tmp_path: Path) -> None:
    f = flow(tmp_path, CHEAP)
    q = replace(f.qualification, currently_reserved=MicroTrlusd(10_000_000))
    assert refusal(f, qualification=q) is X402Refusal.CORE_DECISION_MISMATCH


@offline
def test_e1_merchant_cannot_take_a_reserved_or_foreign_identity(tmp_path: Path) -> None:
    f = flow(tmp_path, CHEAP)
    assert x402_provider_id(f.pinned).value.startswith("x402.merchant.example.")
    moved = replace(f.pinned, pay_to=account("other-pay-to"))
    assert x402_provider_id(moved) != f.merchant  # identity is bound to the destination
    # The pinned endpoint is part of the check: a purchase for one merchant cannot back another.
    with pytest.raises(X402Refused):
        propose(f, pinned=moved)


@offline
def test_e1_request_body_must_be_the_signed_prompt(tmp_path: Path) -> None:
    f = flow(tmp_path, CHEAP)
    from inference_world import Scenario, make_purchase

    p = f.projection
    other_prompt = make_purchase(
        Scenario(f.facility, p.quote, p.service_class, p.execution_profile, f.procurement),
        96,
        request_prompt=b'{"prompt":"something else"}',
    )
    from credence.core.reservation_ledger import ReservationLedger
    from credence.inference.market import qualify_and_select

    selection = qualify_and_select(
        [other_prompt],
        registry=f.registry,
        ledger=ReservationLedger(tmp_path / "other-prompt.sqlite3"),
        now=NOW,
    )
    q = selection.qualifications[0]
    assert q.qualified  # Core accepted it: the mismatch is only between prompt and HTTP body
    assert refusal(f, qualification=q) is X402Refusal.RESOURCE_MISMATCH


@offline
def test_e1_paying_account_must_be_the_facility_borrower(tmp_path: Path) -> None:
    f = flow(tmp_path, CHEAP)
    ledger = replace(PAY_LEDGER, source_account=account("someone-else"))
    assert refusal(f, ledger=ledger) is X402Refusal.AGENT_MISMATCH


@offline
def test_e1_unsigned_purchase_is_refused(tmp_path: Path) -> None:
    f = flow(tmp_path, CHEAP)
    q = f.qualification
    unsigned: Purchase = replace(q.purchase, request_signature=None)
    code = refusal(f, qualification=replace(q, purchase=unsigned))
    assert code is X402Refusal.CORE_DECISION_MISMATCH


@offline
def test_e1_intent_comes_from_the_signed_request(tmp_path: Path) -> None:
    f = flow(tmp_path, CHEAP)
    p = propose(f)
    request = f.qualification.purchase.request
    assert p.intent.purchase_request_digest == request.digest().value
    assert p.intent.facility_id == request.facility_id.value
    assert p.intent.prompt_digest == request.prompt_digest.value
    assert p.merchant_id == request.provider_id.value == f.merchant.value


# --- E2: compatibility is recomputed --------------------------------------------------------


def _mainnet_candidate(tmp_path: Path) -> Any:
    f = flow(tmp_path, CHEAP)
    doc = challenge_doc(
        requirement_doc(
            network="xrpl:0",
            asset=RLUSD,
            extra={"issuer": RLUSD_MAINNET_ISSUER, "invoiceId": "MAINNET-1"},
        )
    )
    challenge = parse([("PAYMENT-REQUIRED", b64(doc))], b"")
    assert isinstance(challenge, X402Challenge)
    req = challenge.requirement()
    forged_decision = replace(
        assess(req, APPROVED, LOCAL_FACILITATOR), policy_evaluable=True, reasons=()
    )
    candidate = replace(
        f.candidate,
        status=VerificationStatus.CHALLENGE_COMPATIBLE,
        challenge=challenge,
        compatibility=(forged_decision,),
    )
    return f, candidate, challenge, req


@offline
def test_e2_forged_candidate_compatibility_is_recomputed(tmp_path: Path) -> None:
    _, candidate, _, _ = _mainnet_candidate(tmp_path)
    assert candidate.selectable  # the forged candidate claims to be selectable
    with pytest.raises(X402Refused) as exc:
        offer_from(
            candidate,
            approved=APPROVED,
            facilitator=LOCAL_FACILITATOR,
            model_family="m",
            assertions=OperatorAssertions(),
            now=NOW,
        )
    assert exc.value.code is X402Refusal.SERVICE_UNVERIFIED


@offline
def test_e2_mainnet_rlusd_never_reaches_a_proposal(tmp_path: Path) -> None:
    f, _, challenge, req = _mainnet_candidate(tmp_path)
    pinned = replace(f.pinned, network="xrpl:0", pay_to=req.pay_to)
    for ledger in (PAY_LEDGER, replace(PAY_LEDGER, network_id=0)):
        code = refusal(f, challenge=challenge, requirement=req, pinned=pinned, ledger=ledger)
        assert code is X402Refusal.MAINNET_READ_ONLY


@offline
@pytest.mark.parametrize(("network", "network_id"), [("xrpl:0", 0), ("xrpl:1", 1), ("xrpl:2", 2)])
def test_e2_no_public_network_can_be_approved(network: str, network_id: int) -> None:
    with pytest.raises(X402Refused) as exc:
        ApprovedExecution(network, network_id, RLUSD, RLUSD_MAINNET_ISSUER)
    assert exc.value.code is X402Refusal.NETWORK_NOT_APPROVED
    with pytest.raises(X402Refused):
        ApprovedExecution("xrpl:70000", 63_456, APPROVED.currency, APPROVED.issuer)


@offline
def test_e2_ledger_must_be_on_the_approved_network(tmp_path: Path) -> None:
    f = flow(tmp_path, CHEAP)
    for network_id in (0, 1, 63_457):
        ledger = replace(PAY_LEDGER, network_id=network_id)
        assert refusal(f, ledger=ledger) is X402Refusal.NETWORK_NOT_APPROVED


# --- E4: one purchase, one payment authority -----------------------------------------------


@offline
def test_e4_rechallenge_with_a_new_invoice_cannot_authorize_twice(tmp_path: Path) -> None:
    f = flow(tmp_path, CHEAP)
    book = PaymentAuthorityBook()
    first = propose(f)
    record = lc.policy_accepted(lc.quoted(first.agent_id, NOW), NOW)
    lc.authorization_requested(record, first, NOW, book=book)
    rechallenge = challenge_doc(
        requirement_doc(amount="0.00005", extra={"issuer": TRLUSD_ISSUER, "invoiceId": "NEW-INV"})
    )
    g = flow(tmp_path, rechallenge)
    again = propose(g, qualification=f.qualification)  # same signed purchase, new invoice
    assert again.intent.purchase_request_digest == first.intent.purchase_request_digest
    with pytest.raises(X402Refused) as exc:
        lc.authorization_requested(
            lc.policy_accepted(lc.quoted(again.agent_id, NOW), NOW), again, NOW, book=book
        )
    assert exc.value.code is X402Refusal.PURCHASE_ALREADY_AUTHORIZED


@offline
@pytest.mark.parametrize("outcome", ["timeout", "provisional", "rejected"])
def test_e4_no_outcome_reopens_authorization(outcome: str) -> None:
    p = proposal(invoice_id="A" * 64)
    book = PaymentAuthorityBook()
    record = lc.authorization_requested(
        lc.policy_accepted(lc.quoted(p.agent_id, NOW), NOW), p, NOW, book=book
    )
    record = lc.payment_pending(record, NOW, transaction_hash=TX, book=book)
    trusted = _trusted()
    if outcome == "timeout":
        obs = lc.PaymentObservation(p.agent_id, p.proposal_id, LedgerOutcome.TIMEOUT, LATER)
    elif outcome == "provisional":
        obs = lc.PaymentObservation(
            p.agent_id, p.proposal_id, LedgerOutcome.PROVISIONAL, LATER, TX, "terQUEUED"
        )
    else:
        obs = trusted.issue(
            replace(
                validated(record), outcome=LedgerOutcome.VALIDATED_FAILURE, result_code="tecNO_DST"
            )
        )
    record = lc.payment_observed(record, obs, verifier=trusted, book=book)
    assert not record.may_propose_payment
    second = rechallenged(p, "Z" * 64)  # same purchase request, merchant re-challenge
    assert second.intent.purchase_request_digest == p.intent.purchase_request_digest
    assert second.idempotency_key != p.idempotency_key
    with pytest.raises(X402Refused) as exc:
        lc.authorization_requested(
            lc.policy_accepted(lc.quoted(p.agent_id, NOW), NOW), second, NOW, book=book
        )
    assert exc.value.code is X402Refusal.PURCHASE_ALREADY_AUTHORIZED


def rechallenged(p: PaymentAuthorizationProposal, invoice_id: str) -> PaymentAuthorizationProposal:
    """The same purchase request against a new merchant challenge (new invoice and requirement)."""
    from credence.x402.proposal import build_proposal

    t = p.terms()
    return build_proposal(
        **vars(
            replace(
                t,
                invoice=replace(t.invoice, invoice_id=invoice_id),
                requirement_digest="sha256:" + "9" * 64,
            )
        )
    )


def _trusted() -> Any:
    from x402_world import TRUSTED

    return TRUSTED


@offline
def test_e4_one_transaction_hash_backs_one_purchase() -> None:
    book = PaymentAuthorityBook()
    a = proposal(label="job-1", invoice_id="A" * 64)
    b = proposal(label="job-2", invoice_id="E" * 64)
    ra = lc.authorization_requested(
        lc.policy_accepted(lc.quoted(a.agent_id, NOW), NOW), a, NOW, book=book
    )
    rb = lc.authorization_requested(
        lc.policy_accepted(lc.quoted(b.agent_id, NOW), NOW), b, NOW, book=book
    )
    ra = lc.payment_pending(ra, NOW, transaction_hash="D" * 64, book=book)
    with pytest.raises(X402Refused) as exc:
        lc.payment_pending(rb, NOW, transaction_hash="D" * 64, book=book)
    assert exc.value.code is X402Refusal.TRANSACTION_ALREADY_BOUND
    with pytest.raises(X402Refused) as exc:
        book.bind_transaction(a, "F" * 64)  # a proposal cannot switch to another transaction
    assert exc.value.code is X402Refusal.TRANSACTION_ALREADY_BOUND
    confirmed = lc.payment_observed(ra, validated(ra), verifier=_trusted(), book=book)
    assert confirmed.state is PurchaseState.PAYMENT_CONFIRMED
    with pytest.raises(X402Refused):
        book.confirm(b, "D" * 64)


@offline
@pytest.mark.parametrize(
    "change",
    [
        {"transaction_hash": "E" * 64},
        {"unsigned_transaction_digest": "0" * 64},
        {"invoice_id_field": "0" * 64},
        {"memo_data": "00"},
        {"source_tag": 804681468},
        {"destination_tag": 1},
    ],
)
def test_e4_confirmation_must_be_the_expected_transaction(change: dict[str, object]) -> None:
    record = pending_record()
    with pytest.raises(X402Refused) as exc:
        observe(record, validated(record, **change))
    assert exc.value.code is X402Refusal.PAYMENT_EVIDENCE_MISMATCH
    assert record.state is PurchaseState.PAYMENT_PENDING


@offline
def test_e4_paid_but_undelivered_is_preserved_and_not_repaid() -> None:
    record = pending_record()
    record = observe(record, validated(record))
    record = lc.delivery_failed(lc.delivery_started(record, LATER), "connection reset", LATER)
    assert record.state is PurchaseState.DELIVERY_FAILED
    assert record.payment is not None and record.transaction_hash == TX
    assert not record.may_propose_payment
    with pytest.raises(X402Refused):
        make_pending(record, LATER)


@offline
def test_e4_amounts_are_exact_strings_not_rescaled() -> None:
    assert XrplAmount("A" * 40, TRLUSD_ISSUER, "0.25") != XrplAmount(
        "A" * 40, TRLUSD_ISSUER, "0.250"
    )
    record = pending_record()
    unexpected = observe(record, validated(record, delivered_amount="0.250"))
    assert unexpected.state is PurchaseState.VALIDATED_UNEXPECTED_DELIVERY


@offline
def test_adapter_remains_non_paying(tmp_path: Path) -> None:
    p = propose(flow(tmp_path, CHEAP))
    assert p.submission_authorized is False
    assert APPROVED.submission_authorized is False
    tx = p.transaction()
    assert tx["SigningPubKey"] == "" and "TxnSignature" not in tx and "Signers" not in tx
    assert ProviderId(p.merchant_id).value.startswith("x402.")
