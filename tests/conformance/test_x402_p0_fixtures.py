"""X402-P0 conformance: recorded t54 observations and the deterministic X402-P0 fixture.

Recorded evidence (contracts/fixtures/x402_p0/) is checked against the adapter:
- both observed merchant 402s parse as x402 v2 `exact` on `xrpl:0`, with header and body equal;
- their RLUSD terms are recorded exactly and assessed read-only, never executable;
- Credence's invoice-binding derivation reproduces the InvoiceID and Memo of two validated Mainnet
  Payments settled through t54's facilitator;
- the deterministic fixture regenerates byte-for-byte, pinning every recorded file's sha256.
No network.
"""

from __future__ import annotations

import importlib.util
import json
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from credence.core.primitives import Timestamp
from credence.x402.challenge import X402Challenge, parse_challenge
from credence.x402.proposal import InvoiceBinding
from credence.x402.transport import HttpRequest, filter_response

ROOT = Path(__file__).resolve().parents[2]
DIR = ROOT / "contracts" / "fixtures" / "x402_p0"
RLUSD = "524C555344000000000000000000000000000000"
RLUSD_MAINNET_ISSUER = "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De"


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield


def load(name: str) -> dict[str, Any]:
    return json.loads((DIR / name).read_text(encoding="ascii"))  # type: ignore[no-any-return]


def observed(name: str) -> X402Challenge:
    doc = load(name)
    req, resp = doc["request"], doc["response"]
    headers = (("content-type", req["content_type"]),) if req["content_type"] else ()
    request = HttpRequest(req["method"], req["url"], headers, req["body"].encode("utf-8"))
    response = filter_response(
        resp["status"], [tuple(h) for h in resp["headers"]], resp["body"].encode("utf-8"), False
    )
    challenge = parse_challenge(request, response, Timestamp(doc["observed_at"]))
    assert isinstance(challenge, X402Challenge)
    return challenge


def test_heurist_observed_challenge() -> None:
    challenge = observed("observed-heurist-402.json")
    assert challenge.version == 2 and challenge.observed.carried_in == "header+body"
    assert challenge.resource_url == "https://router.heurist.xyz/x402/xrpl/v1/chat/completions"
    req = challenge.requirement()
    assert (req.scheme, req.network, req.amount.value) == ("exact", "xrpl:0", "0.0005")
    assert (req.amount.currency, req.amount.issuer) == (RLUSD, RLUSD_MAINNET_ISSUER)
    assert req.asset_label == "RLUSD"
    assert req.source_tag is None  # Heurist omits extra.sourceTag (t54 docs say it is required)
    assert req.invoice_id == "01A170E6C1CD7C6583F4A1A20CE2DAC3"
    assert req.advertised_extra["execution"]["model"] == "heurist/glm-5.3"
    assert req.advertised_extra["discoveryProbe"] is True  # the price is for a probe body


def test_monid_observed_challenge() -> None:
    req = observed("observed-monid-402.json").requirement()
    assert (req.network, req.amount.value, req.source_tag) == ("xrpl:0", "0.001", 804681468)
    assert (req.amount.currency, req.amount.issuer) == (RLUSD, RLUSD_MAINNET_ISSUER)


@pytest.mark.parametrize("tx", load("t54-invoice-binding-observed.json")["transactions"])
def test_invoice_binding_matches_validated_mainnet_payments(tx: dict[str, Any]) -> None:
    assert tx["validated"] is True and tx["engine_result"] == "tesSUCCESS"
    binding = InvoiceBinding(tx["invoice_id"], tx["source_tag"], None)
    assert binding.invoice_id_field == tx["invoice_id_field"]
    assert binding.memo_data == tx["memo_0_memo_data"]
    assert tx["send_max"] == tx["amount"]  # issued-currency SendMax equal to Amount, as proposed
    assert tx["has_network_id"] is False  # Mainnet refuses NetworkID


def test_deterministic_fixture_regenerates_byte_for_byte() -> None:
    spec = importlib.util.spec_from_file_location(
        "build_x402_p0_fixtures", ROOT / "contracts" / "fixtures" / "build_x402_p0_fixtures.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.build() == module.OUT.read_text(encoding="ascii")


def test_observed_terms_are_read_only_in_the_fixture() -> None:
    doc = load("x402-p0-deterministic.json")
    for item in doc["observed_assessments"]:
        for req in item["requirements"]:
            assert req["advertised"]["asset_label"] == "RLUSD"
            assert req["approved_execution"]["asset_label"] == "tRLUSD"
            assert req["decision"]["read_only"] is True
            assert req["decision"]["executable"] is False
            assert req["decision"]["policy_evaluable"] is False
    assert doc["mock_proposal"]["submission_authorized"] is False
