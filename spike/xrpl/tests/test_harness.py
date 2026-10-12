"""Checkpoint 1C-R regression tests for fail-closed result handling."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

SPIKE_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = SPIKE_ROOT / "scripts" / "harness.py"
COMMITTED_FAILURE = (
    SPIKE_ROOT
    / "evidence"
    / "checkpoint-1c"
    / "transactions"
    / "15-investment_deposit_rejected.json"
)


def load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("checkpoint_1c_r_harness", MODULE_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


HARNESS = load_module()


def assertions(passed: bool = True) -> list[object]:
    return [HARNESS.predicate_assertion("relevant state unchanged", passed, True, passed)]


def test_r1_tec_submission_is_not_overwritten_by_unvalidated_lookup() -> None:
    result = HARNESS.resolve_transaction_result(
        submission_response={"engine_result": "tecEXPIRED"},
        validated_lookup={"status": "success", "validated": False},
    )
    assert result["authoritative_result"] == "tecEXPIRED"
    assert result["validated_result_if_any"] is None
    assert result["validated_lookup_status"] == "NOT_IN_VALIDATED_LEDGER"


def test_r2_expected_negative_requires_and_passes_with_unchanged_state() -> None:
    resolution = HARNESS.resolve_transaction_result(
        submission_response={"engine_result": "tecEXPIRED"}
    )
    result = HARNESS.classify_transaction(
        expected_result="tecEXPIRED",
        resolution=resolution,
        before_state={"value": "1"},
        after_state={"value": "1"},
        assertions=assertions(),
        state_unchanged=True,
    )
    assert result["status"] == "PASS"


def test_r3_tes_success_cannot_pass_without_validated_poststate() -> None:
    resolution = HARNESS.resolve_transaction_result(
        submission_response={"engine_result": "tesSUCCESS"}
    )
    result = HARNESS.classify_transaction(
        expected_result="tesSUCCESS",
        resolution=resolution,
        before_state=None,
        after_state=None,
        assertions=[],
    )
    assert result["status"] == "FAIL"


def test_r4_client_exception_is_distinct_from_ledger_failure() -> None:
    result = HARNESS.resolve_transaction_result(
        client_exception={"type": "XRPLModelException", "message": "invalid transaction"}
    )
    assert result["category"] == "CLIENT_SIDE_EXCEPTION"
    assert result["authoritative_result"] is None


def test_r5_absent_lookup_never_overwrites_submission_result() -> None:
    result = HARNESS.resolve_transaction_result(
        submission_response={"engine_result": "tecTOO_SOON"}, validated_lookup=None
    )
    assert result["submission_result"] == "tecTOO_SOON"
    assert result["validated_lookup_status"] == "NOT_ATTEMPTED"
    assert result["authoritative_result"] == "tecTOO_SOON"


def test_r6_unexpected_engine_result_fails_even_when_state_is_unchanged() -> None:
    resolution = HARNESS.resolve_transaction_result(
        submission_response={"engine_result": "tecNO_PERMISSION"}
    )
    result = HARNESS.classify_transaction(
        expected_result="tecEXPIRED",
        resolution=resolution,
        before_state={"value": "1"},
        after_state={"value": "1"},
        assertions=assertions(),
        state_unchanged=True,
    )
    assert result["status"] == "FAIL"


def test_r7_not_attempted_and_not_applicable_are_distinct_statuses() -> None:
    assert "NOT_ATTEMPTED" in HARNESS.VALID_EXPERIMENT_STATUSES
    assert "NOT_APPLICABLE" in HARNESS.VALID_EXPERIMENT_STATUSES
    assert "NOT_ATTEMPTED" != "NOT_APPLICABLE"


def test_r8_committed_checkpoint_1c_evidence_classifies_correctly() -> None:
    evidence = json.loads(COMMITTED_FAILURE.read_text())
    resolution = HARNESS.resolve_transaction_result(
        submission_response=evidence["submission_response"],
        submission_hash_if_any=evidence["transaction_hash"],
        validated_lookup=evidence["validated_transaction_response"],
    )
    state_checks = [HARNESS.StateAssertion(**item) for item in evidence["state_assertions"]]
    result = HARNESS.classify_transaction(
        expected_result=evidence["expected_result"],
        resolution=resolution,
        before_state=evidence["before_state"],
        after_state=evidence["after_state"],
        assertions=state_checks,
        state_unchanged=True,
    )
    assert resolution["submission_result"] == "tecEXPIRED"
    assert resolution["validated_lookup_status"] == "NOT_IN_VALIDATED_LEDGER"
    assert result["status"] == "PASS"
