"""X402-P0: the operator discovery entry point, offline (its transport replaced by the mock)."""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator

import pytest
from x402_world import APPROVED, MERCHANT_URL, TRLUSD_ISSUER, TRLUSD_WIRE_CODE, challenge_doc, route

from credence.x402 import discover
from credence.x402.mock import MockTransport


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield


def run(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *extra: str) -> dict:  # type: ignore[type-arg]
    transport = MockTransport({("POST", MERCHANT_URL): route(challenge_doc())})
    monkeypatch.setattr(discover, "StdlibTransport", lambda: transport)
    assert discover.main(["--endpoint", "POST", MERCHANT_URL, '{"prompt":"ping"}', *extra]) == 0
    assert len(transport.sent) == 1
    return json.loads(capsys.readouterr().out)  # type: ignore[no-any-return]


def test_default_approved_execution_matches_no_merchant(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (candidate,) = run(monkeypatch, capsys)["candidates"]
    assert candidate["status"] == "challenge_incompatible" and not candidate["selectable"]
    reasons = candidate["challenge"]["requirements"][0]["decision"]["reasons"]
    assert "ISSUER_NOT_APPROVED" in reasons and "SUBMISSION_NOT_AUTHORIZED" in reasons


def test_configured_approved_execution(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    args = (
        "--approved-network", APPROVED.network,
        "--approved-network-id", str(APPROVED.network_id),
        "--approved-currency", TRLUSD_WIRE_CODE,
        "--approved-issuer", TRLUSD_ISSUER,
    )  # fmt: skip
    (candidate,) = run(monkeypatch, capsys, *args)["candidates"]
    assert candidate["status"] == "challenge_compatible" and candidate["selectable"]
    decision = candidate["challenge"]["requirements"][0]["decision"]
    assert decision["policy_evaluable"] and not decision["executable"]
