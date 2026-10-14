"""Checkpoint 1C-B B1-B12 frozen-oracle runner regressions."""

from __future__ import annotations

import ast
import hashlib
import sys
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

SPIKE_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SPIKE_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from frozen_oracle import (  # noqa: E402
    CAPTURE_PATHS,
    ORACLE_ORDER,
    REQUIRED_OUTPUT_FIELDS,
    OracleExecutionError,
    OraclePreflightError,
    OracleSession,
    default_bindings,
    evaluate_observation,
    preflight,
)
from oracle_fixture import fixture_providers, run_fixture_dry_run, synthetic_observations  # noqa: E402

H_TEST = SPIKE_ROOT / "tests" / "test_state_assertions.py"
H_TEST_SHA256 = "634880c33181da678d8dead2dac1e932c0dd6aafac44c862fa48259b7710b77c"
PROTOCOL_RUNNER = SCRIPTS / "run_checkpoint_1c_r.py"


def test_b1_preflight_passes_when_a_through_k_are_fully_bound() -> None:
    bindings = preflight(default_bindings())
    assert tuple(binding.step for binding in bindings) == ORACLE_ORDER


def test_b2_missing_a_fails_preflight() -> None:
    with pytest.raises(OraclePreflightError, match="order mismatch"):
        preflight(default_bindings()[1:])


def test_b3_missing_k_fails_preflight() -> None:
    with pytest.raises(OraclePreflightError, match="order mismatch"):
        preflight(default_bindings()[:-1])


def test_b4_reordered_a_through_k_fails_preflight() -> None:
    bindings = list(default_bindings())
    bindings[5], bindings[6] = bindings[6], bindings[5]
    with pytest.raises(OraclePreflightError, match="order mismatch"):
        preflight(bindings)


def test_b5_missing_semantic_predicate_binding_fails_preflight() -> None:
    bindings = list(default_bindings())
    bindings[0] = replace(bindings[0], predicate_ids=bindings[0].predicate_ids[:-1])
    with pytest.raises(OraclePreflightError, match="semantic predicate binding mismatch"):
        preflight(bindings)


def test_preflight_fails_when_positive_validation_binding_is_missing() -> None:
    bindings = list(default_bindings())
    bindings[0] = replace(bindings[0], requires_validated_post_state=False)
    with pytest.raises(OraclePreflightError, match="validated post-state"):
        preflight(bindings)


def test_preflight_fails_when_negative_protected_state_binding_is_missing() -> None:
    bindings = list(default_bindings())
    bindings[3] = replace(bindings[3], requires_protected_state=False)
    with pytest.raises(OraclePreflightError, match="protected-state"):
        preflight(bindings)


def test_preflight_fails_when_required_capture_path_is_missing() -> None:
    bindings = list(default_bindings())
    bindings[0] = replace(bindings[0], capture_paths=CAPTURE_PATHS - {"client_exception"})
    with pytest.raises(OraclePreflightError, match="missing capture paths"):
        preflight(bindings)


def test_runtime_fails_closed_if_declared_predicate_is_not_invoked() -> None:
    binding = default_bindings()[0]
    original = binding.evaluator

    def incomplete(observation: dict[str, Any]) -> dict[str, Any]:
        results = original(observation)
        results.pop(binding.predicate_ids[-1])
        return results

    with pytest.raises(OracleExecutionError, match="predicate invocation mismatch"):
        evaluate_observation(replace(binding, evaluator=incomplete), synthetic_observations()["A"])


def test_b6_tes_success_without_validated_post_state_fails() -> None:
    observation = synthetic_observations()["A"]
    observation["validated_lookup"] = {"validated": False}
    result = evaluate_observation(default_bindings()[0], observation)
    assert result["classification"]["status"] == "FAIL"
    assert "matching validated result" in " ".join(result["classification"]["reasons"])


def test_b7_expected_negative_with_changed_protected_state_fails() -> None:
    observation = synthetic_observations()["D"]
    observation["post_state"] = deepcopy(observation["post_state"])
    observation["post_state"]["protected"]["lender_account_sequence"] = 8
    result = evaluate_observation(default_bindings()[3], observation)
    assert result["classification"]["status"] == "FAIL"
    assert result["protected_state_unchanged"] is False


