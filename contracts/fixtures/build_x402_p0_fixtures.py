"""Generate the deterministic X402-P0 fixture (no network, no keys, no payment).

Run from the repository root:  uv run python contracts/fixtures/build_x402_p0_fixtures.py

DETERMINISTIC PROTOCOL FIXTURE. It records:

- `recorded`: the sha256 of every recorded observation file in x402_p0/ (directory excerpt, the two
  observed merchant 402s, the two t54 `/supported` bodies, the ledger invoice-binding vectors), so a
  silent edit of recorded evidence fails the conformance test;
- `observed_assessments`: what the adapter makes of each observed merchant 402: parsed terms
  (merchant-advertised, recorded exactly: RLUSD on XRPL Mainnet stays RLUSD on `xrpl:0`) beside
  Credence's approved execution (local synthetic tRLUSD, a test token), and the decision;
- `mock_proposal`: the unsigned proposal for the deterministic mock merchant on the configured
  local standalone network (tests/unit/x402_world.py). Not signed, not submittable.

tests/conformance/test_x402_p0_fixtures.py regenerates it byte-for-byte.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DIR = Path(__file__).resolve().parent / "x402_p0"
OUT = DIR / "x402-p0-deterministic.json"
RECORDED = (
    "observed-heurist-402.json",
    "observed-monid-402.json",
    "t54-facilitator-mainnet-supported.json",
    "t54-facilitator-testnet-supported.json",
    "t54-invoice-binding-observed.json",
    "xrpl-ai-directory-excerpt.json",
)
sys.path.insert(0, str(ROOT / "tests" / "unit"))

from x402_world import APPROVED, LEDGER, NOW, challenge_doc, intent, probe_request, route  # noqa: E402

from credence.core.primitives import EvidenceTier, Timestamp  # noqa: E402
from credence.x402.challenge import X402Challenge, parse_challenge  # noqa: E402
from credence.x402.compatibility import assess, t54_profile  # noqa: E402
from credence.x402.proposal import InvoiceBinding, build_proposal  # noqa: E402
from credence.x402.transport import HttpRequest, filter_response  # noqa: E402


def load(name: str) -> dict[str, Any]:
    return json.loads((DIR / name).read_text(encoding="ascii"))  # type: ignore[no-any-return]


def observed(name: str) -> tuple[HttpRequest, X402Challenge]:
    doc = load(name)
    req, resp = doc["request"], doc["response"]
    headers = (("content-type", req["content_type"]),) if req["content_type"] else ()
    request = HttpRequest(req["method"], req["url"], headers, req["body"].encode("utf-8"))
    response = filter_response(
        resp["status"], [tuple(h) for h in resp["headers"]], resp["body"].encode("utf-8"), False
    )
    challenge = parse_challenge(request, response, Timestamp(doc["observed_at"]))
    assert isinstance(challenge, X402Challenge)
    return request, challenge


def assessment(name: str) -> dict[str, Any]:
    _, challenge = observed(name)
    t54 = t54_profile(
        "t54 hosted facilitator (mainnet)",
        "https://xrpl-facilitator-mainnet.t54.ai",
        load("t54-facilitator-mainnet-supported.json")["body"],
    )
    out = []
    for req in challenge.requirements:
        decision = assess(req, APPROVED, t54, version=challenge.version)
        out.append(
            {
                "advertised": {
                    "scheme": req.scheme,
                    "network": req.network,
                    "amount": req.amount.value,
                    "asset": req.amount.currency,
                    "asset_label": req.asset_label,
                    "issuer": req.amount.issuer,
                    "pay_to": req.pay_to,
                    "max_timeout_seconds": req.max_timeout_seconds,
                    "invoice_id": req.invoice_id,
                    "source_tag": req.source_tag,
                    "advertised_extra_keys": sorted(req.advertised_extra),
                },
                "approved_execution": {
                    "network": decision.approved_network,
                    "asset": decision.approved_currency,
                    "asset_label": decision.approved_asset_label,
                    "issuer": decision.approved_issuer,
                },
                "decision": {
                    "policy_evaluable": decision.policy_evaluable,
                    "read_only": decision.read_only,
                    "executable": decision.executable,
                    "reasons": [r.value for r in decision.reasons],
                },
            }
        )
    return {
        "fixture": name,
        "version": challenge.version,
        "resource_url": challenge.resource_url,
        "carried_in": challenge.observed.carried_in,
        "request_digest": challenge.observed.request_digest,
        "requirements": out,
    }


def mock_proposal() -> dict[str, Any]:
    status, headers, body = route(challenge_doc())
    request = probe_request()
    challenge = parse_challenge(request, filter_response(status, headers, body, False), NOW)
    assert isinstance(challenge, X402Challenge)
    req = challenge.requirement()
    p = build_proposal(
        intent=intent(),
        merchant_id="x402-mock-merchant",
        resource_url=challenge.resource_url,
        request_method=request.method,
        request_digest=challenge.observed.request_digest,
        requirement_digest=req.requirement_digest,
        invoice=InvoiceBinding(req.invoice_id, req.source_tag, req.destination_tag),
        amount=req.amount,
        destination=req.pay_to,
        network=req.network,
        expires_at=req.expires_at(NOW),
        ledger=LEDGER,
        required_evidence_tier=EvidenceTier.WITNESSED,
    )
    return {
        "proposal_id": p.proposal_id,
        "idempotency_key": p.idempotency_key,
        "request_digest": p.request_digest,
        "requirement_digest": p.requirement_digest,
        "unsigned_transaction": p.transaction(),
        "unsigned_transaction_digest": p.unsigned_transaction_digest,
        "unsigned_transaction_digest_scheme": p.unsigned_transaction_digest_scheme,
        "x1_template_gaps": list(p.x1_template_gaps),
        "submission_authorized": p.submission_authorized,
    }


def build() -> str:
    doc = {
        "fixture": "credence.x402.p0.deterministic",
        "note": "Generated by contracts/fixtures/build_x402_p0_fixtures.py. No network, no keys, "
        "no payment. Merchant terms are third-party data recorded exactly as observed.",
        "recorded": {
            name: hashlib.sha256((DIR / name).read_bytes()).hexdigest() for name in RECORDED
        },
        "observed_assessments": [
            assessment("observed-heurist-402.json"),
            assessment("observed-monid-402.json"),
        ],
        "mock_proposal": mock_proposal(),
    }
    return json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


if __name__ == "__main__":
    OUT.write_text(build(), encoding="ascii")
    print(f"wrote {OUT.relative_to(ROOT)}")
