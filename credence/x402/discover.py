"""Operator-run, non-paying x402 discovery: one request per configured endpoint, a JSON report.

    uv run python -m credence.x402.discover \\
        --endpoint POST https://router.heurist.xyz/x402/xrpl/v1/chat/completions '{"prompt":"ping"}' \\
        [--directory directory.json] [--supported supported.json]

Each `--endpoint` origin is the whole allow-list: nothing else is contacted, redirects are never
followed, and no payment, credential or cookie header can be sent (credence.x402.transport). The
probe body is a discovery request and must not contain anything sensitive. The report records each
candidate's observed status, the merchant's advertised terms exactly as given, Credence's approved
execution network and asset, and the compatibility decision. It pays nothing and signs nothing.

The approved execution is configured with --approved-network/--approved-network-id/--approved-
currency/--approved-issuer (the local synthetic tRLUSD on the local standalone network); without
them every real merchant is reported as not policy-evaluable, which is the X402-P0 expectation.

Owner: x402 (X402-P0).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from typing import Any

from xrpl.core.addresscodec import encode_classic_address

from credence.core.primitives import Timestamp
from credence.x402.compatibility import FacilitatorProfile, MultisigSupport, t54_profile
from credence.x402.discovery import CandidateEndpoint, ServiceCandidate, parse_directory, probe
from credence.x402.procurement import ApprovedExecution
from credence.x402.transport import GuardedClient, OriginAllowList, StdlibTransport

# Placeholder approved execution: a configured local network ID and XRPL's account zero (the
# all-zero account ID, which no one controls) as the tRLUSD issuer, so no real merchant matches.
PLACEHOLDER_ISSUER = encode_classic_address(bytes(20))
TRLUSD_WIRE_CODE = b"tRLUSD".hex().upper().ljust(40, "0")


def report(candidate: ServiceCandidate) -> dict[str, Any]:
    out: dict[str, Any] = {
        "url": candidate.endpoint.url,
        "method": candidate.endpoint.method,
        "status": candidate.status.value,
        "selectable": candidate.selectable,
        "observed_at": candidate.observed_at.value if candidate.observed_at else None,
        "http_status": candidate.http_status,
        "refusal": candidate.refusal.value if candidate.refusal else None,
        "refusal_detail": candidate.refusal_detail,
        "listing": None,
        "challenge": None,
    }
    if candidate.listing is not None:
        out["listing"] = {"advertised": True, **vars(candidate.listing)}
    challenge = candidate.challenge
    if challenge is not None:
        out["challenge"] = {
            "version": challenge.version,
            "carried_in": challenge.observed.carried_in,
            "resource_url": challenge.resource_url,
            "advertised_model": candidate.advertised_model,
            "requirements": [
                {
                    "advertised": dict(req.raw),
                    "asset_label": req.asset_label,
                    "decision": {
                        "observed_network": d.observed_network,
                        "observed_asset": d.observed_asset_label,
                        "approved_network": d.approved_network,
                        "approved_asset": d.approved_asset_label,
                        "policy_evaluable": d.policy_evaluable,
                        "read_only": d.read_only,
                        "executable": d.executable,
                        "reasons": [r.value for r in d.reasons],
                    },
                }
                for req, d in zip(challenge.requirements, candidate.compatibility, strict=True)
            ],
            "ignored": [[i, code.value] for i, code in challenge.ignored],
        }
    if candidate.incompatible is not None:
        out["incompatible"] = {
            "code": candidate.incompatible.code.value,
            "detail": candidate.incompatible.detail,
        }
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--endpoint", nargs=3, action="append", metavar=("METHOD", "URL", "BODY"), default=[]
    )
    parser.add_argument("--directory", help="a saved directory document (JSON) to match listings")
    parser.add_argument("--supported", help="a saved facilitator /supported body (JSON)")
    parser.add_argument("--approved-network", default="xrpl:63456")
    parser.add_argument("--approved-network-id", type=int, default=63456)
    parser.add_argument("--approved-currency", default=TRLUSD_WIRE_CODE)
    parser.add_argument("--approved-issuer", default=PLACEHOLDER_ISSUER)
    args = parser.parse_args(argv)

    approved = ApprovedExecution(
        args.approved_network,
        args.approved_network_id,
        args.approved_currency,
        args.approved_issuer,
    )
    facilitator: FacilitatorProfile
    if args.supported:
        with open(args.supported, encoding="utf-8") as fh:
            facilitator = t54_profile("configured facilitator", "", json.load(fh))
    else:
        facilitator = FacilitatorProfile("none configured", "", (), MultisigSupport.UNKNOWN, "")
    listings = {}
    if args.directory:
        with open(args.directory, encoding="utf-8") as fh:
            listings = {item.url: item for item in parse_directory(json.load(fh), args.directory)}

    endpoints = [
        CandidateEndpoint(
            url, method.upper(), body.encode("utf-8"), "application/json" if body else None
        )
        for method, url, body in args.endpoint
    ]
    allow = OriginAllowList.of(*(e.url for e in endpoints))
    client = GuardedClient(StdlibTransport(), allow)
    results = []
    for endpoint in endpoints:
        now = Timestamp(datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
        candidate = probe(
            endpoint,
            client,
            observed_at=now,
            approved=approved,
            facilitator=facilitator,
            listing=listings.get(endpoint.url),
        )
        results.append(report(candidate))
    json.dump({"candidates": results}, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
