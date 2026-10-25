"""Deterministic offline world for X402-P0 tests: no network, no keys that sign payments, no clock.

Accounts are derived from labelled hashes, not generated keys. The approved execution asset is the
local synthetic tRLUSD (a test token) on a configured local standalone network ID; this is test
configuration, not a claim about any deployed ledger. Mainnet and Testnet terms appear only as
observed merchant data, to be displayed and refused for execution.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from xrpl.core.addresscodec import encode_classic_address

from credence.core.primitives import EvidenceTier, Timestamp
from credence.x402.compatibility import FacilitatorKind, FacilitatorProfile, MultisigSupport
from credence.x402.discovery import CandidateEndpoint
from credence.x402.mock import MockTransport
from credence.x402.procurement import ApprovedExecution
from credence.x402.proposal import (
    InvoiceBinding,
    LedgerContext,
    PaymentAuthorizationProposal,
    PurchaseIntent,
    XrplAmount,
    build_proposal,
    digest_json,
)
from credence.x402.transport import GuardedClient, HttpRequest, OriginAllowList


def account(label: str) -> str:
    return encode_classic_address(
        hashlib.sha256(b"credence-x402-p0:" + label.encode()).digest()[:20]
    )


def digest(label: str) -> str:
    return digest_json({"label": label})


NOW = Timestamp("2026-10-24T15:00:00Z")
LATER = Timestamp("2026-10-24T15:01:00Z")
EXPIRES = Timestamp("2026-10-24T15:05:00Z")

TRLUSD_WIRE_CODE = b"tRLUSD".hex().upper().ljust(40, "0")
TRLUSD_ISSUER = account("trlusd-issuer")
LOCAL_NETWORK_ID = 63_456  # configured standalone network ID for tests (> 1024)
LOCAL_NETWORK = f"xrpl:{LOCAL_NETWORK_ID}"

APPROVED = ApprovedExecution(
    network=LOCAL_NETWORK,
    network_id=LOCAL_NETWORK_ID,
    currency=TRLUSD_WIRE_CODE,
    issuer=TRLUSD_ISSUER,
)

AGENT_PAYMENT_ACCOUNT = account("researchbot-payment-account")
MERCHANT_PAY_TO = account("mock-merchant-pay-to")
OTHER_ACCOUNT = account("someone-else")

LEDGER = LedgerContext(
    source_account=AGENT_PAYMENT_ACCOUNT,
    network_id=LOCAL_NETWORK_ID,
    fee_drops=36,
    sequence=7,
    last_ledger_sequence=120,
)


def intent(agent_id: str = "agent-researchbot", label: str = "job-1") -> PurchaseIntent:
    return PurchaseIntent(
        agent_id=agent_id,
        facility_id="facility-x402",
        purchase_request_digest=digest(f"purchase-request:{agent_id}:{label}"),
        prompt_digest=digest(f"prompt:{label}"),
    )


def proposal(
    agent_id: str = "agent-researchbot",
    *,
    label: str = "job-1",
    invoice_id: str | None = "A" * 64,
    value: str = "0.25",
    tier: EvidenceTier = EvidenceTier.WITNESSED,
) -> PaymentAuthorizationProposal:
    return build_proposal(
        intent=intent(agent_id, label),
        merchant_id="mock-merchant",
        resource_url="https://merchant.example/v1/chat",
        request_method="POST",
        request_digest=digest(f"request:{label}"),
        requirement_digest=digest(f"requirement:{label}"),
        invoice=InvoiceBinding(invoice_id=invoice_id, source_tag=None, destination_tag=None),
        amount=XrplAmount(TRLUSD_WIRE_CODE, TRLUSD_ISSUER, value),
        destination=MERCHANT_PAY_TO,
        network=LOCAL_NETWORK,
        expires_at=EXPIRES,
        ledger=LEDGER,
        required_evidence_tier=tier,
    )


# --- mock merchant and facilitator ---------------------------------------------------------

MERCHANT_URL = "https://merchant.example/v1/chat"
PROBE_BODY = b'{"prompt":"ping"}'
INVOICE = "0123456789ABCDEF0123456789ABCDEF"

LOCAL_FACILITATOR = FacilitatorProfile(
    name="mock-local-facilitator",
    base_url="https://facilitator.example",
    kinds=(FacilitatorKind(2, "exact", LOCAL_NETWORK),),
    multisig=MultisigSupport.SUPPORTED,
    multisig_basis="test configuration",
)


def requirement_doc(**changes: object) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "scheme": "exact",
        "network": LOCAL_NETWORK,
        "amount": "0.0005",
        "asset": TRLUSD_WIRE_CODE,
        "payTo": MERCHANT_PAY_TO,
        "maxTimeoutSeconds": 600,
        "extra": {
            "issuer": TRLUSD_ISSUER,
            "invoiceId": INVOICE,
            "execution": {"model": "mock/qwen3-30b"},
        },
    }
    entry.update(changes)
    return entry


def challenge_doc(*accepts: dict[str, Any], **changes: object) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "x402Version": 2,
        "resource": {"url": MERCHANT_URL, "description": "mock", "mimeType": "application/json"},
        "accepts": list(accepts) or [requirement_doc()],
        "error": "PAYMENT-SIGNATURE header is required",
        "extensions": {},
    }
    doc.update(changes)
    return doc


def b64(doc: object) -> str:
    return base64.b64encode(
        json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
    ).decode()


def route(
    doc: object | None,
    *,
    status: int = 402,
    header: bool = True,
    body: bool = True,
    extra_headers: list[tuple[str, str]] | None = None,
    raw_body: bytes | None = None,
) -> tuple[int, list[tuple[str, str]], bytes]:
    headers = [("content-type", "application/json"), ("set-cookie", "session=secret")]
    if header and doc is not None:
        headers.append(("PAYMENT-REQUIRED", b64(doc)))
    headers += extra_headers or []
    content = raw_body if raw_body is not None else (json.dumps(doc).encode() if body else b"")
    return status, headers, content


def probe_request(url: str = MERCHANT_URL) -> HttpRequest:
    return HttpRequest("POST", url, (("content-type", "application/json"),), PROBE_BODY)


def client(
    routes: dict[tuple[str, str], tuple[int, list[tuple[str, str]], bytes]],
) -> tuple[GuardedClient, MockTransport]:
    transport = MockTransport(routes)
    return GuardedClient(transport, OriginAllowList.of(MERCHANT_URL)), transport


def endpoint(url: str = MERCHANT_URL) -> CandidateEndpoint:
    return CandidateEndpoint(url, "POST", PROBE_BODY, "application/json")
