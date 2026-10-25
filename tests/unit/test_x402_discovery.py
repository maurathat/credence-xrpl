"""X402-P0: discovery (advertised vs observed), endpoint pinning, facilitator profile, multisig.

Covers brief tests 10 and 11, and discovery section 4. Directory and facilitator data come from
the recorded fixtures in contracts/fixtures/x402_p0/; merchants are mocked.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from x402_world import (
    APPROVED,
    LOCAL_FACILITATOR,
    MERCHANT_URL,
    NOW,
    account,
    challenge_doc,
    client,
    endpoint,
    requirement_doc,
    route,
)

from credence.x402.compatibility import MultisigSupport, assess, t54_profile
from credence.x402.discovery import (
    CandidateEndpoint,
    VerificationStatus,
    parse_directory,
    probe,
    reprobe,
    unprobed,
)
from credence.x402.errors import X402Refusal, X402Refused

FIXTURES = Path(__file__).resolve().parents[2] / "contracts" / "fixtures" / "x402_p0"


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield


def load(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="ascii"))  # type: ignore[no-any-return]


def run(routes: dict[tuple[str, str], tuple[int, list[tuple[str, str]], bytes]]):  # type: ignore[no-untyped-def]
    guarded, transport = client(routes)
    return probe(
        endpoint(), guarded, observed_at=NOW, approved=APPROVED, facilitator=LOCAL_FACILITATOR
    ), transport


# --- directory: advertised only ------------------------------------------------------------


def test_directory_listings_are_advertised_and_unverified() -> None:
    listings = parse_directory(
        load("xrpl-ai-directory-excerpt.json"), "https://xrpl-ai.org/directory"
    )
    heurist = next(item for item in listings if "heurist" in item.url)
    assert heurist.claims_inference
    assert heurist.network == "xrpl:0" and heurist.price_amount == "0.0005"
    candidates = unprobed(listings)
    assert all(c.status is VerificationStatus.UNVERIFIED and not c.selectable for c in candidates)
    assert all(c.challenge is None for c in candidates)


def test_directory_category_is_the_directorys_claim_only() -> None:
    listings = parse_directory(load("xrpl-ai-directory-excerpt.json"), "x")
    claiming = [item.name for item in listings if item.claims_inference]
    assert "X Tweets by IDs" in claiming  # a data endpoint the directory files under inference
    assert not next(item for item in listings if "greenhead" in item.url).claims_inference


def test_malformed_directory_is_refused() -> None:
    for doc in (
        {},
        {"services": "x"},
        {"services": [{"id": "a"}]},
        {"services": [{"id": 1, "url": "u"}]},
    ):
        with pytest.raises(X402Refused) as exc:
            parse_directory(doc, "x")
        assert exc.value.code is X402Refusal.MALFORMED_DIRECTORY


# --- probe outcomes ------------------------------------------------------------------------


def test_compatible_challenge_is_selectable() -> None:
    candidate, transport = run({("POST", MERCHANT_URL): route(challenge_doc())})
    assert candidate.status is VerificationStatus.CHALLENGE_COMPATIBLE and candidate.selectable
    assert candidate.advertised_model == "mock/qwen3-30b"
    assert len(transport.sent) == 1
    sent = transport.sent[0]
    assert {name.lower() for name, _ in sent.headers} == {"content-type"}  # nothing else sent


def test_mainnet_challenge_is_observed_but_not_selectable() -> None:
    doc = challenge_doc(requirement_doc(network="xrpl:0"))
    candidate, _ = run({("POST", MERCHANT_URL): route(doc)})
    assert candidate.status is VerificationStatus.CHALLENGE_INCOMPATIBLE
    assert not candidate.selectable
    assert candidate.compatibility[0].read_only
    with pytest.raises(X402Refused) as exc:
        candidate.compatible_requirement()
    assert exc.value.code is X402Refusal.SERVICE_UNVERIFIED


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (404, VerificationStatus.NOT_X402),
        (200, VerificationStatus.NOT_X402),
        (302, VerificationStatus.NOT_X402),
    ],
)
def test_non_402_answers_are_not_x402(status: int, expected: VerificationStatus) -> None:
    candidate, _ = run({("POST", MERCHANT_URL): route(None, status=status, raw_body=b"<html>")})
    assert candidate.status is expected and not candidate.selectable


def test_malformed_402_is_challenge_invalid() -> None:
    candidate, _ = run({("POST", MERCHANT_URL): route(challenge_doc(accepts=[]))})
    assert candidate.status is VerificationStatus.CHALLENGE_INVALID
    assert candidate.refusal is X402Refusal.MALFORMED_CHALLENGE


def test_unsupported_version_is_incompatible() -> None:
    candidate, _ = run({("POST", MERCHANT_URL): route(challenge_doc(x402Version=1))})
    assert candidate.status is VerificationStatus.CHALLENGE_INCOMPATIBLE
    assert candidate.refusal is X402Refusal.UNSUPPORTED_VERSION


def test_unreachable_and_not_allowed_are_distinct() -> None:
    unreachable, _ = run({})  # mock has no route: transport failure
    assert unreachable.status is VerificationStatus.UNREACHABLE
    guarded, transport = client({})
    other = CandidateEndpoint("https://elsewhere.example/v1", "POST")
    not_allowed = probe(
        other, guarded, observed_at=NOW, approved=APPROVED, facilitator=LOCAL_FACILITATOR
    )
    assert not_allowed.status is VerificationStatus.NOT_ALLOWED and transport.sent == []


# --- 10: changed merchant endpoint ---------------------------------------------------------


def selected():  # type: ignore[no-untyped-def]
    candidate, _ = run({("POST", MERCHANT_URL): route(challenge_doc())})
    return candidate.pin()


def test_reprobe_with_same_identity_stays_selectable() -> None:
    pinned = selected()
    guarded, _ = client({("POST", MERCHANT_URL): route(challenge_doc())})
    again = reprobe(
        pinned,
        endpoint(),
        guarded,
        observed_at=NOW,
        approved=APPROVED,
        facilitator=LOCAL_FACILITATOR,
    )
    assert again.selectable


def test_changed_pay_to_is_endpoint_changed() -> None:
    pinned = selected()
    doc = challenge_doc(requirement_doc(payTo=account("new-pay-to")))
    guarded, _ = client({("POST", MERCHANT_URL): route(doc)})
    again = reprobe(
        pinned,
        endpoint(),
        guarded,
        observed_at=NOW,
        approved=APPROVED,
        facilitator=LOCAL_FACILITATOR,
    )
    assert again.status is VerificationStatus.ENDPOINT_CHANGED and not again.selectable
    assert again.refusal_detail == "pay_to"


def test_changed_configured_url_is_endpoint_changed() -> None:
    pinned = selected()
    moved = "https://merchant.example/v2/chat"
    guarded, transport = client({})
    again = reprobe(
        pinned,
        CandidateEndpoint(moved, "POST"),
        guarded,
        observed_at=NOW,
        approved=APPROVED,
        facilitator=LOCAL_FACILITATOR,
    )
    assert again.status is VerificationStatus.ENDPOINT_CHANGED
    assert transport.sent == []


def test_merchant_moving_to_mainnet_is_no_longer_selectable() -> None:
    pinned = selected()
    doc = challenge_doc(requirement_doc(network="xrpl:0"))
    guarded, _ = client({("POST", MERCHANT_URL): route(doc)})
    again = reprobe(
        pinned,
        endpoint(),
        guarded,
        observed_at=NOW,
        approved=APPROVED,
        facilitator=LOCAL_FACILITATOR,
    )
    assert not again.selectable


# --- 11: multisignature and facilitator profile --------------------------------------------


def test_t54_profiles_from_recorded_supported_bodies() -> None:
    mainnet = t54_profile(
        "t54 mainnet",
        "https://xrpl-facilitator-mainnet.t54.ai",
        load("t54-facilitator-mainnet-supported.json")["body"],
    )
    testnet = t54_profile(
        "t54 testnet",
        "https://xrpl-facilitator-testnet.t54.ai",
        load("t54-facilitator-testnet-supported.json")["body"],
    )
    assert mainnet.supports(2, "exact", "xrpl:0") and not mainnet.supports(2, "exact", "xrpl:1")
    assert testnet.supports(2, "exact", "xrpl:1") and testnet.supports(2, "upto", "xrpl:1")
    assert not mainnet.supports(1, "exact", "xrpl:0")
    assert mainnet.multisig is MultisigSupport.UNKNOWN and "UNVERIFIED" in mainnet.multisig_basis


def test_unsupported_multisignature_is_refused() -> None:
    single_sig_only = replace(LOCAL_FACILITATOR, multisig=MultisigSupport.UNSUPPORTED)
    candidate, _ = run({("POST", MERCHANT_URL): route(challenge_doc())})
    assert candidate.challenge is not None
    decision = assess(candidate.challenge.requirement(), APPROVED, single_sig_only)
    assert X402Refusal.MULTISIGNATURE_UNSUPPORTED in decision.reasons
    assert not decision.executable


def test_facilitator_not_serving_the_network_is_recorded() -> None:
    t54 = t54_profile(
        "t54",
        "https://xrpl-facilitator-mainnet.t54.ai",
        load("t54-facilitator-mainnet-supported.json")["body"],
    )
    candidate, _ = run({("POST", MERCHANT_URL): route(challenge_doc())})
    assert candidate.challenge is not None
    decision = assess(candidate.challenge.requirement(), APPROVED, t54)
    assert X402Refusal.FACILITATOR_KIND_UNSUPPORTED in decision.reasons
    assert X402Refusal.MULTISIGNATURE_UNKNOWN in decision.reasons


def test_malformed_supported_body_is_refused() -> None:
    for body in ({}, {"kinds": [{"x402Version": "2", "scheme": "exact", "network": "xrpl:0"}]}):
        with pytest.raises(X402Refused):
            t54_profile("t54", "https://x", body)