def test_negative_cannot_pass_when_protected_snapshot_is_absent() -> None:
    observation = synthetic_observations()["D"]
    observation["pre_state"] = {}
    observation["post_state"] = {}
    result = evaluate_observation(default_bindings()[3], observation)
    assert result["classification"]["status"] == "FAIL"
    assert result["protected_state_unchanged"] is False


def test_b8_client_exception_is_recorded_distinctly() -> None:
    observation = synthetic_observations()["A"]
    observation["submission_response"] = None
    observation["validated_lookup"] = None
    observation["client_exception"] = {
        "type": "SyntheticClientException",
        "message": "fixture-only client failure",
    }
    result = evaluate_observation(default_bindings()[0], observation)
    assert result["classification"]["status"] == "FAIL"
    assert result["result_resolution"]["category"] == "CLIENT_SIDE_EXCEPTION"
    assert result["client_exception"]["type"] == "SyntheticClientException"


def test_b9_failure_at_d_prevents_e_through_k_execution() -> None:
    observations = synthetic_observations()
    observations["D"]["post_state"] = deepcopy(observations["D"]["post_state"])
    observations["D"]["post_state"]["protected"]["lender_account_sequence"] = 8
    called: list[str] = []

    def provider(step: str, value: dict[str, Any]) -> Any:
        def capture() -> dict[str, Any]:
            called.append(step)
            return deepcopy(value)

        return capture

    providers = {step: provider(step, value) for step, value in observations.items()}
    session = OracleSession()
    records = session.run(providers)
    assert called == ["A", "B", "C", "D"]
    assert [record["step"] for record in records] == called
    assert records[-1]["classification"]["status"] == "FAIL"


def test_b10_complete_fixture_dry_run_traverses_a_through_k_in_order() -> None:
    result = run_fixture_dry_run()
    assert result["outcome"] == "PASS"
    assert result["order"] == list(ORACLE_ORDER)
    assert all(record["classification"]["status"] == "PASS" for record in result["records"])
    assert result["protocol_transactions_submitted"] == 0
    assert result["environment_started"] is False


def test_live_protocol_runner_binds_a_through_k_in_source_order() -> None:
    tree = ast.parse(PROTOCOL_RUNNER.read_text())
    observed: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg not in {"step", "oracle_step"}:
                continue
            if isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                observed.append(keyword.value.value)
    line_order = sorted(
        (
            node.lineno,
            keyword.value.value,
        )
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg in {"step", "oracle_step"}
        and isinstance(keyword.value, ast.Constant)
        and isinstance(keyword.value.value, str)
    )
    assert observed
    assert [step for _line, step in line_order] == list(ORACLE_ORDER)


@pytest.mark.parametrize("failed_step", ORACLE_ORDER)
def test_failure_at_each_step_short_circuits_every_later_step(failed_step: str) -> None:
    observations = synthetic_observations()
    target = observations[failed_step]
    target["submission_response"] = None
    target["validated_lookup"] = None
    target["client_exception"] = {
        "type": "SyntheticClientException",
        "message": f"fixture failure at {failed_step}",
    }
    called: list[str] = []

    def provider(step: str, value: dict[str, Any]) -> Any:
        def capture() -> dict[str, Any]:
            called.append(step)
            return deepcopy(value)

        return capture

    session = OracleSession()
    session.run({step: provider(step, value) for step, value in observations.items()})
    expected = list(ORACLE_ORDER[: ORACLE_ORDER.index(failed_step) + 1])
    assert called == expected
    assert session.records[-1]["classification"]["status"] == "FAIL"


def test_b11_every_a_through_k_output_has_required_evidence_metadata() -> None:
    session = OracleSession()
    records = session.run(fixture_providers())
    session.require_complete()
    assert [record["step"] for record in records] == list(ORACLE_ORDER)
    for record in records:
        assert REQUIRED_OUTPUT_FIELDS.issubset(record)
        assert record["ledger_index"] is not None
        assert record["close_time"] is not None
        assert record["synthetic"] is True
        assert record["state_assertions"]


def test_b12_h1_through_h12_test_file_is_unchanged() -> None:
    assert hashlib.sha256(H_TEST.read_bytes()).hexdigest() == H_TEST_SHA256
