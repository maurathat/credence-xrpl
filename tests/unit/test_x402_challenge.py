"""X402-P0: HTTP 402 challenge parsing and the non-paying transport boundary.

Covers brief tests 1-5, 7, 9 (parse level) and 18. Deterministic mock transport only.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from typing import Any

import pytest
from x402_world import (
    APPROVED,
    INVOICE,
    LOCAL_FACILITATOR,
    LOCAL_NETWORK,
    MERCHANT_PAY_TO,
    MERCHANT_URL,
    NOW,
    TRLUSD_ISSUER,
    TRLUSD_WIRE_CODE,
    account,
    b64,
    challenge_doc,
    client,
    probe_request,
    requirement_doc,
    route,
)

from credence.core.primitives import Timestamp
from credence.x402.challenge import (
    IncompatibleChallenge,
    X402Challenge,
    check_live,
    parse_challenge,
    parse_settlement_response,
    request_digest,
)
from credence.x402.compatibility import MultisigSupport, assess
from credence.x402.errors import X402Refusal, X402Refused
from credence.x402.mock import MockTransport
from credence.x402.transport import (
    GuardedClient,
    HttpRequest,
    OriginAllowList,
    filter_response,
)

RLUSD = "524C555344000000000000000000000000000000"
RLUSD_MAINNET_ISSUER = "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De"


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield


def parse(doc: Any, **route_kwargs: Any) -> X402Challenge | IncompatibleChallenge:
    status, headers, body = route(doc, **route_kwargs)
    return parse_challenge(probe_request(), filter_response(status, headers, body, False), NOW)


def parsed(doc: Any, **route_kwargs: Any) -> X402Challenge:
    result = parse(doc, **route_kwargs)
    assert isinstance(result, X402Challenge)
    return result


def refused(doc: Any, **route_kwargs: Any) -> X402Refusal:
    with pytest.raises(X402Refused) as exc:
        parse(doc, **route_kwargs)
    return exc.value.code


# --- 1: valid challenge --------------------------------------------------------------------


def test_valid_challenge_header_and_body() -> None:
    challenge = parsed(challenge_doc())
    req = challenge.requirement()
    assert challenge.version == 2
    assert challenge.observed.carried_in == "header+body"
    assert challenge.observed.http_status == 402
    assert challenge.observed.request_method == "POST"
    assert challenge.observed.request_digest == request_digest(probe_request())
    assert req.network == LOCAL_NETWORK and req.network_id == 63_456
    assert req.amount.currency == TRLUSD_WIRE_CODE and req.amount.issuer == TRLUSD_ISSUER
    assert req.amount.value == "0.0005"
    assert req.pay_to == MERCHANT_PAY_TO
    assert req.invoice_id == INVOICE
    assert req.asset_label == "tRLUSD"
    assert req.advertised_extra == {"execution": {"model": "mock/qwen3-30b"}}
    assert req.raw == requirement_doc()  # kept verbatim for an exact echo


@pytest.mark.parametrize(
    ("header", "body", "where"), [(True, False, "header"), (False, True, "body")]
)
def test_challenge_in_header_only_or_body_only(header: bool, body: bool, where: str) -> None:
    assert parsed(challenge_doc(), header=header, body=body).observed.carried_in == where


def test_validity_window_is_observation_plus_timeout() -> None:
    challenge = parsed(challenge_doc())
    expiry = check_live(challenge.requirement(), challenge.observed, NOW)
    assert expiry == Timestamp("2026-10-24T15:10:00Z")


def test_xrp_amounts_are_drops() -> None:
    doc = challenge_doc(requirement_doc(asset="XRP", amount="1000", extra={"invoiceId": INVOICE}))
    req = parsed(doc).requirement()
    assert req.amount.currency == "XRP" and req.amount.issuer is None and req.amount.value == "1000"


def test_other_chains_are_ignored_not_parsed() -> None:
    evm = {"scheme": "exact", "network": "eip155:8453", "amount": "1", "asset": "0xabc"}
    challenge = parsed(challenge_doc(evm, requirement_doc()))
    assert challenge.ignored == ((0, X402Refusal.UNSUPPORTED_NETWORK),)
    assert len(challenge.requirements) == 1


# --- 2: missing or malformed payment requirements ------------------------------------------


@pytest.mark.parametrize(
    ("doc", "code"),
    [
        (challenge_doc(accepts=[]), X402Refusal.MALFORMED_CHALLENGE),
        (challenge_doc(accepts="exact"), X402Refusal.MALFORMED_CHALLENGE),
        (
            challenge_doc(resource="https://merchant.example/v1/chat"),
            X402Refusal.MALFORMED_CHALLENGE,
        ),
        (challenge_doc(x402Version="2"), X402Refusal.MALFORMED_CHALLENGE),
        (challenge_doc(x402Version=True), X402Refusal.MALFORMED_CHALLENGE),
        (challenge_doc(requirement_doc(amount=5)), X402Refusal.MALFORMED_ASSET),
        (challenge_doc(requirement_doc(amount="1e-3")), X402Refusal.MALFORMED_AMOUNT),
        (challenge_doc(requirement_doc(amount="-0.1")), X402Refusal.MALFORMED_AMOUNT),
        (challenge_doc(requirement_doc(amount="0")), X402Refusal.MALFORMED_AMOUNT),
        (
            challenge_doc(requirement_doc(amount="0.12345678901234567")),
            X402Refusal.MALFORMED_AMOUNT,
        ),
        (challenge_doc(requirement_doc(asset="RLUSD")), X402Refusal.MALFORMED_ASSET),
        (challenge_doc(requirement_doc(payTo="not-an-address")), X402Refusal.MALFORMED_DESTINATION),
        (challenge_doc(requirement_doc(maxTimeoutSeconds=0)), X402Refusal.MALFORMED_CHALLENGE),
        (challenge_doc(requirement_doc(maxTimeoutSeconds=86_400)), X402Refusal.MALFORMED_CHALLENGE),
        (challenge_doc(requirement_doc(maxTimeoutSeconds=60.0)), X402Refusal.MALFORMED_CHALLENGE),
        (
            challenge_doc(requirement_doc(extra={"issuer": TRLUSD_ISSUER})),
            X402Refusal.MALFORMED_INVOICE_BINDING,
        ),
        (
            challenge_doc(requirement_doc(extra={"issuer": TRLUSD_ISSUER, "invoiceId": ""})),
            X402Refusal.MALFORMED_INVOICE_BINDING,
        ),
        (
            challenge_doc(requirement_doc(extra={"invoiceId": INVOICE})),
            X402Refusal.MALFORMED_ASSET,
        ),
        (
            challenge_doc(
                requirement_doc(
                    extra={"issuer": TRLUSD_ISSUER, "invoiceId": INVOICE, "sourceTag": -1}
                )
            ),
            X402Refusal.MALFORMED_CHALLENGE,
        ),
    ],
)
def test_malformed_requirements_are_refused(doc: dict[str, Any], code: X402Refusal) -> None:
    assert refused(doc) is code


def test_402_without_any_challenge_is_malformed() -> None:
    assert refused(None, header=False, raw_body=b"") is X402Refusal.MALFORMED_CHALLENGE


def test_non_json_body_and_bad_base64_are_malformed() -> None:
    assert refused(None, raw_body=b"<html>") is X402Refusal.MALFORMED_CHALLENGE
    bad = [("PAYMENT-REQUIRED", "!!!not-base64!!!")]
    assert (
        refused(None, header=False, extra_headers=bad, raw_body=b"")
        is X402Refusal.MALFORMED_CHALLENGE
    )


def test_duplicate_json_keys_are_contradictory() -> None:
    raw = b'{"x402Version":2,"x402Version":1,"accepts":[]}'
    assert refused(None, header=False, raw_body=raw) is X402Refusal.CONTRADICTORY_CHALLENGE


def test_deeply_nested_json_is_malformed_not_a_crash() -> None:
    raw = b'{"x402Version":2,"accepts":' + b"[" * 20_000 + b"]" * 20_000 + b"}"
    assert refused(None, header=False, raw_body=raw) is X402Refusal.MALFORMED_CHALLENGE


@pytest.mark.parametrize("amount", ["0." + "0" * 99 + "1", "1" + "0" * 96])
def test_issued_amount_outside_xrpl_range_is_refused(amount: str) -> None:
    assert refused(challenge_doc(requirement_doc(amount=amount))) is X402Refusal.MALFORMED_AMOUNT


def test_nan_is_refused() -> None:
    raw = b'{"x402Version":2,"accepts":[{"amount":NaN}]}'
    assert refused(None, header=False, raw_body=raw) is X402Refusal.MALFORMED_CHALLENGE


def test_header_and_body_that_differ_are_contradictory() -> None:
    other = challenge_doc(requirement_doc(amount="0.0001"))
    headers = [("PAYMENT-REQUIRED", b64(other))]
    code = refused(challenge_doc(), header=False, extra_headers=headers)
    assert code is X402Refusal.CONTRADICTORY_CHALLENGE


def test_repeated_payment_required_header_is_contradictory() -> None:
    headers = [("PAYMENT-REQUIRED", b64(challenge_doc(requirement_doc(amount="0.0001"))))]
    assert refused(challenge_doc(), body=False, extra_headers=headers) is (
        X402Refusal.CONTRADICTORY_CHALLENGE
    )


@pytest.mark.parametrize(
    "entry",
    [
        requirement_doc(maxAmountRequired="0.0005"),
        requirement_doc(currency="USD"),
        requirement_doc(invoiceId="SOMETHING-ELSE"),
        requirement_doc(
            asset="XRP", amount="10", extra={"issuer": TRLUSD_ISSUER, "invoiceId": INVOICE}
        ),
    ],
)
def test_contradictory_fields_are_refused(entry: dict[str, Any]) -> None:
    assert refused(challenge_doc(entry)) is X402Refusal.CONTRADICTORY_CHALLENGE


def test_two_prices_for_one_asset_are_contradictory() -> None:
    doc = challenge_doc(
        requirement_doc(),
        requirement_doc(amount="0.0001", extra={"issuer": TRLUSD_ISSUER, "invoiceId": "OTHER"}),
    )
    assert refused(doc) is X402Refusal.CONTRADICTORY_CHALLENGE


def test_cross_currency_is_parsed_and_then_refused_by_compatibility() -> None:
    entry = requirement_doc(
        extra={"issuer": TRLUSD_ISSUER, "invoiceId": INVOICE, "crossCurrency": True}
    )
    decision = assess(parsed(challenge_doc(entry)).requirement(), APPROVED, LOCAL_FACILITATOR)
    assert not decision.policy_evaluable
    assert X402Refusal.CROSS_CURRENCY_UNSUPPORTED in decision.reasons


# --- 3: unsupported version and scheme -----------------------------------------------------


@pytest.mark.parametrize("version", [1, 3])
def test_unsupported_version_is_an_explicit_incompatibility(version: int) -> None:
    result = parse(challenge_doc(x402Version=version))
    assert isinstance(result, IncompatibleChallenge)
    assert result.code is X402Refusal.UNSUPPORTED_VERSION
    assert result.detail == str(version)


def test_v1_shaped_challenge_is_incompatible_not_guessed() -> None:
    v1 = {
        "x402Version": 1,
        "error": "X-PAYMENT header is required",
        "accepts": [{"scheme": "exact", "network": "xrpl-testnet", "maxAmountRequired": "1"}],
    }
    result = parse(v1, header=False)
    assert isinstance(result, IncompatibleChallenge)
    assert result.code is X402Refusal.UNSUPPORTED_VERSION


@pytest.mark.parametrize("scheme", ["upto", "batch-settlement", "something-new"])
def test_unsupported_scheme_is_an_explicit_incompatibility(scheme: str) -> None:
    result = parse(challenge_doc(requirement_doc(scheme=scheme)))
    assert isinstance(result, IncompatibleChallenge)
    assert result.code is X402Refusal.UNSUPPORTED_SCHEME


def test_no_xrpl_requirement() -> None:
    evm = {"scheme": "exact", "network": "eip155:8453", "amount": "1", "asset": "0xabc"}
    result = parse(challenge_doc(evm))
    assert isinstance(result, IncompatibleChallenge)
    assert result.code is X402Refusal.NO_XRPL_REQUIREMENT


# --- 4, 5: network, asset and issuer against Credence's approved execution -----------------


@pytest.mark.parametrize(
    ("network", "code"),
    [
        ("xrpl:0", X402Refusal.MAINNET_READ_ONLY),
        ("xrpl:1", X402Refusal.NETWORK_NOT_APPROVED),
        ("xrpl:2", X402Refusal.NETWORK_NOT_APPROVED),
        ("xrpl:63457", X402Refusal.NETWORK_NOT_APPROVED),
    ],
)
def test_wrong_network_is_recorded_and_not_evaluable(network: str, code: X402Refusal) -> None:
    req = parsed(challenge_doc(requirement_doc(network=network))).requirement()
    decision = assess(req, APPROVED, LOCAL_FACILITATOR)
    assert decision.observed_network == network
    assert decision.approved_network == LOCAL_NETWORK
    assert not decision.policy_evaluable and not decision.executable
    assert code in decision.reasons


@pytest.mark.parametrize("network", ["xrpl-testnet", "xrpl:01", "xrpl:", "XRPL:0"])
def test_unknown_network_spellings_are_not_xrpl(network: str) -> None:
    result = parse(challenge_doc(requirement_doc(network=network)))
    assert isinstance(result, IncompatibleChallenge)
    assert result.code is X402Refusal.NO_XRPL_REQUIREMENT


def test_mainnet_rlusd_is_recorded_exactly_and_read_only() -> None:
    entry = requirement_doc(
        network="xrpl:0", asset=RLUSD, extra={"issuer": RLUSD_MAINNET_ISSUER, "invoiceId": INVOICE}
    )
    decision = assess(parsed(challenge_doc(entry)).requirement(), APPROVED, LOCAL_FACILITATOR)
    assert decision.observed_asset_label == "RLUSD"  # the merchant's asset, not renamed
    assert decision.observed_currency == RLUSD and decision.observed_issuer == RLUSD_MAINNET_ISSUER
    assert decision.approved_asset_label == "tRLUSD"
    assert decision.read_only and not decision.policy_evaluable and not decision.executable
    assert decision.reasons[:2] == (X402Refusal.MAINNET_READ_ONLY, X402Refusal.ASSET_NOT_APPROVED)


def test_wrong_issuer_is_not_evaluable() -> None:
    entry = requirement_doc(extra={"issuer": account("look-alike"), "invoiceId": INVOICE})
    decision = assess(parsed(challenge_doc(entry)).requirement(), APPROVED, LOCAL_FACILITATOR)
    assert X402Refusal.ISSUER_NOT_APPROVED in decision.reasons
    assert not decision.policy_evaluable


def test_compatible_terms_are_evaluable_but_never_executable() -> None:
    decision = assess(parsed(challenge_doc()).requirement(), APPROVED, LOCAL_FACILITATOR)
    assert decision.policy_evaluable
    assert not decision.executable
    assert X402Refusal.SUBMISSION_NOT_AUTHORIZED in decision.reasons
    assert X402Refusal.INVOICE_BINDING_NOT_IN_X1_TEMPLATE in decision.reasons


# --- 7: expired quote ----------------------------------------------------------------------


def test_expired_challenge_is_refused() -> None:
    challenge = parsed(challenge_doc())
    with pytest.raises(X402Refused) as exc:
        check_live(challenge.requirement(), challenge.observed, Timestamp("2026-10-24T15:10:01Z"))
    assert exc.value.code is X402Refusal.CHALLENGE_EXPIRED


def test_challenge_from_the_future_is_refused() -> None:
    challenge = parsed(challenge_doc())
    with pytest.raises(X402Refused):
        check_live(challenge.requirement(), challenge.observed, Timestamp("2026-10-24T14:59:59Z"))


# --- 9: request/resource substitution ------------------------------------------------------


def test_challenge_for_another_resource_is_refused() -> None:
    doc = challenge_doc(resource={"url": "https://merchant.example/v1/other"})
    assert refused(doc) is X402Refusal.RESOURCE_MISMATCH


def test_request_digest_binds_method_url_and_body() -> None:
    base = request_digest(probe_request())
    assert request_digest(HttpRequest("POST", MERCHANT_URL, (), b'{"prompt":"ping"}')) != base
    other_body = HttpRequest("POST", MERCHANT_URL, (("content-type", "application/json"),), b"{}")
    assert request_digest(other_body) != base
    assert request_digest(HttpRequest("GET", MERCHANT_URL)) != base


# --- 18: redirects, origins and payment material -------------------------------------------


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_redirect_is_never_followed(status: int) -> None:
    location = [("location", "https://evil.example/collect")]
    guarded, transport = client(
        {("POST", MERCHANT_URL): route(None, status=status, extra_headers=location)}
    )
    request = probe_request()
    _, response = guarded.send(request)
    with pytest.raises(X402Refused) as exc:
        parse_challenge(request, response, NOW)
    assert exc.value.code is X402Refusal.REDIRECT_REFUSED
    assert [r.url for r in transport.sent] == [MERCHANT_URL]  # nothing sent to the Location


def test_untrusted_origin_is_never_contacted() -> None:
    transport = MockTransport({})
    guarded = GuardedClient(transport, OriginAllowList.of(MERCHANT_URL))
    for url in (
        "https://evil.example/collect",
        "https://merchant.example:8443/v1/chat",
        "http://merchant.example/v1/chat",
        "https://user:pw@merchant.example/v1/chat",
    ):
        with pytest.raises(X402Refused) as exc:
            guarded.send(HttpRequest("GET", url))
        assert exc.value.code is X402Refusal.ORIGIN_NOT_ALLOWED
    assert transport.sent == []


@pytest.mark.parametrize(
    "header",
    [
        "PAYMENT-SIGNATURE",
        "X-PAYMENT",
        "Authorization",
        "cookie",
        "X-API-Key",
        "Proxy-Authorization",
    ],
)
def test_payment_and_credential_headers_cannot_be_sent(header: str) -> None:
    with pytest.raises(X402Refused) as exc:
        HttpRequest("POST", MERCHANT_URL, ((header, "x"),))
    assert exc.value.code is X402Refusal.PAYMENT_MATERIAL_REFUSED


def test_sensitive_response_headers_are_not_kept() -> None:
    guarded, _ = client({("POST", MERCHANT_URL): route(challenge_doc())})
    _, response = guarded.send(probe_request())
    assert "set-cookie" not in response.headers
    assert response.header("payment-required") is not None


def test_only_get_post_head() -> None:
    with pytest.raises(X402Refused) as exc:
        HttpRequest("PUT", MERCHANT_URL)
    assert exc.value.code is X402Refusal.UNSUPPORTED_METHOD


# --- settlement response decoding (merchant claim) -----------------------------------------


def test_settlement_response_decodes_strictly() -> None:
    ok = parse_settlement_response(
        b64(
            {
                "success": True,
                "transaction": "A" * 64,
                "network": "xrpl:0",
                "payer": MERCHANT_PAY_TO,
            }
        )
    )
    assert ok.success and ok.transaction_hash == "A" * 64
    for bad in (
        {"success": "true", "transaction": "A" * 64},
        {"success": True},
        {"success": True, "transaction": "abc"},
        {"success": True, "transaction": "A" * 64, "network": "base"},
        {"success": True, "transaction": "A" * 64, "payer": "nobody"},
    ):
        with pytest.raises(X402Refused) as exc:
            parse_settlement_response(b64(bad))
        assert exc.value.code is X402Refusal.SETTLEMENT_RESPONSE_MISMATCH
    with pytest.raises(X402Refused):
        parse_settlement_response("%%%")


def test_unknown_multisig_support_is_not_assumed() -> None:
    from dataclasses import replace

    unknown = replace(LOCAL_FACILITATOR, multisig=MultisigSupport.UNKNOWN)
    decision = assess(parsed(challenge_doc()).requirement(), APPROVED, unknown)
    assert X402Refusal.MULTISIGNATURE_UNKNOWN in decision.reasons
