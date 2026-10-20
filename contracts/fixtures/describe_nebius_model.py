"""Record Nebius's full public catalog entry for exactly one model ID (sanitized, no inference).

Run from the repository root with NEBIUS_API_KEY in the environment (never on the command line):

    CREDENCE_NEBIUS_LIVE=1 uv run python contracts/fixtures/describe_nebius_model.py <model-id>

Calls only GET /v1/models?verbose=true and writes contracts/fixtures/inference_c3/
nebius-model-<slug>.json holding that one entry exactly as returned, plus the observation time.
No credential, no headers, no inference call. Catalog fields such as `quantization` are Nebius's
own declarations, recorded as metadata, never as Credence ExecutionProfile evidence. Runtime
observation: not deterministically regenerable.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from credence.inference.nebius import NEBIUS_BASE_URL, HttpsNebiusTransport

OUT_DIR = Path(__file__).resolve().parent / "inference_c3"


def main(argv: list[str]) -> None:
    if os.environ.get("CREDENCE_NEBIUS_LIVE") != "1":
        raise SystemExit("set CREDENCE_NEBIUS_LIVE=1 (and NEBIUS_API_KEY) to call Nebius")
    if len(argv) != 1:
        raise SystemExit("usage: describe_nebius_model.py <exact-model-id>")
    model_id = argv[0]
    listing = HttpsNebiusTransport.from_environment().get("/models?verbose=true")
    data = listing.get("data")
    if not isinstance(data, list):
        raise SystemExit("unexpected catalog shape: no data list")
    entries = [m for m in data if isinstance(m, dict) and m.get("id") == model_id]
    record = {
        "id": "nebius-model-detail",
        "kind": "runtime-observation",
        "regenerable": False,
        "observed_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endpoint": f"GET {NEBIUS_BASE_URL}/models?verbose=true",
        "requested_model_id": model_id,
        "exact_match_count": len(entries),
        "entry": entries[0] if len(entries) == 1 else None,
        "note": "catalog fields are Nebius declarations, not Credence evidence",
    }
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", model_id)
    out = OUT_DIR / f"nebius-model-{slug}.json"
    OUT_DIR.mkdir(exist_ok=True)
    out.write_text(json.dumps(record, indent=2, ensure_ascii=True) + "\n", encoding="ascii")
    print(f"wrote {out.name}; exact matches: {len(entries)}")


if __name__ == "__main__":
    main(sys.argv[1:])
