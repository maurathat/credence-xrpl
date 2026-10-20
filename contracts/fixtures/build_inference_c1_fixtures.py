"""Generate the Inference Checkpoint 1 contract fixtures in contracts/fixtures/inference_c1/.

Run from the repository root:  uv run python contracts/fixtures/build_inference_c1_fixtures.py

Each fixture is one complete purchase through the real gateway and the real Core engines, using
the synthetic world in tests/unit/inference_world.py: synthetic prompts, the deterministic fake
providers, a fixed step clock and TEST keys (NOT FOR PRODUCTION) derived from public labels. Only
public key IDs and signatures are written, never key material. Ed25519 is deterministic, so
regeneration is byte-identical; tests/conformance/test_inference_c1_fixtures.py checks that and
re-verifies every fixture through Core.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent / "inference_c1"
sys.path.insert(0, str(ROOT / "tests" / "unit"))

from inference_world import (  # noqa: E402
    REGISTERED_KEYS,
    TIER1,
    TIER2,
    Scenario,
    make_gateway,
    make_purchase,
    tier1_provider,
    tier2_provider,
)

from credence.core.objects import CanonicalObject  # noqa: E402
from credence.inference.gateway import DeliveryVerified  # noqa: E402
from credence.inference.provider import InferenceProvider  # noqa: E402


def _registry_entries() -> list[dict[str, str]]:
    return [
        {
            "key_id": key.key_id.value,
            "role": key.role.value,
            "subject": key.subject,
            "status": key.status.value,
        }
        for key in sorted(REGISTERED_KEYS, key=lambda k: k.key_id.value)
    ]


def _c(obj: CanonicalObject | None) -> Any:
    return None if obj is None else obj.to_canonical()


def _delivery(
    name: str, description: str, scenario: Scenario, provider: InferenceProvider
) -> dict[str, Any]:
    purchase = make_purchase(scenario)
    with tempfile.TemporaryDirectory() as tmp:
        outcome = make_gateway(Path(tmp), provider).purchase(purchase)
    assert isinstance(outcome, DeliveryVerified), outcome
    evidence = outcome.evidence
    return {
        "id": name,
        "description": description,
        "synthetic": True,
        "registry": _registry_entries(),
        "prompt_text": purchase.prompt.decode("utf-8"),
        "response_text": outcome.response.decode("utf-8"),
        "objects": {
            "facility": _c(scenario.facility),
            "procurement_class": _c(scenario.procurement),
            "service_class": _c(scenario.service_class),
            "execution_profile": _c(scenario.profile),
            "quote": _c(scenario.quote),
            "purchase_request": _c(purchase.request),
            "purchase_request_signature": _c(purchase.request_signature),
            "provider_attestation": _c(evidence.provider_attestation),
            "provider_attestation_signature": _c(evidence.provider_signature),
            "delivery_receipt": _c(evidence.receipt),
            "gateway_signature": _c(evidence.gateway_signature),
            "reservation": _c(outcome.reservation),
            "policy_decision": _c(outcome.policy_decision.record),
            "verifier_decision": _c(outcome.decision.record),
        },
        "token_count_source": evidence.token_count_source.value,
        "expected": {
            "outcome": "DeliveryVerified",
            "prompt_digest": purchase.request.prompt_digest.value,
            "response_digest": evidence.receipt.response_digest.value,
            "receipt_digest": evidence.receipt.digest().value,
            "evidence_tier": evidence.receipt.evidence_tier.value,
        },
    }


def render() -> dict[str, str]:
    fixtures = [
        _delivery(
            "tier1-witnessed-delivery",
            "Tier 1: general class, synthetic witnessed provider, gateway-signed receipt",
            TIER1,
            tier1_provider(),
        ),
        _delivery(
            "tier2-bound-and-signed-delivery",
            "Tier 2: controlled class, synthetic signing provider, provider-signed attestation "
            "and gateway-signed receipt",
            TIER2,
            tier2_provider(),
        ),
    ]
    files = {f"{f['id']}.json": json.dumps(f, indent=2, ensure_ascii=True) + "\n" for f in fixtures}
    manifest = [
        [hashlib.sha256(text.encode("ascii")).hexdigest(), name]
        for name, text in sorted(files.items())
    ]
    files["MANIFEST.json"] = json.dumps(manifest, indent=1) + "\n"
    return files


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for name, text in render().items():
        (OUT / name).write_text(text, encoding="ascii")


if __name__ == "__main__":
    main()
