#!/usr/bin/env python3
"""Freeze and execute Checkpoint 1E-N without another positive LoanSet."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from amendment_validation import (
    REQUIRED_AMENDMENTS,
    amendments_ledger_entry_params,
    cross_check_amendment_representations,
    validate_amendments_ledger_entry,
    validate_feature_response,
)
from e6_negative_oracle import (
    NEGATIVE_CASES,
    PROSPECTIVE_TIMING,
    case_definitions,
    live_binding_assertions,
    n5_mutation_assertions,
    negative_authorization_assertions,
    require_live_bindings,
    require_static_preflight,
    static_preflight_assertions,
)
from e6_oracle import BORROWER_MULTISIGN_PREFIX_HEX, loan_index, negative_state_assertions
from harness import (
    StateAssertion,
    create_manifest,
    json_safe,
    serialize_json,
    sha256_file,
    state_assertion,
    write_json,
)
from run_checkpoint_1e import (
    NETWORK_ID,
    ROOT_SEED_ENV,
    SOURCE_COMMIT,
    CheckpointStop,
    Runner,
    all_pass,
    create_actors,
    create_lending_fixture,
    finalize_and_sign,
    git_head,
    ledger_header,
    ledger_node,
    loan_state,
    loan_template,
    rpc,
    rpc_success,
    signed_terms_from,
)
from xrpl.clients import JsonRpcClient
from xrpl.core.binarycodec import encode
from xrpl.wallet import Wallet

ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parents[1]
EVIDENCE_ROOT = ROOT / "evidence"
ASSERTIONS_PATH = ROOT / "E6-NEGATIVE-ASSERTIONS.md"
TIMING_PATH = ROOT / "E6-TIMING-ORACLE-v2.md"
ORACLE_PATH = ROOT / "scripts" / "e6_negative_oracle.py"
RUNNER_PATH = Path(__file__).resolve()
TEST_PATH = ROOT / "tests" / "test_checkpoint_1e_n.py"
EVIDENCE_NAME = "checkpoint-1e-n"

CASE_LABELS = {
    "N1": "n1_research_bot_alone",
    "N2": "n2_credence_policy_alone",
    "N3": "n3_missing_counterparty_signature",
    "N4": "n4_invalid_counterparty_signature",
    "N5": "n5_altered_principal_reuses_counterparty_signature",
    "N6": "n6_subscription_phase",
}


def evidence_destination(value: str | Path) -> Path:
    raw = Path(value)
    if not raw.parts or any(part == ".." for part in raw.parts):
        raise CheckpointStop("evidence destination path traversal is not allowed")
    root = EVIDENCE_ROOT.resolve()
    destination = (root / raw).resolve() if len(raw.parts) == 1 else (REPO_ROOT / raw).resolve()
    if destination == root or root not in destination.parents:
        raise CheckpointStop("evidence destination must be under spike/xrpl/evidence")
    return destination


def frozen_file_hashes() -> dict[str, str]:
    return {
        "E6-NEGATIVE-ASSERTIONS.md": sha256_file(ASSERTIONS_PATH),
        "E6-TIMING-ORACLE-v2.md": sha256_file(TIMING_PATH),
        "scripts/e6_negative_oracle.py": sha256_file(ORACLE_PATH),
        "scripts/run_checkpoint_1e_n.py": sha256_file(RUNNER_PATH),
        "tests/test_checkpoint_1e_n.py": sha256_file(TEST_PATH),
    }


def preflight_payload() -> dict[str, Any]:
    require_static_preflight()
    return {
        "checkpoint": "1E-N",
        "mode": "PRE_LEDGER_STATIC_PREFLIGHT",
        "git_commit": git_head(),
        "new_xrpl_transactions": 0,
        "xrpld_started": False,
        "positive_loanset_included": False,
        "loan_pay_included": False,
        "e7_e8_e9_included": False,
        "prospective_timing_oracle": PROSPECTIVE_TIMING,
        "negative_cases": case_definitions(),
        "protected_state": {
            "N1-N5": "complete loan_state projection unchanged",
            "N6": "complete lending projection unchanged; only exact borrower XRP fee and Sequence increment allowed",
        },
        "frozen_files": frozen_file_hashes(),
        "assertions": static_preflight_assertions(),
        "full_evidence_serialization": full_evidence_serialization_preflight(),
        "result": "PASS",
    }


def build_negative_case_record(
    *,
    case_id: str,
    label: str,
    expected_result: str,
    observed_result: str | None,
    expect_validated: bool,
    validated: bool,
    local_validation_result: str | None,
    submission_result: str | None,
    validated_result: str | None,
    submission_hash_if_any: str | None,
    before_state: dict[str, Any],
    after_state: dict[str, Any],
    state_assertions: list[StateAssertion],
    classification_status: str,
    public_signing_evidence: dict[str, Any],
    submission_response: dict[str, Any] | None,
    validated_transaction_response: dict[str, Any] | None,
    ledger_before: dict[str, Any],
    ledger_after: dict[str, Any],
    timing: dict[str, Any],
) -> dict[str, Any]:
    """Construct one JSON-native record through the production evidence boundary."""

    value = {
        "experiment": "E6-NEGATIVE",
        "case": case_id,
        "label": label,
        "expected_result": expected_result,
        "observed_result": observed_result,
        "expect_validated": expect_validated,
        "validated": validated,
        "local_validation_result": local_validation_result,
        "submission_result": submission_result,
        "validated_result": validated_result,
        "submission_hash_if_any": submission_hash_if_any,
        "before_state": before_state,
        "after_state": after_state,
        "state_assertions": state_assertions,
        "classification": {"status": classification_status},
        "public_signing_evidence": public_signing_evidence,
        "submission_response": submission_response,
        "validated_transaction_response": validated_transaction_response,
        "ledger_before": ledger_before,
        "ledger_after": ledger_after,
        "timing": timing,
    }
    normalized = json_safe(value)
    if not isinstance(normalized, dict):  # pragma: no cover - fixed top-level construction
        raise TypeError("negative evidence record did not normalize to an object")
    return normalized


def _serialization_skeleton_record(
    definition: Any,
    prepared: dict[str, Any] | None = None,
) -> dict[str, Any]:
    fixture = dict((prepared or {}).get("fixture") or {})
    subscription_date = int(fixture.get("subscription_date", 100))
    redemption_date = int(fixture.get("redemption_date", 200))
    ledger_before = {
        "close_flags": 0,
        "close_time": subscription_date if definition.case_id == "N6" else subscription_date + 1,
        "close_time_resolution": 10,
        "closed": True,
        "ledger_hash": "HASH_BEFORE_PLACEHOLDER",
        "ledger_index": 1,
        "parent_close_time": subscription_date - 1,
    }
    ledger_after = {
        **ledger_before,
        "close_time": ledger_before["close_time"] + 1,
        "ledger_hash": "HASH_AFTER_PLACEHOLDER",
        "ledger_index": 2,
        "parent_close_time": ledger_before["close_time"],
    }
    signing_evidence = (prepared or {}).get("auth") or {
        "transaction_hash": None,
        "signed_transaction_blob": None,
        "exact_loan_set_body": {"PrincipalRequested": definition.principal},
        "bound_loan_terms": {"PrincipalRequested": definition.principal},
        "borrower_authorization": {
            "account": "BORROWER_ACCOUNT_PLACEHOLDER",
            "top_level_signing_public_key": "",
            "top_level_transaction_signature": None,
            "signers": [
                {
                    "account": f"{role.upper()}_ACCOUNT_PLACEHOLDER",
                    "public_key_hex": None,
                    "txn_signature_hex": None,
                    "signing_prefix_hex": BORROWER_MULTISIGN_PREFIX_HEX,
                    "signing_payload_hex": None,
                    "signing_payload_sha512_half": None,
                    "serialized_body_hex": None,
                }
                for role in definition.borrower_signers
            ],
        },
        "broker_counterparty_authorization": {
            "counterparty": "BROKER_ACCOUNT_PLACEHOLDER",
            "public_key_hex": None,
            "txn_signature_hex": None,
            "counterparty_signers": None,
            "signing_prefix_hex": "CPT-NUL",
            "signing_payload_hex": None,
            "signing_payload_sha512_half": None,
            "serialized_body_hex": None,
        },
    }
    auth_assertions = list((prepared or {}).get("auth_checks") or [])
    if not auth_assertions:
        auth_assertions = [
            state_assertion(
                "borrower SMT-NUL domains",
                [BORROWER_MULTISIGN_PREFIX_HEX],
                [BORROWER_MULTISIGN_PREFIX_HEX],
            )
        ]
    protected_before = {
        "loan": None,
        "borrower_account": {
            "Account": "BORROWER_ACCOUNT_PLACEHOLDER",
            "Balance": "1000",
            "OwnerCount": 2,
            "Sequence": 7,
        },
        "vault": {"AssetsAvailable": "100000"},
    }
    protected_after = json.loads(json.dumps(protected_before))
    if definition.case_id == "N6":
        protected_after["borrower_account"]["Balance"] = "960"
        protected_after["borrower_account"]["Sequence"] = 8
        protected_assertions = n6_state_assertions(
            before=protected_before,
            after=protected_after,
            observed="tecTOO_SOON",
            fee=40,
        )
    else:
        protected_assertions = negative_state_assertions(
            observed_result=definition.expected_result,
            expected_result=definition.expected_result,
            before=protected_before,
            after=protected_after,
        )
    assertions = [
        *auth_assertions,
        *((prepared or {}).get("extra_checks") or []),
        *protected_assertions,
        state_assertion(
            f"{definition.case_id} execution phase", definition.phase, definition.phase
        ),
    ]
    timing: dict[str, Any] = {
        "subscription_date": subscription_date,
        "redemption_date": redemption_date,
        "parent_validated_ledger": ledger_before,
        "accepted_or_unchanged_ledger": ledger_after,
        "phase_at_parent_close": definition.phase,
    }
    if definition.case_id == "N6":
        timing.update(
            {
                "source_derived_applying_view_header_close_time": (
                    ledger_before["close_time"] + ledger_after["close_time_resolution"]
                ),
                "accepted_ledger": ledger_after,
                "accepted_transaction_date": None,
                "prospective_oracle_start_date_anchor_used": False,
            }
        )
    return build_negative_case_record(
        case_id=definition.case_id,
        label=CASE_LABELS[definition.case_id],
        expected_result=definition.expected_result,
        observed_result=None,
        expect_validated=definition.expect_validated,
        validated=False,
        local_validation_result=None,
        submission_result=None,
        validated_result=None,
        submission_hash_if_any=None,
        before_state={"loan": None, "phase": definition.phase},
        after_state={"loan": None, "phase": definition.phase},
        state_assertions=assertions,
        classification_status="NOT_EXECUTED",
        public_signing_evidence=signing_evidence,
        submission_response=None,
        validated_transaction_response=None,
        ledger_before=ledger_before,
        ledger_after=ledger_after,
        timing=timing,
    )


def complete_evidence_skeleton(
    prepared_cases: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build all post-execution shapes without submitting a transaction."""

    prepared_cases = prepared_cases or {}
    return {
        "cases": {
            definition.case_id: _serialization_skeleton_record(
                definition, prepared_cases.get(definition.case_id)
            )
            for definition in NEGATIVE_CASES
        },
        "experiment_summary": [],
        "checkpoint_result": {
            "outcome": "NOT_EXECUTED",
            "negative_results": {
                definition.case_id: {
                    "status": "NOT_EXECUTED",
                    "expected": definition.expected_result,
                    "observed": None,
                }
                for definition in NEGATIVE_CASES
            },
        },
        "manifest_metadata": {"algorithm": "SHA-256", "file_count": 0, "files": []},
    }


