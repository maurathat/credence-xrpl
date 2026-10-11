"""Regression coverage for the Checkpoint 1A amendment-state validator."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SPIKE_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = SPIKE_ROOT / "scripts" / "amendment_validation.py"
FIXTURE_PATH = SPIKE_ROOT / "evidence" / "feature-raw-original.json"


def load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("amendment_validation", MODULE_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


MODULE = load_module()


@pytest.fixture
def raw_feature_response() -> dict[str, Any]:
    value = json.loads(FIXTURE_PATH.read_text())
    assert isinstance(value, dict)
    return value


def test_actual_xrpld_3_4_0_response_passes(raw_feature_response: dict[str, Any]) -> None:
    validation = MODULE.validate_feature_response(raw_feature_response)

    assert validation["valid"] is True
    assert validation["required_count"] == 8
    assert all(item["exists"] for item in validation["amendments"].values())
    assert all(item["supported"] for item in validation["amendments"].values())
    assert all(item["enabled"] for item in validation["amendments"].values())


def test_old_shallow_result_lookup_reproduces_false_blocked(
    raw_feature_response: dict[str, Any],
) -> None:
    result = raw_feature_response["result"]
    old_interpretation = {
        name: bool(result.get(amendment_id, {}).get("enabled"))
        for name, amendment_id in MODULE.REQUIRED_AMENDMENTS.items()
    }

    assert not any(old_interpretation.values())
    assert MODULE.validate_feature_response(raw_feature_response)["valid"] is True


@pytest.mark.parametrize("defect", ["missing", "disabled", "unsupported"])
def test_genuine_required_amendment_defect_fails(
    raw_feature_response: dict[str, Any], defect: str
) -> None:
    response = copy.deepcopy(raw_feature_response)
    amendment_id = MODULE.REQUIRED_AMENDMENTS["LendingProtocolV1_1"]
    features = response["result"]["features"]
    if defect == "missing":
        del features[amendment_id]
    elif defect == "disabled":
        features[amendment_id]["enabled"] = False
    else:
        features[amendment_id]["supported"] = False

    with pytest.raises(MODULE.AmendmentValidationError):
        MODULE.validate_feature_response(response)


def test_missing_nested_features_map_fails(raw_feature_response: dict[str, Any]) -> None:
    response = copy.deepcopy(raw_feature_response)
    del response["result"]["features"]

    with pytest.raises(MODULE.AmendmentValidationError, match=r"result\.features"):
        MODULE.validate_feature_response(response)
