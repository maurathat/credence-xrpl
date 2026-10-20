"""Record one real local Llama 3.1 8B Instruct Tier 2 purchase as runtime-observation evidence.

Run from the repository root (needs Ollama on loopback with llama3.1:8b-instruct-fp16):

    CREDENCE_LOCAL_LLAMA=1 uv run python contracts/fixtures/record_local_llama_observation.py

This is RUNTIME OBSERVATION EVIDENCE, not a deterministic fixture: it cannot be regenerated
byte-for-byte (wall-clock timestamps, timings, and a provider key generated fresh for the run).
What is fixed is checked instead: tests/conformance/test_inference_c2_observation.py re-verifies
every digest, signature and the verifier decision in the committed file through Core alone.

Written: public key IDs, signatures, canonical objects (digests only for prompt and response),
token counts, timings, artifact provenance by content address, and the synthetic prompt text.
Not written: model weights, any private key, the response text, machine-specific paths.
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
OUT = Path(__file__).resolve().parent / "inference_c2" / "local-llama-tier2-observation.json"
sys.path.insert(0, str(ROOT / "tests" / "integration"))
sys.path.insert(0, str(ROOT / "tests" / "unit"))

from inference_world import make_purchase  # noqa: E402
from local_llama_world import (  # noqa: E402
    ENABLE_ENV,
    PROMPT,
    TAG,
    build_local_world,
    enabled,
)

from credence.core.objects import CanonicalObject  # noqa: E402
from credence.core.signatures import key_id_for  # noqa: E402
from credence.inference.gateway import DeliveryVerified  # noqa: E402
from credence.inference.local_llama import (  # noqa: E402
    OLLAMA_REGISTRY,
    default_models_dir,
    sha256_file,
)


def _c(obj: CanonicalObject | None) -> Any:
    return None if obj is None else obj.to_canonical()


def _sysctl(name: str) -> str:
    return subprocess.run(["sysctl", "-n", name], capture_output=True, text=True).stdout.strip()


def _runtime_process(model_digest_hex: str) -> dict[str, Any]:
    """Observe (not measure) which file the runtime's llama.cpp server process was started with,
    from the process listing. Records only whether it is the bound blob, never the path."""
    listing = subprocess.run(["ps", "-axo", "args"], capture_output=True, text=True).stdout
    servers = [
        line for line in listing.splitlines() if "llama-server" in line and "--model" in line
    ]
    binary = next((line.split()[0] for line in servers if model_digest_hex in line), None)
    version = (
        subprocess.run([binary, "--version"], capture_output=True, text=True).stderr.splitlines()[0]
        if binary
        else None
    )
    return {
        "server_started_with_bound_model_file": binary is not None,
        "llama_server_version": version,
        "how_observed": "process listing after the run; an observation, not a measurement",
    }


def main() -> None:
    if not enabled():
        raise SystemExit(f"set {ENABLE_ENV}=1 to run real local inference")
    world = build_local_world()
    bound = world.bound
    installed = bound.installed
    manifest = json.loads(installed.manifest_path.read_text(encoding="utf-8"))

    with tempfile.TemporaryDirectory() as tmp:
        gateway = world.gateway(Path(tmp))
        purchase = make_purchase(world.scenario, request_prompt=PROMPT)
        outcome = gateway.purchase(purchase)
        timing = dict(world.provider.last_timing)
        # The same prompt again under a new nonce, to observe (not assert) determinism.
        repeat = gateway.purchase(make_purchase(world.scenario, 2, request_prompt=PROMPT))
    assert isinstance(outcome, DeliveryVerified), outcome
    assert isinstance(repeat, DeliveryVerified), repeat
    evidence = outcome.evidence

    record = {
        "id": "local-llama-tier2-observation",
        "kind": "runtime-observation",
        "regenerable": False,
        "description": (
            "One real Tier 2 purchase: local Llama 3.1 8B Instruct (Ollama library F16 GGUF) "
            "through the real gateway and Core engines. Artifact binding is not execution "
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
            "upstream_model_family": "Llama 3.1 8B Instruct (Meta)",
            "local_execution_artifact": "Ollama library GGUF conversion; not Meta's original files",
            "source": f"{OLLAMA_REGISTRY}/library/{TAG}",
            "manifest_sha256": sha256_file(installed.manifest_path).value,
            "layers": [
                {"media_type": layer["mediaType"], "digest": layer["digest"], "size": layer["size"]}
                for layer in manifest["layers"]
            ],
            "model_file_bytes": installed.model_size,
            "model_file_sha256": installed.model_digest.value,
            "chat_template_sha256": None
            if installed.template_digest is None
            else installed.template_digest.value,
            "gguf_general_file_type": bound.file_type,
            "tensor_types": sorted(bound.tensor_types),
            "models_dir_is_outside_repository": ROOT not in default_models_dir().parents,
        },
        "sampling": {
            "temperature_milli": bound.sampling.temperature_milli,
            "seed": bound.sampling.seed,
            "context_tokens": bound.sampling.context_tokens,
            "max_output_tokens": world.scenario.quote.max_output_tokens,
        },
        "registry": _registry(world),
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
        "runtime_process": _runtime_process(installed.model_digest.hex),
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


def _registry(world: Any) -> list[dict[str, str]]:
    from inference_world import AGENT_KEY, BORROWER, GATEWAY_KEY, GATEWAY_SUBJECT

    from credence.inference.local_llama import LOCAL_PROVIDER_ID

    entries = [
        (AGENT_KEY.verify_key, "agent", BORROWER.value),
        (GATEWAY_KEY.verify_key, "gateway", GATEWAY_SUBJECT),
        (world.provider_key.verify_key, "provider", LOCAL_PROVIDER_ID.value),
    ]
    return sorted(
        (
            {"key_id": key_id_for(k).value, "role": r, "subject": s, "status": "current"}
            for k, r, s in entries
        ),
        key=lambda e: e["key_id"],
    )


if __name__ == "__main__":
    main()