def full_evidence_serialization_preflight(
    prepared_cases: dict[str, dict[str, Any]] | None = None,
    *,
    skeleton: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Exercise the production serializer over every future N1-N6 evidence shape."""

    value = skeleton if skeleton is not None else complete_evidence_skeleton(prepared_cases)
    serialized = serialize_json(value)
    decoded = json.loads(serialized)
    case_ids = sorted(decoded.get("cases", {}))
    expected_ids = [definition.case_id for definition in NEGATIVE_CASES]
    if case_ids != expected_ids:
        raise TypeError(f"serialization preflight cases {case_ids!r} != {expected_ids!r}")
    return {
        "result": "PASS",
        "cases": {case_id: "PASS" for case_id in case_ids},
        "manifest_metadata": "PASS",
        "protected_state": "PASS",
        "authorization_signing": "PASS",
        "result_placeholders": "PASS",
        "phase_timing": "PASS",
        "serialized_sha256": hashlib.sha256(serialized.encode()).hexdigest(),
    }


def write_preflight(destination: Path) -> int:
    if destination.exists() and any(destination.iterdir()):
        raise CheckpointStop(f"evidence destination is not empty: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    write_json(destination / "preflight.json", preflight_payload())
    print("Checkpoint 1E-N pre-ledger preflight PASS; xrpld was not started.")
    return 0


def verify_preflight(destination: Path) -> dict[str, Any]:
    path = destination / "preflight.json"
    if not path.is_file():
        raise CheckpointStop("frozen preflight.json is absent")
    value = json.loads(path.read_text())
    if value.get("result") != "PASS":
        raise CheckpointStop("frozen preflight did not pass")
    if value.get("git_commit") != git_head():
        raise CheckpointStop("git HEAD changed after negative preflight")
    if value.get("frozen_files") != frozen_file_hashes():
        raise CheckpointStop("a frozen negative file changed after preflight")
    if value.get("negative_cases") != case_definitions():
        raise CheckpointStop("negative case definitions changed after preflight")
    require_static_preflight()
    return cast(dict[str, Any], value)


def detailed_ledger_header() -> dict[str, Any]:
    ledger = rpc_success("ledger", ledger_index="validated", transactions=False, expand=False)[
        "ledger"
    ]
    return {
        "close_flags": int(ledger.get("close_flags", 0)),
        "close_time": int(ledger["close_time"]),
        "close_time_resolution": int(ledger["close_time_resolution"]),
        "closed": bool(ledger["closed"]),
        "ledger_hash": ledger["ledger_hash"],
        "ledger_index": int(ledger["ledger_index"]),
        "parent_close_time": int(ledger["parent_close_time"]),
    }


def phase_at(close_time: int, fixture: dict[str, Any]) -> str:
    if close_time <= int(fixture["subscription_date"]):
        return "Subscription"
    if close_time < int(fixture["redemption_date"]):
        return "Investment"
    return "Redemption"


def n6_state_assertions(
    *,
    before: dict[str, Any],
    after: dict[str, Any],
    observed: str | None,
    fee: int,
) -> list[StateAssertion]:
    from e6_oracle import negative_state_assertions

    before_protected = dict(before)
    after_protected = dict(after)
    before_account = dict(before_protected.pop("borrower_account"))
    after_account = dict(after_protected.pop("borrower_account"))
    before_balance = int(before_account.pop("Balance"))
    after_balance = int(after_account.pop("Balance"))
    before_sequence = int(before_account.pop("Sequence"))
    after_sequence = int(after_account.pop("Sequence"))
    return [
        *negative_state_assertions(
            observed_result=observed,
            expected_result="tecTOO_SOON",
            before={**before_protected, "borrower_account": before_account},
            after={**after_protected, "borrower_account": after_account},
        ),
        state_assertion("N6 borrower Sequence increment", after_sequence - before_sequence, 1),
        state_assertion("N6 XRP fee debit", before_balance - after_balance, fee),
    ]


def submit_negative_case(
    runner: Runner,
    *,
    case_id: str,
    label: str,
    signed: Any,
    signing_evidence: dict[str, Any],
    expected_result: str,
    state: Callable[[], dict[str, Any]],
    assertions: Callable[[dict[str, Any], dict[str, Any], str | None], list[StateAssertion]],
    expect_validated: bool,
    fixture: dict[str, Any],
    expected_phase: str,
) -> dict[str, Any]:
    runner.sequence += 1
    before = state()
    ledger_before = detailed_ledger_header()
    tx_hash = signed.get_hash()
    submission = rpc("submit", tx_blob=encode(signed.to_xrpl()), fail_hard=True)
    local_error = submission.get("error")
    submission_result = submission.get("engine_result")
    observed = (
        "local invalidTransaction" if local_error == "invalidTransaction" else submission_result
    )
    lookup: dict[str, Any] | None = None
    if local_error is None:
        runner.close(f"validate or establish rejection for {label}")
        lookup = rpc("tx", transaction=tx_hash, binary=False)
        if lookup.get("validated") is True and isinstance(lookup.get("meta"), dict):
            observed = lookup["meta"].get("TransactionResult")
    after = state()
    ledger_after = detailed_ledger_header()
    checks = assertions(before, after, cast(str | None, observed))
    observed_phase = phase_at(ledger_before["close_time"], fixture)
    checks.append(state_assertion(f"{case_id} execution phase", observed_phase, expected_phase))
    validated = lookup is not None and lookup.get("validated") is True
    timing: dict[str, Any] = {
        "subscription_date": fixture["subscription_date"],
        "redemption_date": fixture["redemption_date"],
        "parent_validated_ledger": ledger_before,
        "accepted_or_unchanged_ledger": ledger_after,
        "phase_at_parent_close": observed_phase,
    }
    if case_id == "N6":
        applying_time = ledger_before["close_time"] + ledger_after["close_time_resolution"]
        accepted_time = ledger_after["close_time"]
        transaction_date = lookup.get("date") if lookup is not None else None
        timing.update(
            {
                "source_derived_applying_view_header_close_time": applying_time,
                "accepted_ledger": ledger_after,
                "accepted_transaction_date": transaction_date,
                "prospective_oracle_start_date_anchor_used": False,
            }
        )
        checks.extend(
            [
                state_assertion("N6 parent phase", timing["phase_at_parent_close"], "Subscription"),
                state_assertion(
                    "N6 validated ledger increment",
                    ledger_after["ledger_index"],
                    ledger_before["ledger_index"] + 1,
                ),
                state_assertion("N6 accepted transaction date", transaction_date, accepted_time),
            ]
        )
    status = (
        "PASS"
        if observed == expected_result
        and all_pass(checks)
        and (validated if expect_validated else not validated)
        else "FAIL"
    )
    record = build_negative_case_record(
        case_id=case_id,
        label=label,
        expected_result=expected_result,
        observed_result=cast(str | None, observed),
        expect_validated=expect_validated,
        validated=validated,
        local_validation_result=cast(str | None, local_error),
        submission_result=cast(str | None, submission_result),
        validated_result=(
            (lookup.get("meta") or {}).get("TransactionResult") if lookup is not None else None
        ),
        submission_hash_if_any=tx_hash,
        before_state=before,
        after_state=after,
        state_assertions=checks,
        classification_status=status,
        public_signing_evidence=signing_evidence,
        submission_response=submission,
        validated_transaction_response=lookup,
        ledger_before=ledger_before,
        ledger_after=ledger_after,
        timing=timing,
    )
    write_json(runner.transactions_dir / f"{runner.sequence:02d}-{label}.json", record)
    runner.records.append(record)
    runner.write("experiment-summary.json", runner.records)
    if status != "PASS":
        runner.write("unexpected-result.json", record)
        raise CheckpointStop(
            f"{case_id}: expected {expected_result}, observed {observed}; result or frozen assertion failed"
        )
    return record


def execute_live(destination: Path) -> int:
    frozen_preflight = verify_preflight(destination)
    root_seed = os.environ.get(ROOT_SEED_ENV)
    if not root_seed:
        raise CheckpointStop(f"{ROOT_SEED_ENV} is absent from the runtime environment")
    transactions_dir = destination / "transactions"
    if transactions_dir.exists():
        raise CheckpointStop("transactions evidence directory already exists")
    transactions_dir.mkdir()
    runner = Runner(
        JsonRpcClient(os.environ.get("XRPL_RPC_URL", "http://127.0.0.1:5005")),
        destination,
        transactions_dir,
    )
    provenance = {
        "checkpoint": "1E-N",
        "git_commit_at_run_start": git_head(),
        "rippled_source_commit": SOURCE_COMMIT,
        "evidence_directory": f"evidence/{EVIDENCE_NAME}",
        "preflight_sha256": sha256_file(destination / "preflight.json"),
        "frozen_files": frozen_file_hashes(),
    }
    runner.write("provenance.json", provenance)

    server_info = rpc_success("server_info")["info"]
    server_info.pop("pubkey_node", None)
    feature = rpc_success("feature")
    amendments = rpc_success("ledger_entry", **amendments_ledger_entry_params())
    feature_validation = validate_feature_response({"result": feature})
    ledger_validation = validate_amendments_ledger_entry({"result": amendments})
    cross_check = cross_check_amendment_representations(feature_validation, ledger_validation)
    environment = {
        "server_info": server_info,
        "ledger": detailed_ledger_header(),
        "network_id": server_info.get("network_id"),
        "required_amendments": sorted(REQUIRED_AMENDMENTS),
        "feature_validation": feature_validation,
        "amendments_ledger_validation": ledger_validation,
        "amendment_cross_check": cross_check,
        "fresh_environment": True,
    }
    runner.write("feature-raw.json", feature)
    runner.write("amendments-ledger-entry.json", amendments)
    runner.write("environment-start.json", environment)
    if (
        server_info.get("build_version") != "3.4.0"
        or server_info.get("network_id") != NETWORK_ID
        or not feature_validation["valid"]
        or not ledger_validation["valid"]
        or not cross_check["valid"]
    ):
        runner.write("unexpected-result.json", environment)
        raise CheckpointStop("frozen environment invariants are absent")

    actors = create_actors(runner, Wallet.from_seed(root_seed))
    main_fixture = create_lending_fixture(
        runner, actors, label="negative_investment", subscription_offset=60
    )
    investment_entry = runner.advance_to_investment(
        main_fixture["subscription_date"], main_fixture["redemption_date"]
    )
    wrong_phase_fixture = create_lending_fixture(
        runner, actors, label="negative_subscription", subscription_offset=60
    )
    wrong_phase_header = ledger_header()
    if phase_at(wrong_phase_header["close_time"], wrong_phase_fixture) != "Subscription":
        raise CheckpointStop("N6 fixture is not in Subscription before negative binding")

    borrower = actors["borrower"]
    research = actors["research_bot"]
    policy = actors["credence_policy"]
    owner = actors["broker_owner"]
    actor_by_role = {"research_bot": research, "credence_policy": policy}

    main_broker = ledger_node(index=main_fixture["loan_broker_id"]) or {}
    main_index = loan_index(main_fixture["loan_broker_id"], int(main_broker["LoanSequence"]))
    n6_broker = ledger_node(index=wrong_phase_fixture["loan_broker_id"]) or {}
    n6_index = loan_index(wrong_phase_fixture["loan_broker_id"], int(n6_broker["LoanSequence"]))

    prepared: dict[str, dict[str, Any]] = {}
    case_constructions: dict[str, Any] = {}
    for definition in NEGATIVE_CASES:
        fixture = wrong_phase_fixture if definition.case_id == "N6" else main_fixture
        expected_index = n6_index if definition.case_id == "N6" else main_index
        broker_signer = None if definition.broker_mode == "missing" else owner
        signed, auth, finalized_body = finalize_and_sign(
            runner.client,
            loan_template(
                borrower=borrower.address,
                broker_owner=owner.address,
                broker_id=fixture["loan_broker_id"],
                principal=definition.principal,
            ),
            broker_owner=broker_signer,
            borrower_signers=[actor_by_role[role] for role in definition.borrower_signers],
            invalid_broker_signature=definition.broker_mode == "invalid",
            broker_signed_principal=definition.broker_signed_principal,
        )
        expected_signers = {
            actor_by_role[role].address: actor_by_role[role].public_key
            for role in definition.borrower_signers
        }
        auth_checks = negative_authorization_assertions(
            auth,
            borrower_account=borrower.address,
            expected_borrower_signers=expected_signers,
            broker_owner_account=owner.address,
            broker_owner_public_key=owner.public_key,
            expected_terms=signed_terms_from(signed.to_xrpl()),
            broker_mode=definition.broker_mode,
        )
        extra_checks: list[StateAssertion] = []
        broker_body_x: dict[str, Any] | None = None
        if definition.case_id == "N5":
            broker_body_x = dict(finalized_body)
            broker_body_x["PrincipalRequested"] = "1000"
            counterparty = signed.to_xrpl()["CounterpartySignature"]
            extra_checks = n5_mutation_assertions(
                broker_body_x=broker_body_x,
                submitted_body_y=finalized_body,
                broker_public_key=str(counterparty["SigningPubKey"]),
                broker_signature=str(counterparty["TxnSignature"]),
            )
        if not all_pass([*auth_checks, *extra_checks]):
            runner.write(
                "unexpected-result.json",
                {
                    "stage": "pre-submit authorization binding",
                    "case": definition.case_id,
                    "assertions": [*auth_checks, *extra_checks],
                },
            )
            raise CheckpointStop(f"{definition.case_id} authorization preflight failed")
        state_before = loan_state(
            actors=actors,
            fixture=fixture,
            expected_loan_index=expected_index,
            include_borrower_xrp=True,
        )
        prepared[definition.case_id] = {
            "definition": definition,
            "fixture": fixture,
            "expected_index": expected_index,
            "signed": signed,
            "auth": auth,
            "auth_checks": auth_checks,
            "extra_checks": extra_checks,
        }
        case_constructions[definition.case_id] = {
            "expected_result": definition.expected_result,
            "expect_validated": definition.expect_validated,
            "phase": definition.phase,
            "exact_signed_transaction": signed.to_xrpl(),
            "exact_finalized_body": finalized_body,
            "broker_body_x": broker_body_x,
            "expected_loan_index": expected_index,
            "expected_loan_absent_before": state_before["loan"] is None,
            "authorization_assertions_pass": all_pass([*auth_checks, *extra_checks]),
        }

    serialization_preflight = full_evidence_serialization_preflight(prepared)
    bindings = {
        "actors": {
            role: {"account": wallet.address, "public_key_hex": wallet.public_key}
            for role, wallet in actors.items()
        },
        "amendments": sorted(REQUIRED_AMENDMENTS),
        "assertion_spec_hash": provenance["frozen_files"]["E6-NEGATIVE-ASSERTIONS.md"],
        "case_constructions": case_constructions,
        "evidence_destination": provenance["evidence_directory"],
        "git_commit": provenance["git_commit_at_run_start"],
        "main_fixture": {**main_fixture, "investment_entry": investment_entry},
        "negative_oracle_hash": provenance["frozen_files"]["scripts/e6_negative_oracle.py"],
        "network_id": NETWORK_ID,
        "preflight_hash": provenance["preflight_sha256"],
        "rippled_source_commit": SOURCE_COMMIT,
        "rippled_version": server_info.get("build_version"),
        "runner_hash": provenance["frozen_files"]["scripts/run_checkpoint_1e_n.py"],
        "tests_hash": provenance["frozen_files"]["tests/test_checkpoint_1e_n.py"],
        "timing_spec_hash": provenance["frozen_files"]["E6-TIMING-ORACLE-v2.md"],
        "wrong_phase_fixture": {
            **wrong_phase_fixture,
            "phase_before_negative_execution": phase_at(
                wrong_phase_header["close_time"], wrong_phase_fixture
            ),
        },
        "full_evidence_serialization_preflight": serialization_preflight,
    }
    binding_checks = live_binding_assertions(bindings)
    require_live_bindings(bindings)
    if not all_pass(binding_checks):
        raise CheckpointStop("negative live binding assertions failed")
    runner.write(
        "live-bindings.json",
        {"result": "PASS", "bindings": bindings, "assertions": binding_checks},
    )
    runner.write(
        "prerequisite-state.json",
        {
            "main_fixture": main_fixture,
            "investment_entry": investment_entry,
            "wrong_phase_fixture": wrong_phase_fixture,
            "wrong_phase_header": wrong_phase_header,
            "main_state": loan_state(
                actors=actors,
                fixture=main_fixture,
                expected_loan_index=main_index,
                include_borrower_xrp=True,
            ),
            "wrong_phase_state": loan_state(
                actors=actors,
                fixture=wrong_phase_fixture,
                expected_loan_index=n6_index,
                include_borrower_xrp=True,
            ),
        },
    )

    results: dict[str, Any] = {}
    for definition in NEGATIVE_CASES:
        item = prepared[definition.case_id]
        fixture = cast(dict[str, Any], item["fixture"])
        expected_index = cast(str, item["expected_index"])

        def state(
            fixture: dict[str, Any] = fixture,
            expected_index: str = expected_index,
        ) -> dict[str, Any]:
            return loan_state(
                actors=actors,
                fixture=fixture,
                expected_loan_index=expected_index,
                include_borrower_xrp=True,
            )

        auth_checks = cast(list[StateAssertion], item["auth_checks"])
        extra_checks = cast(list[StateAssertion], item["extra_checks"])
        signed = item["signed"]
        if definition.case_id == "N6":

            def checks(
                before: dict[str, Any],
                after: dict[str, Any],
                observed: str | None,
                frozen_auth: list[StateAssertion] = auth_checks,
                frozen_extra: list[StateAssertion] = extra_checks,
                frozen_signed: Any = signed,
            ) -> list[StateAssertion]:
                return [
                    *frozen_auth,
                    *frozen_extra,
                    *n6_state_assertions(
                        before=before,
                        after=after,
                        observed=observed,
                        fee=int(frozen_signed.to_xrpl()["Fee"]),
                    ),
                ]
        else:

            def checks(
                before: dict[str, Any],
                after: dict[str, Any],
                observed: str | None,
                frozen_auth: list[StateAssertion] = auth_checks,
                frozen_extra: list[StateAssertion] = extra_checks,
                frozen_definition: Any = definition,
            ) -> list[StateAssertion]:
                return [
                    *frozen_auth,
                    *frozen_extra,
                    *negative_state_assertions(
                        observed_result=observed,
                        expected_result=frozen_definition.expected_result,
                        before=before,
                        after=after,
                    ),
                ]

        results[definition.case_id] = submit_negative_case(
            runner,
            case_id=definition.case_id,
            label=CASE_LABELS[definition.case_id],
            signed=signed,
            signing_evidence=item["auth"],
            expected_result=definition.expected_result,
            state=state,
            assertions=checks,
            expect_validated=definition.expect_validated,
            fixture=fixture,
            expected_phase=definition.phase,
        )

    checkpoint_result = {
        "checkpoint": "1E-N",
        "outcome": "PASS",
        "historical_e6": "FAIL",
        "positive_authorization_composition": "SUPPORTED from committed first-live evidence",
        "timing_behavior": "SOURCE_CONFORMANT",
        "historical_timing_oracle": "ORACLE_DEFECT",
        "prospective_timing_oracle": "FROZEN v2",
        "negative_results": {
            case_id: {
                "status": record["classification"]["status"],
                "expected": record["expected_result"],
                "observed": record["observed_result"],
            }
            for case_id, record in results.items()
        },
        "e6_authorization_architecture": "VALIDATED",
        "positive_loanset_submitted": False,
        "loan_pay_submitted": False,
        "e7_e8_e9": "NOT_ATTEMPTED",
        "prerequisite_transaction_count": runner.sequence - len(NEGATIVE_CASES),
        "negative_case_count": len(NEGATIVE_CASES),
        "ledger_close_count": len(runner.closes),
        "preflight": frozen_preflight["result"],
    }
    runner.write("checkpoint-result.json", checkpoint_result)
    create_manifest(destination, destination / "MANIFEST.json")
    print("Checkpoint 1E-N PASS: N1-N6 passed; no positive LoanSet or LoanPay was submitted.")
    return 0


def run_live(destination: Path) -> int:
    try:
        return execute_live(destination)
    except Exception as exc:
        if destination.exists():
            unexpected = destination / "unexpected-result.json"
            if not unexpected.exists():
                write_json(
                    unexpected,
                    {
                        "checkpoint": "1E-N",
                        "outcome": "STOP",
                        "exception_type": type(exc).__name__,
                        "message": str(exc),
                        "retry_attempted": False,
                    },
                )
            create_manifest(destination, destination / "MANIFEST.json")
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", required=True)
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args(argv)
    destination = evidence_destination(args.evidence_dir)
    try:
        if args.preflight_only:
            return write_preflight(destination)
        return run_live(destination)
    except CheckpointStop as exc:
        print(f"STOP: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
