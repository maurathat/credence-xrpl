"""The deterministic CP3 marketplace selection fixture regenerates byte-for-byte and shows the
price-vs-evidence outcomes from Core's own dry-run decisions."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[2] / "contracts" / "fixtures"
PATH = FIXTURES / "inference_c3" / "market-selection-deterministic.json"


def test_regeneration_is_byte_identical() -> None:
    spec = importlib.util.spec_from_file_location(
        "build_c3_market", FIXTURES / "build_inference_c3_market_fixture.py"
    )
    assert spec is not None and spec.loader is not None
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    first, second = builder.render(), builder.render()
    assert first == second == PATH.read_text(encoding="ascii")


def test_price_vs_evidence() -> None:
    record = json.loads(PATH.read_text(encoding="ascii"))
    a = {q["provider_id"]: q for q in record["scenario_a_general"]["qualifications"]}
    b = {q["provider_id"]: q for q in record["scenario_b_controlled"]["qualifications"]}
    nebius, local = "nebius-token-factory", "local-qwen-provider"
    assert a[nebius]["qualified"] and a[local]["qualified"]
    assert a[nebius]["max_total_cost_micro_trlusd"] < a[local]["max_total_cost_micro_trlusd"]
    assert record["scenario_a_general"]["chosen_provider_id"] == nebius
    assert not b[nebius]["qualified"] and b[nebius]["reason_code"] == "PROFILE_NOT_ALLOWED"
    assert b[nebius]["max_total_cost_micro_trlusd"] < b[local]["max_total_cost_micro_trlusd"]
    assert b[local]["qualified"]
    assert record["scenario_b_controlled"]["chosen_provider_id"] == local
    assert record["kind"] == "deterministic-protocol-fixture" and record["regenerable"] is True
