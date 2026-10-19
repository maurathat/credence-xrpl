"""Live Nebius model discovery for Inference Checkpoint 3: a bounded, sanitized catalog snapshot.

Run from the repository root with NEBIUS_API_KEY in the environment (never pass it on the
command line):

    CREDENCE_NEBIUS_LIVE=1 uv run python contracts/fixtures/discover_nebius_models.py

Calls GET /v1/models?verbose=true and writes contracts/fixtures/inference_c3/
nebius-model-discovery.json. The file holds the total model count, every model ID, and the full
public catalog entry for each model whose ID mentions Llama 3.1 8B. It holds no credential and no
request headers. This is runtime observation evidence: not deterministically regenerable.
Selecting the model is a separate, human-reviewed step; this script selects nothing.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from credence.inference.nebius import NEBIUS_BASE_URL, HttpsNebiusTransport

OUT = Path(__file__).resolve().parent / "inference_c3" / "nebius-model-discovery.json"
LLAMA_31_8B = re.compile(r"llama-?3\.1-8b", re.IGNORECASE)


def main() -> None:
    if os.environ.get("CREDENCE_NEBIUS_LIVE") != "1":
        raise SystemExit("set CREDENCE_NEBIUS_LIVE=1 (and NEBIUS_API_KEY) to call Nebius")
    transport = HttpsNebiusTransport.from_environment()
    listing = transport.get("/models?verbose=true")
    data = listing.get("data")
    if not isinstance(data, list):
        raise SystemExit("unexpected catalog shape: no data list")
    ids = sorted(str(m.get("id")) for m in data if isinstance(m, dict))
    matches = [m for m in data if isinstance(m, dict) and LLAMA_31_8B.search(str(m.get("id")))]
    record = {
        "id": "nebius-model-discovery",
        "kind": "runtime-observation",
        "regenerable": False,
        "observed_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endpoint": f"GET {NEBIUS_BASE_URL}/models?verbose=true",
        "model_count": len(ids),
        "model_ids": ids,
        "llama_3_1_8b_entries": sorted(matches, key=lambda m: str(m.get("id"))),
    }
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(record, indent=2, ensure_ascii=True) + "\n", encoding="ascii")
    print(f"wrote {OUT.relative_to(Path.cwd()) if OUT.is_relative_to(Path.cwd()) else OUT}")
    print(f"{len(ids)} models; Llama 3.1 8B entries: {[m.get('id') for m in matches]}")


if __name__ == "__main__":
    sys.exit(main())
