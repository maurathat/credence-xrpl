"""Generate the deterministic CP3 marketplace selection fixture (no execution, no network).

Run from the repository root:  uv run python contracts/fixtures/build_inference_c3_market_fixture.py

DETERMINISTIC PROTOCOL FIXTURE. Built from the real local Qwen Q8_0 profile (recorded in
inference_c3/local-qwen-tier2-observation.json), the Nebius NOT ASSERTED profile for
Qwen/Qwen3-30B-A3B-Instruct-2507, configured DEMO quotes (micro-tRLUSD; tRLUSD is a test token; not
Nebius's billed price), the TEST agent key and a fixed step clock. It records only Core's dry-run
qualification of each candidate and the routing choice for the two price-vs-evidence scenarios;
no provider is called. tests/conformance/test_inference_c3_market_fixture.py regenerates it
byte-for-byte.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent / "inference_c3" / "market-selection-deterministic.json"
sys.path.insert(0, str(ROOT / "tests" / "unit"))

from market_world import (  # noqa: E402
    CONTROLLED,
    CONTROLLED_FACILITY,
    GENERAL,
    GENERAL_FACILITY,
    LOCAL_PROFILE,
    LOCAL_QUOTE,
    LOCAL_SERVICE,
    NEBIUS_PROFILE,
    NEBIUS_QUOTE,
    NEBIUS_SERVICE,
    candidates,
    market_world,
)

from credence.core.objects import CanonicalObject  # noqa: E402
from credence.inference.market import Selection  # noqa: E402


def _c(obj: CanonicalObject) -> Any:
    return obj.to_canonical()


def _selection(selection: Selection) -> dict[str, Any]:
    return {
        "qualifications": [
            {
                "provider_id": q.purchase.request.provider_id.value,
                "qualified": q.qualified,
                "reason_code": None if q.reason_code is None else q.reason_code.value,
                "max_total_cost_micro_trlusd": q.max_total_cost,
                "purchase_request": _c(q.purchase.request),
                "dry_run_policy_decision": _c(q.decision.record),
            }
            for q in selection.qualifications
        ],
        "chosen_provider_id": None
        if selection.chosen is None
        else selection.chosen.purchase.request.provider_id.value,
    }


def render() -> str:
    with tempfile.TemporaryDirectory() as tmp:
        gateway = market_world().gateway(Path(tmp))
        scenario_a = gateway.select(candidates(GENERAL_FACILITY, GENERAL))
        scenario_b = gateway.select(candidates(CONTROLLED_FACILITY, CONTROLLED))
    record = {
        "id": "market-selection-deterministic",
        "kind": "deterministic-protocol-fixture",
        "regenerable": True,
        "routing_rule": "lowest max_total_cost among Core-accepted candidates, then provider ID, then quote ID",
        "quote_provenance": "configured DEMO prices in micro-tRLUSD (tRLUSD is a test token); not Nebius's billed price",
        "objects": {
            "nebius_execution_profile": _c(NEBIUS_PROFILE),
            "nebius_service_class": _c(NEBIUS_SERVICE),
            "nebius_quote": _c(NEBIUS_QUOTE),
            "local_execution_profile": _c(LOCAL_PROFILE),
            "local_service_class": _c(LOCAL_SERVICE),
            "local_quote": _c(LOCAL_QUOTE),
            "general_procurement_class": _c(GENERAL),
            "controlled_procurement_class": _c(CONTROLLED),
            "general_facility": _c(GENERAL_FACILITY),
            "controlled_facility": _c(CONTROLLED_FACILITY),
        },
        "scenario_a_general": _selection(scenario_a),
        "scenario_b_controlled": _selection(scenario_b),
    }
    return json.dumps(record, indent=2, ensure_ascii=True) + "\n"


def main() -> None:
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(render(), encoding="ascii")
    print(f"wrote {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
