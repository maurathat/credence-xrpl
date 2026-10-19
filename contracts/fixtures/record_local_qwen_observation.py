"""Record one real local Qwen3 30B A3B Instruct 2507 (Q8_0) Tier 2 purchase as runtime evidence.

Run from the repository root (local only, no credential, no remote network):

    CREDENCE_LOCAL_QWEN=1 uv run python contracts/fixtures/record_local_qwen_observation.py

RUNTIME OBSERVATION, not deterministically regenerable (wall-clock timestamps, timings, a provider
key generated for the run). tests/conformance/test_inference_c3_local_qwen_observation.py
re-verifies its digests, signatures and verifier decision through Core alone.

Written: artifact provenance by content address, GGUF metadata as bound, the bound profile, the
runtime-reported chat-template digest (observation only), canonical objects, public key IDs,
signatures, counts, timings and the synthetic prompt. Not written: weights, private keys, response
text, machine paths.
"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent / "inference_c3" / "local-qwen-tier2-observation.json"
sys.path.insert(0, str(ROOT / "tests" / "integration"))
sys.path.insert(0, str(ROOT / "tests" / "unit"))

from inference_world import AGENT_KEY, BORROWER, GATEWAY_KEY, GATEWAY_SUBJECT, make_purchase  # noqa: E402
from local_qwen_world import ENABLE_ENV, PROMPT, TAG, build_qwen_world, enabled  # noqa: E402

from credence.core.objects import CanonicalObject  # noqa: E402
from credence.core.signatures import key_id_for  # noqa: E402
from credence.inference.gateway import DeliveryVerified  # noqa: E402
from credence.inference.local_llama import (  # noqa: E402
    LOCAL_QWEN_PROVIDER_ID,
    OLLAMA_REGISTRY,
    default_models_dir,
    sha256_file,
)


def _c(obj: CanonicalObject | None) -> Any:
    return None if obj is None else obj.to_canonical()


def _sysctl(name: str) -> str:
    return subprocess.run(["sysctl", "-n", name], capture_output=True, text=True).stdout.strip()


def main() -> None:
    if not enabled():
        raise SystemExit(f"set {ENABLE_ENV}=1 to run real local inference")
    world = build_qwen_world()
    bound, installed = world.bound, world.bound.installed
    manifest = json.loads(installed.manifest_path.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as tmp:
        gateway = world.gateway(Path(tmp))
        purchase = make_purchase(world.scenario, request_prompt=PROMPT)
        outcome = gateway.purchase(purchase)
        timing = dict(world.provider.last_timing)
        repeat = gateway.purchase(make_purchase(world.scenario, 2, request_prompt=PROMPT))
    assert isinstance(outcome, DeliveryVerified), outcome
    assert isinstance(repeat, DeliveryVerified), repeat
    evidence = outcome.evidence
    record = {
        "id": "local-qwen-tier2-observation",
        "kind": "runtime-observation",
        "regenerable": False,
        "description": (
            "One real Tier 2 purchase: local Qwen3 30B A3B Instruct 2507 (Ollama library Q8_0 "
            "GGUF) through the real gateway and Core engines. Artifact binding is not execution "
            "measurement: no GPU-memory, TEE or Tier 3 claim."
        ),
        "environment": {
            "machine": platform.machine(),
            "os": f"macOS {platform.mac_ver()[0]}",
            "chip": _sysctl("machdep.cpu.brand_string"),
            "memory_bytes": int(_sysctl("hw.memsize")),
            "runtime": bound.profile.runtime,
        },
        "artifact": {
            "upstream_model_family": "Qwen3 30B A3B Instruct 2507 (Qwen)",
            "local_execution_artifact": "Ollama library GGUF (Q8_0); not Qwen's original files",
            "source": f"{OLLAMA_REGISTRY}/library/{TAG}",
            "manifest_sha256": sha256_file(installed.manifest_path).value,
            "layers": [
                {"media_type": layer["mediaType"], "digest": layer["digest"], "size": layer["size"]}
                for layer in manifest["layers"]
            ],
            "model_file_bytes": installed.model_size,
            "model_file_sha256": installed.model_digest.value,
            "gguf_general_file_type": bound.file_type,
            "tensor_types": sorted(bound.tensor_types),
            "template_file_sha256": None
            if installed.template_digest is None
            else installed.template_digest.value,
            "runtime_reported_template_sha256": None
            if bound.runtime_template_sha256 is None
            else bound.runtime_template_sha256.value,
            "chat_template_binding": "NOT ASSERTED: the runtime renders with the template embedded "
            "in the GGUF, not the manifest's template file (observation only)",
            "models_dir_is_outside_repository": ROOT not in default_models_dir().parents,
        },
        "service_location_provenance": "operator-supplied deployment fact: the execution machine "
        "physically operates in the United States (jurisdiction US, residency {US})",
        "sampling": {
            "temperature_milli": bound.sampling.temperature_milli,
            "seed": bound.sampling.seed,
            "context_tokens": bound.sampling.context_tokens,
            "max_output_tokens": world.scenario.quote.max_output_tokens,
        },
        "registry": sorted(
            (
                {
                    "key_id": key_id_for(k.verify_key).value,
                    "role": r,
                    "subject": s,
                    "status": "current",
                }
                for k, r, s in (
                    (AGENT_KEY, "agent", BORROWER.value),
                    (GATEWAY_KEY, "gateway", GATEWAY_SUBJECT),
                    (world.provider_key, "provider", LOCAL_QWEN_PROVIDER_ID.value),
                )
            ),
            key=lambda e: e["key_id"],
        ),
        "provider_key_id": key_id_for(world.provider_key.verify_key).value,
        "provider_key_origin": "generated at runtime for this run; never written",
        "prompt_text": PROMPT.decode("utf-8"),
        "response_text": None,
        "objects": {
            "facility": _c(world.scenario.facility),
            "procurement_class": _c(world.scenario.procurement),
            "service_class": _c(world.scenario.service_class),
            "execution_profile": _c(world.scenario.profile),
            "quote": _c(world.scenario.quote),
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
        "runtime_timing_ns": timing,
        "observed": {
            "outcome": "DeliveryVerified",
            "receipt_digest": evidence.receipt.digest().value,
            "provider_attestation_digest": None
            if evidence.provider_attestation is None
            else evidence.provider_attestation.digest().value,
            "repeat_run_same_response_digest": repeat.evidence.receipt.response_digest
            == evidence.receipt.response_digest,
            "repeat_run_same_token_counts": (
                repeat.evidence.receipt.input_token_count,
                repeat.evidence.receipt.output_token_count,
            )
            == (evidence.receipt.input_token_count, evidence.receipt.output_token_count),
        },
    }
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(record, indent=2, ensure_ascii=True) + "\n", encoding="ascii")
    print(f"wrote {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
