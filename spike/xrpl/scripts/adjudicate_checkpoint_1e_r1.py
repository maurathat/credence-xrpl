#!/usr/bin/env python3
"""Adjudicate the committed E6 positive evidence without XRPL access."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from e6_oracle import (
    LOAN_TERM_FIELDS,
    authorization_assertions,
    positive_post_state_assertions,
    project_positive_oracle_terms,
    public_signing_evidence,
)
from harness import create_manifest, sha256_file, verify_manifest, write_json
from xrpl.core.binarycodec import decode

ROOT = Path(__file__).resolve().parents[1]
HISTORICAL = ROOT / "evidence" / "checkpoint-1e-live-01"
OUTPUT = ROOT / "evidence" / "checkpoint-1e-r1-corrected"


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise TypeError(f"{path} is not a JSON object")
    return value


def evidence_value(value: Any) -> Any:
    """Convert assertion-only containers into deterministic JSON values."""

    if isinstance(value, set):
        return sorted(evidence_value(item) for item in value)
    if isinstance(value, dict):
        return {key: evidence_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [evidence_value(item) for item in value]
    return value


def assertion_evidence(checks: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "name": check.name,
            "actual": evidence_value(check.actual),
            "expected": evidence_value(check.expected),
            "passed": check.passed,
        }
        for check in checks
    ]


def committed_states(
    bindings: dict[str, Any], observation: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    before = read_json(HISTORICAL / "prerequisite-state.json")["state"]
    after = deepcopy(before)
    post = observation["post_state"]
    after.update(
        {
            "loan": post["loan"],
            "borrower_account": {
                "Account": bindings["borrower_account"],
                "Balance": post["borrower"]["xrp_balance"],
                "OwnerCount": post["borrower"]["OwnerCount"],
                "Sequence": post["borrower"]["Sequence"],
            },
            "borrower_balance": post["borrower"]["tRLUSD_balance"],
            "broker_owner_balance": post["broker_owner_tRLUSD_balance"],
            "vault_pseudo_balance": post["vault"]["pseudo_account_tRLUSD_balance"],
            "vault": {**before["vault"], **post["vault"]},
            "loan_broker": {**before["loan_broker"], **post["loan_broker"]},
        }
    )
    return before, after


def main() -> int:
    if OUTPUT.exists():
        raise RuntimeError(f"refusing to overwrite {OUTPUT}")
    manifest_path = HISTORICAL / "MANIFEST.json"
    historical_manifest_hash_before = sha256_file(manifest_path)
    historical_errors_before = verify_manifest(HISTORICAL, manifest_path)
    if historical_errors_before:
        raise RuntimeError(f"historical evidence verification failed: {historical_errors_before}")

    bindings = read_json(HISTORICAL / "d12-live-bindings.json")["bindings"]
    observation = read_json(HISTORICAL / "positive-observation.json")
    actors = read_json(HISTORICAL / "actors-public.json")
    envelope = bindings["exact_loan_set_body"]
    old_projection = {field_name: envelope.get(field_name) for field_name in LOAN_TERM_FIELDS}
    old_projection["VaultID"] = bindings["vault_id"]
    reproduction: dict[str, Any]
    before, after = committed_states(bindings, observation)
    try:
        positive_post_state_assertions(
            validated_result="tesSUCCESS",
            before=before,
            after=after,
            terms=old_projection,
            expected_start_date=observation["validation"]["transaction_date"],
        )
    except Exception as exc:  # noqa: BLE001 - exact historical failure is evidence
        reproduction = {"exception_type": type(exc).__name__, "message": str(exc)}
    else:  # pragma: no cover - the committed defect must remain reproducible
        reproduction = {"exception_type": None, "message": None}

    corrected_projection = project_positive_oracle_terms(
        envelope,
        vault_id=bindings["vault_id"],
        intended_borrower=bindings["borrower_account"],
    )
    positive_checks = positive_post_state_assertions(
        validated_result=observation["validation"]["result"],
        before=before,
        after=after,
        terms=corrected_projection,
        expected_start_date=observation["validation"]["transaction_date"],
    )
    failed_positive = [check for check in positive_checks if not check.passed]
    positive_classification = (
        "POSITIVE_SUPPORTED" if not failed_positive else "POSITIVE_CONTRADICTED"
    )

    transaction = decode(observation["transaction"]["signed_transaction_blob"])
    signing = public_signing_evidence(transaction, observation["transaction"]["hash"])
    authorization_checks = authorization_assertions(
        signing,
        borrower_account=bindings["borrower_account"],
        expected_borrower_signers={
            actors["research_bot"]["account"]: actors["research_bot"]["public_key_hex"],
            actors["credence_policy"]["account"]: actors["credence_policy"]["public_key_hex"],
        },
        broker_owner_account=bindings["broker_owner_account"],
        broker_owner_public_key=actors["broker_owner"]["public_key_hex"],
        expected_terms={field_name: transaction.get(field_name) for field_name in LOAN_TERM_FIELDS},
    )
    borrower_signer_accounts = {
        row["account"] for row in signing["borrower_authorization"]["signers"]
    }
    broker_pseudo = before["loan_broker"]["Account"]
    pseudo_account_did_not_sign = (
        broker_pseudo not in borrower_signer_accounts
        and signing["broker_counterparty_authorization"]["counterparty"] != broker_pseudo
    )
    authorization_classification = (
        "SUPPORTED"
        if all(check.passed for check in authorization_checks) and pseudo_account_did_not_sign
        else "CONTRADICTED"
    )

    OUTPUT.mkdir(parents=True)
    write_json(
        OUTPUT / "adjudication-results.json",
        {
            "checkpoint": "1E-R1-CORRECTED",
            "mode": "OFFLINE_COMMITTED_EVIDENCE_ADJUDICATION",
            "new_xrpl_transactions": 0,
            "history": {
                "transaction_hash": observation["transaction"]["hash"],
                "validated_result": observation["validation"]["result"],
                "validated_ledger_index": observation["validation"]["ledger_index"],
                "finalized_envelope_account": envelope["Account"],
                "intended_borrower": bindings["borrower_account"],
            },
            "diagnosis": {
                "old_projection_fields": sorted(old_projection),
                "old_projection_has_account": "Account" in old_projection,
                "finalized_envelope_has_account": "Account" in envelope,
                "reproduction": reproduction,
                "root_cause": (
                    "The runner projected LOAN_TERM_FIELDS plus VaultID into the positive "
                    "oracle; LOAN_TERM_FIELDS excludes Account although the oracle consumes it."
                ),
            },
            "corrected_projection": corrected_projection,
            "positive_post_state": {
                "classification": positive_classification,
                "expected_start_date_source": "committed validated transaction date",
                "expected_start_date": observation["validation"]["transaction_date"],
                "assertions": assertion_evidence(positive_checks),
                "failed_assertions": assertion_evidence(failed_positive),
            },
            "authorization_domains": {
                "classification": authorization_classification,
                "assertions": assertion_evidence(authorization_checks),
                "loan_broker_pseudo_account": broker_pseudo,
                "pseudo_account_did_not_sign": pseudo_account_did_not_sign,
            },
            "result_state": {
                "E1": "PASS/FROZEN",
                "E2": "PASS/FROZEN",
                "E3": "PASS/RETIRED within tested scope",
                "E4": "PASS/RETIRED within tested scope",
                "E5": "PASS",
                "E6": (
                    "BLOCKED / INCOMPLETE"
                    if positive_classification == "POSITIVE_SUPPORTED"
                    else "FAIL"
                ),
                "E7-E9": "NOT_ATTEMPTED",
            },
            "remaining_negative_cases_executed": False,
            "loan_pay_executed": False,
        },
    )
    write_json(
        OUTPUT / "source-findings.json",
        {
            "rippled_version": "3.4.0",
            "source_commit": "4a4fded2eba11427c48ce3f24d9c1aea5e7a9d17",
            "findings": [
                {
                    "path": "src/libxrpl/tx/transactors/lending/LoanSet.cpp",
                    "lines": "282-304",
                    "finding": (
                        "preclaim reads tx[sfAccount]; when Counterparty equals the Loan "
                        "Broker owner, that Account is the borrower."
                    ),
                },
                {
                    "path": "src/libxrpl/tx/transactors/lending/LoanSet.cpp",
                    "lines": "226-229,650",
                    "finding": "Loan StartDate is the applying view header closeTime.",
                },
                {
                    "path": "src/libxrpl/protocol/STTx.cpp",
                    "lines": "249-280",
                    "finding": "transaction and counterparty signatures are checked by role.",
                },
            ],
        },
    )
    historical_manifest_hash_after = sha256_file(manifest_path)
    historical_errors_after = verify_manifest(HISTORICAL, manifest_path)
    write_json(
        OUTPUT / "historical-evidence-verification.json",
        {
            "manifest_sha256_before": historical_manifest_hash_before,
            "manifest_sha256_after": historical_manifest_hash_after,
            "manifest_unchanged": historical_manifest_hash_before == historical_manifest_hash_after,
            "verification_errors_before": historical_errors_before,
            "verification_errors_after": historical_errors_after,
        },
    )
    create_manifest(OUTPUT, OUTPUT / "MANIFEST.json")
    print(
        f"{positive_classification}; authorization {authorization_classification}; "
        "zero XRPL transactions"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
