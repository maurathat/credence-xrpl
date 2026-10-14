"""Checkpoint 1C-H H1-H12 semantic state-assertion regressions."""

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
MODULE_PATH = SPIKE_ROOT / "scripts" / "harness.py"
VAULTCREATE_FIXTURE = SPIKE_ROOT / "evidence" / "checkpoint-1c-r" / "unexpected-result.json"
NEGATIVE_FIXTURE = (
    SPIKE_ROOT
    / "evidence"
    / "checkpoint-1c"
    / "transactions"
    / "15-investment_deposit_rejected.json"
)


def load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("checkpoint_1c_h_harness", MODULE_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


HARNESS = load_module()


@pytest.fixture
def vaultcreate() -> dict[str, Any]:
    value = json.loads(VAULTCREATE_FIXTURE.read_text())
    assert value["submission_result"] == "tesSUCCESS"
    assert value["validated_lookup_status"] == "VALIDATED"
    return value


def created_index(fixture: dict[str, Any], ledger_entry_type: str) -> str:
    meta = fixture["validated_transaction_response"]["meta"]
    matches = [
        wrapper["CreatedNode"]["LedgerIndex"]
        for wrapper in meta["AffectedNodes"]
        if wrapper.get("CreatedNode", {}).get("LedgerEntryType") == ledger_entry_type
    ]
    assert len(matches) == 1
    return str(matches[0])


def semantic_checks(
    fixture: dict[str, Any], *, after_objects: list[dict[str, Any]] | None = None
) -> list[Any]:
    transaction = fixture["canonical_public_transaction"]
    return HARNESS.vault_create_transition_assertions(
        before_objects=fixture["before_state"]["objects"],
        after_objects=(
            fixture["after_state"]["objects"] if after_objects is None else after_objects
        ),
        vault_index=created_index(fixture, "Vault"),
        owner=transaction["Account"],
        asset=transaction["Asset"],
        vault_kind=transaction["VaultKind"],
        subscription_date=transaction["SubscriptionDate"],
        redemption_date=transaction["RedemptionDate"],
        withdrawal_policy=transaction["WithdrawalPolicy"],
    )


def classify_positive(
    fixture: dict[str, Any],
    checks: list[Any],
    *,
    after_objects: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    resolution = HARNESS.resolve_transaction_result(
        submission_response=fixture["submission_response"],
        submission_hash_if_any=fixture["submission_hash_if_any"],
        validated_lookup=fixture["validated_transaction_response"],
    )
    after_state = copy.deepcopy(fixture["after_state"])
    if after_objects is not None:
        after_state["objects"] = after_objects
    return HARNESS.classify_transaction(
        expected_result="tesSUCCESS",
        resolution=resolution,
        before_state=fixture["before_state"],
        after_state=after_state,
        assertions=checks,
    )


def object_of_type(objects: list[dict[str, Any]], ledger_entry_type: str) -> dict[str, Any]:
    matches = [item for item in objects if item.get("LedgerEntryType") == ledger_entry_type]
    assert len(matches) == 1
    return matches[0]


def test_h1_real_vaultcreate_fixture_accepted(vaultcreate: dict[str, Any]) -> None:
    before_objects = vaultcreate["before_state"]["objects"]
    after_objects = vaultcreate["after_state"]["objects"]
    old_count_check = HARNESS.state_assertion(
        "one owner object created", len(after_objects) - len(before_objects), 1
    )
    checks = semantic_checks(vaultcreate)

    assert old_count_check.passed is False
    assert old_count_check.actual == 2
    assert all(check.passed for check in checks)
    assert classify_positive(vaultcreate, checks)["status"] == "PASS"


def test_h2_missing_vault_fails(vaultcreate: dict[str, Any]) -> None:
    after = [
        item
        for item in vaultcreate["after_state"]["objects"]
        if item.get("LedgerEntryType") != "Vault"
    ]
    checks = semantic_checks(vaultcreate, after_objects=after)

    assert classify_positive(vaultcreate, checks, after_objects=after)["status"] == "FAIL"


@pytest.mark.parametrize("defect", ["missing", "wrong_owner"])
def test_h3_missing_or_wrong_owner_mptoken_fails(vaultcreate: dict[str, Any], defect: str) -> None:
    after = copy.deepcopy(vaultcreate["after_state"]["objects"])
    token = object_of_type(after, "MPToken")
    if defect == "missing":
        after.remove(token)
    else:
        token["Account"] = "rWrongOwnerForSyntheticFixture"

    checks = semantic_checks(vaultcreate, after_objects=after)
    assert classify_positive(vaultcreate, checks, after_objects=after)["status"] == "FAIL"


def test_h4_unrelated_extra_object_does_not_fail(vaultcreate: dict[str, Any]) -> None:
    after = copy.deepcopy(vaultcreate["after_state"]["objects"])
    after.append(
        {
            "LedgerEntryType": "AccountRoot",
            "Account": "rSyntheticUnrelatedAccount",
            "Flags": 0,
            "index": "SYNTHETIC-UNRELATED-INDEX",
        }
    )
    checks = semantic_checks(vaultcreate, after_objects=after)

    assert all(check.passed for check in checks)
    assert classify_positive(vaultcreate, checks, after_objects=after)["status"] == "PASS"


def test_h5_object_order_does_not_matter(vaultcreate: dict[str, Any]) -> None:
    after = list(reversed(copy.deepcopy(vaultcreate["after_state"]["objects"])))
    checks = semantic_checks(vaultcreate, after_objects=after)

    assert all(check.passed for check in checks)
    assert classify_positive(vaultcreate, checks, after_objects=after)["status"] == "PASS"


def test_h6_wrong_vault_owner_fails(vaultcreate: dict[str, Any]) -> None:
    after = copy.deepcopy(vaultcreate["after_state"]["objects"])
    object_of_type(after, "Vault")["Owner"] = "rWrongOwnerForSyntheticFixture"
    checks = semantic_checks(vaultcreate, after_objects=after)

    assert classify_positive(vaultcreate, checks, after_objects=after)["status"] == "FAIL"


def test_h7_wrong_vault_asset_fails(vaultcreate: dict[str, Any]) -> None:
    after = copy.deepcopy(vaultcreate["after_state"]["objects"])
    object_of_type(after, "Vault")["Asset"] = {
        "currency": "SYNTHETIC-WRONG-ASSET",
        "issuer": "rSyntheticWrongIssuer",
    }
    checks = semantic_checks(vaultcreate, after_objects=after)

    assert classify_positive(vaultcreate, checks, after_objects=after)["status"] == "FAIL"


def test_h8_success_result_with_wrong_semantic_poststate_fails(
    vaultcreate: dict[str, Any],
) -> None:
    after = copy.deepcopy(vaultcreate["after_state"]["objects"])
    object_of_type(after, "Vault")["VaultKind"] = 0
    checks = semantic_checks(vaultcreate, after_objects=after)

    result = classify_positive(vaultcreate, checks, after_objects=after)
    assert result["observed_result"] == "tesSUCCESS"
    assert result["status"] == "FAIL"


def protected_state(fixture: dict[str, Any], side: str) -> dict[str, Any]:
    return {key: value for key, value in fixture[f"{side}_state"].items() if key != "ledger"}


def test_h9_committed_tecexpired_negative_still_passes() -> None:
    fixture = json.loads(NEGATIVE_FIXTURE.read_text())
    before = protected_state(fixture, "before")
    after = protected_state(fixture, "after")
    resolution = HARNESS.resolve_transaction_result(
        submission_response=fixture["submission_response"],
        submission_hash_if_any=fixture["transaction_hash"],
        validated_lookup=fixture["validated_transaction_response"],
    )
    checks = [HARNESS.state_assertion("protected state unchanged", after, before)]
    result = HARNESS.classify_transaction(
        expected_result="tecEXPIRED",
        resolution=resolution,
        before_state=before,
        after_state=after,
        assertions=checks,
        state_unchanged=after == before,
    )

    assert resolution["submission_result"] == "tecEXPIRED"
    assert resolution["validated_lookup_status"] == "NOT_IN_VALIDATED_LEDGER"
    assert resolution["authoritative_result"] == "tecEXPIRED"
    assert result["status"] == "PASS"


def test_h10_expected_negative_with_mutated_state_fails() -> None:
    fixture = json.loads(NEGATIVE_FIXTURE.read_text())
    before = protected_state(fixture, "before")
    after = copy.deepcopy(protected_state(fixture, "after"))
    after["vault"]["AssetsTotal"] = "synthetic-mutation"
    resolution = HARNESS.resolve_transaction_result(
        submission_response=fixture["submission_response"],
        validated_lookup=fixture["validated_transaction_response"],
    )
    checks = [HARNESS.state_assertion("protected state unchanged", after, before)]
    result = HARNESS.classify_transaction(
        expected_result="tecEXPIRED",
        resolution=resolution,
        before_state=before,
        after_state=after,
        assertions=checks,
        state_unchanged=after == before,
    )

    assert resolution["authoritative_result"] == "tecEXPIRED"
    assert result["status"] == "FAIL"


def test_h11_not_attempted_and_not_applicable_remain_distinct() -> None:
    statuses = ["PASS", "NOT_ATTEMPTED", "NOT_APPLICABLE", "NOT_ATTEMPTED"]
    serialized = json.loads(json.dumps(statuses))
    summary = HARNESS.experiment_status_summary(serialized)

    assert serialized[1] == "NOT_ATTEMPTED"
    assert serialized[2] == "NOT_APPLICABLE"
    assert summary["NOT_ATTEMPTED"] == 2
    assert summary["NOT_APPLICABLE"] == 1


def test_h12_evidence_manifest_tamper_detection(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.json"
    manifest = tmp_path / "MANIFEST.json"
    evidence.write_text('{"result":"untampered"}\n')
    HARNESS.create_manifest(tmp_path, manifest)

    assert HARNESS.verify_manifest(tmp_path, manifest) == []
    evidence.write_bytes(evidence.read_bytes() + b"x")
    assert HARNESS.verify_manifest(tmp_path, manifest)


def test_ripple_state_helper_uses_endpoints_asset_and_balance(
    vaultcreate: dict[str, Any],
) -> None:
    node = object_of_type(vaultcreate["before_state"]["objects"], "RippleState")
    checks = HARNESS.ripple_state_assertions(
        node,
        index=node["index"],
        low_limit=node["LowLimit"],
        high_limit=node["HighLimit"],
        balance=node["Balance"],
    )
    assert all(check.passed for check in checks)


def test_account_root_helper_uses_account_identity() -> None:
    fixture = json.loads(
        (SPIKE_ROOT / "evidence" / "checkpoint-1c" / "e2-final-state.json").read_text()
    )
    node = fixture["accounts"]["issuer"]["account_data"]
    checks = HARNESS.account_root_assertions(
        node,
        index=node["index"],
        account=node["Account"],
        required_flags=node["Flags"],
    )
    assert all(check.passed for check in checks)


def test_loan_broker_helper_normalizes_only_default_zero_fields() -> None:
    node = {
        "LedgerEntryType": "LoanBroker",
        "index": "SYNTHETIC-BROKER-ID",
        "Owner": "rSyntheticBrokerOwner",
        "Account": "rSyntheticBrokerPseudoAccount",
        "VaultID": "SYNTHETIC-VAULT-ID",
        "DebtMaximum": "50000",
        "CoverRateMinimum": 10000,
        "CoverRateLiquidation": 25000,
    }
    checks = HARNESS.loan_broker_assertions(
        node,
        index="SYNTHETIC-BROKER-ID",
        owner="rSyntheticBrokerOwner",
        vault_id="SYNTHETIC-VAULT-ID",
        debt_maximum="50000",
        cover_rate_minimum=10000,
        cover_rate_liquidation=25000,
    )
    assert all(check.passed for check in checks)


def test_cover_helper_requires_known_broker_relationship_and_exact_deltas() -> None:
    before = {
        "LedgerEntryType": "LoanBroker",
        "index": "SYNTHETIC-BROKER-ID",
        "Owner": "rSyntheticBrokerOwner",
        "Account": "rSyntheticBrokerPseudoAccount",
        "VaultID": "SYNTHETIC-VAULT-ID",
    }
    after = {**before, "CoverAvailable": "10000"}
    checks = HARNESS.cover_transition_assertions(
        before_broker=before,
        after_broker=after,
        broker_index="SYNTHETIC-BROKER-ID",
        owner="rSyntheticBrokerOwner",
        vault_id="SYNTHETIC-VAULT-ID",
        before_owner_balance="20000",
        after_owner_balance="10000",
        cover_delta=10000,
    )
    assert all(check.passed for check in checks)
