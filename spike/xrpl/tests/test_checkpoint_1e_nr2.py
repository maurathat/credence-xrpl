"""Checkpoint 1E-NR2 offline environment-evidence regressions."""

from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from e6_negative_oracle import normalize_unordered_strings  # noqa: E402
from e6_oracle import BORROWER_MULTISIGN_PREFIX_HEX  # noqa: E402
from harness import serialize_json  # noqa: E402
from run_checkpoint_1e_n import (  # noqa: E402
    BASE_FEE_XRP_PATH,
    complete_pretransaction_evidence_skeleton,
    environment_non_json_inventory,
    evidence_destination,
    full_pretransaction_serialization_preflight,
    normalize_environment_evidence,
)

LIVE_02 = ROOT / "evidence" / "checkpoint-1e-n-live-02"
ENVIRONMENT_FIXTURE = ROOT / "evidence" / "checkpoint-1e-n" / "environment-start.json"
WIRE_FIXTURE = ROOT / "evidence" / "checkpoint-1b-r" / "server-info-before.json"


def _environment() -> dict[str, object]:
    return json.loads(ENVIRONMENT_FIXTURE.read_text())


def test_f1_live_02_fixture_reproduces_exact_float_failure() -> None:
    recorded = json.loads((LIVE_02 / "unexpected-result.json").read_text())
    expected = "unsupported JSON evidence type float at $.server_info.validated_ledger.base_fee_xrp"
    assert recorded["message"] == expected
    with pytest.raises(TypeError, match=expected.replace("$", r"\$")):
        serialize_json(_environment())


def test_f2_repaired_environment_payload_serializes() -> None:
    normalized = normalize_environment_evidence(_environment())
    decoded = json.loads(serialize_json(normalized))
    assert decoded["server_info"]["validated_ledger"]["base_fee_xrp"] == {
        "json_number": "1e-05",
        "normalization": "Python shortest round-trip decimal representation",
        "source_json_type": "number",
    }


def test_f3_base_fee_representation_is_deterministic() -> None:
    first = serialize_json(normalize_environment_evidence(_environment()))
    second = serialize_json(normalize_environment_evidence(_environment()))
    assert first == second


def test_f4_base_fee_normalization_is_non_lossy_for_recorded_value() -> None:
    environment = _environment()
    original = environment["server_info"]["validated_ledger"]["base_fee_xrp"]
    normalized = normalize_environment_evidence(environment)
    decimal_lexeme = normalized["server_info"]["validated_ledger"]["base_fee_xrp"]["json_number"]
    assert WIRE_FIXTURE.read_text().find('"base_fee_xrp":1e-05') >= 0
    assert decimal_lexeme == "1e-05"
    assert float(decimal_lexeme) == original


def test_f5_arbitrary_environment_float_still_fails_closed() -> None:
    with pytest.raises(TypeError, match=r"unhandled environment float at \$.server_info.uptime"):
        normalize_environment_evidence({"server_info": {"uptime": 1.5}})


def test_f6_credence_money_float_semantics_remain_rejected() -> None:
    with pytest.raises(TypeError, match=r"unhandled environment float at \$.tRLUSD_micro_units"):
        normalize_environment_evidence({"tRLUSD_micro_units": 1.5})
    with pytest.raises(
        TypeError, match=r"unsupported JSON evidence type float at \$.tRLUSD_micro_units"
    ):
        serialize_json({"tRLUSD_micro_units": 1.5})


def test_f7_complete_environment_inventory_and_payload_serialize() -> None:
    environment = _environment()
    assert BASE_FEE_XRP_PATH == ("server_info", "validated_ledger", "base_fee_xrp")
    assert environment_non_json_inventory(environment) == [
        {"path": "$.server_info.validated_ledger.base_fee_xrp", "python_type": "float"}
    ]
    serialize_json(normalize_environment_evidence(environment))


def test_f8_set_normalization_regression_remains_deterministic() -> None:
    assert normalize_unordered_strings(
        [BORROWER_MULTISIGN_PREFIX_HEX, BORROWER_MULTISIGN_PREFIX_HEX]
    ) == [BORROWER_MULTISIGN_PREFIX_HEX]


def test_f9_complete_pretransaction_preflight_covers_a_through_j() -> None:
    destination = evidence_destination("checkpoint-1e-n-live-03")
    result = full_pretransaction_serialization_preflight(destination)
    assert result["result"] == "PASS"
    assert list(result["coverage"]) == [
        "A_provenance",
        "B_amendment_evidence",
        "C_feature_response",
        "D_amendments_ledger_entry_response",
        "E_environment_evidence",
        "F_ledger_header_time",
        "G_actor_setup_evidence",
        "H_protected_state_structures",
        "I_N1_N6_evidence",
        "J_manifest_metadata",
    ]


def test_f10_injected_signing_domain_set_fails_preflight() -> None:
    destination = evidence_destination("checkpoint-1e-n-live-03")
    skeleton = complete_pretransaction_evidence_skeleton(destination)
    skeleton["I_N1_N6_evidence"]["cases"]["N1"]["state_assertions"][0]["actual"] = {
        BORROWER_MULTISIGN_PREFIX_HEX
    }
    with pytest.raises(TypeError, match="unsupported JSON evidence type set"):
        full_pretransaction_serialization_preflight(destination, skeleton=skeleton)


def test_f11_injected_unhandled_environment_float_fails_preflight() -> None:
    destination = evidence_destination("checkpoint-1e-n-live-03")
    skeleton = complete_pretransaction_evidence_skeleton(destination)
    skeleton["E_environment_evidence"]["server_info"]["uptime"] = 1.5
    with pytest.raises(TypeError, match="unsupported JSON evidence type float"):
        full_pretransaction_serialization_preflight(destination, skeleton=skeleton)


def test_f12_no_generic_stringify_or_float_fallback_exists() -> None:
    serializer_source = inspect.getsource(serialize_json)
    normalizer_source = inspect.getsource(normalize_environment_evidence)
    assert "default=" not in serializer_source
    assert "default=str" not in serializer_source
    assert "str(value)" not in normalizer_source


def test_future_provenance_uses_selected_destination() -> None:
    destination = evidence_destination("checkpoint-1e-n-live-03")
    skeleton = complete_pretransaction_evidence_skeleton(destination)
    assert skeleton["A_provenance"]["evidence_directory"] == ("evidence/checkpoint-1e-n-live-03")
