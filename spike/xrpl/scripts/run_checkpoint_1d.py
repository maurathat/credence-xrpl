#!/usr/bin/env python3
"""Execute Checkpoint 1D E5 ordinary-Payment authorization exactly once."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import httpx
from amendment_validation import (
    REQUIRED_AMENDMENTS,
    amendments_ledger_entry_params,
    cross_check_amendment_representations,
    validate_amendments_ledger_entry,
    validate_feature_response,
)
from e5_oracle import (
    authorization_assertions,
    negative_payment_assertions,
    parse_account_info_signer_state,
    public_signing_evidence,
    regular_key_assertions,
    signer_list_assertions,
    successful_payment_assertions,
)
from harness import (
    StateAssertion,
    classify_transaction,
    create_manifest,
    resolve_transaction_result,
    sanitize_response,
    sha256_file,
    state_assertion,
    write_json,
)
from xrpl.clients import JsonRpcClient
from xrpl.models.transactions import Payment, SignerListSet
from xrpl.models.transactions.signer_list_set import SignerEntry
from xrpl.transaction import autofill, multisign, sign, submit
from xrpl.wallet import Wallet

RPC_URL = os.environ.get("XRPL_RPC_URL", "http://127.0.0.1:5005")
ROOT_SEED_ENV = "XRPL_STANDALONE_GENESIS_SEED"
NETWORK_ID = 21_337
ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parents[1]
EVIDENCE_ROOT = ROOT / "evidence"
ASSERTIONS_PATH = ROOT / "E5-ASSERTIONS.md"
ORACLE_PATH = ROOT / "scripts" / "e5_oracle.py"
RUNNER_PATH = Path(__file__).resolve()
TEST_PATH = ROOT / "tests" / "test_checkpoint_1d.py"
DESTINATION_TEST_PATH = ROOT / "tests" / "test_checkpoint_1d_destination.py"


class CheckpointStop(RuntimeError):
    """Fail closed after preserving the available evidence."""


class RpcFailure(RuntimeError):
    """A transport, server, or malformed RPC response."""


def resolve_evidence_destination(
    value: str | Path | None,
    *,
    evidence_root: Path = EVIDENCE_ROOT,
    repo_root: Path = REPO_ROOT,
) -> Path:
    """Resolve an explicit destination and reject escape from the evidence root."""

    if value is None:
        raise CheckpointStop("an explicit evidence destination is required")
    raw = Path(value)
    if not raw.parts or any(part == ".." for part in raw.parts):
        raise CheckpointStop("evidence destination path traversal is not allowed")

    root = evidence_root.resolve()
    if raw.is_absolute():
        destination = raw.resolve()
    elif len(raw.parts) == 1:
        destination = (root / raw).resolve()
    else:
        destination = (repo_root.resolve() / raw).resolve()

    if destination == root or root not in destination.parents:
        raise CheckpointStop("evidence destination must be under spike/xrpl/evidence")
    return destination


def prepare_evidence_destination(
    value: str | Path | None,
    *,
    evidence_root: Path = EVIDENCE_ROOT,
    repo_root: Path = REPO_ROOT,
) -> Path:
    """Create an empty selected destination without overwriting prior evidence."""

    destination = resolve_evidence_destination(
        value,
        evidence_root=evidence_root,
        repo_root=repo_root,
    )
    if destination.exists():
        if not destination.is_dir() or any(destination.iterdir()):
            raise CheckpointStop(
                f"evidence destination is not empty; refusing reuse: {destination}"
            )
    else:
        destination.mkdir(parents=True)
    return destination


def evidence_reference(evidence_dir: Path, *, xrpl_root: Path = ROOT) -> str:
    """Return the repository-relative evidence reference stored in run metadata."""

    try:
        return evidence_dir.resolve().relative_to(xrpl_root.resolve()).as_posix()
    except ValueError as exc:
        raise CheckpointStop("evidence metadata path is outside spike/xrpl") from exc


def finalize_evidence(evidence_dir: Path) -> None:
    """Generate the protected manifest inside the selected destination."""

    create_manifest(evidence_dir, evidence_dir / "MANIFEST.json")


def rpc(method: str, **params: Any) -> dict[str, Any]:
    try:
        response = httpx.post(
            RPC_URL,
            json={"method": method, "params": [params]},
            timeout=20,
        )
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        raise RpcFailure(f"{method}: {type(exc).__name__}: {exc}") from exc
    result = body.get("result")
    if not isinstance(result, dict):
        raise RpcFailure(f"{method}: response has no result object")
    return result


def rpc_success(method: str, **params: Any) -> dict[str, Any]:
    result = rpc(method, **params)
    if result.get("status") == "error":
        raise RpcFailure(f"{method}: {result.get('error')} — {result.get('error_message')}")
    return result


def ledger_header() -> dict[str, Any]:
    ledger = rpc_success("ledger", ledger_index="validated", transactions=False, expand=False)[
        "ledger"
    ]
    return {
        "close_time": int(ledger["close_time"]),
        "closed": bool(ledger["closed"]),
        "ledger_hash": ledger["ledger_hash"],
        "ledger_index": int(ledger["ledger_index"]),
    }


def account_data(account: str) -> dict[str, Any] | None:
    result = rpc("account_info", account=account, ledger_index="validated")
    if result.get("status") == "error":
        return None
    value = result.get("account_data")
    return cast(dict[str, Any], sanitize_response(value)) if isinstance(value, dict) else None


def authorization_state(borrower: str, recipient: str) -> dict[str, Any]:
    borrower_info = rpc_success(
        "account_info", account=borrower, ledger_index="validated", signer_lists=True
    )
    recipient_info = rpc_success("account_info", account=recipient, ledger_index="validated")
    signer_state = parse_account_info_signer_state(sanitize_response(borrower_info))
    signer_state["recipient_account"] = sanitize_response(recipient_info["account_data"])
    return signer_state


def git_head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@dataclass
class Runner:
    client: JsonRpcClient
    evidence_dir: Path
    transactions_dir: Path
    records: list[dict[str, Any]] = field(default_factory=list)
    closes: list[dict[str, Any]] = field(default_factory=list)
    sequence: int = 0

    def write_evidence(self, filename: str, value: Any) -> None:
        """Write a run artifact only inside this runner's selected destination."""

        write_json(self.evidence_dir / filename, value)

    def close(self, reason: str) -> dict[str, Any]:
        before = ledger_header()
        response = rpc_success("ledger_accept")
        after = ledger_header()
        record = {
            "reason": reason,
            "before": before,
            "ledger_accept_response": sanitize_response(response),
            "after": after,
        }
        self.closes.append(record)
        self.write_evidence("ledger-closes.json", self.closes)
        return after

    def execute(
        self,
        *,
        experiment: str,
        label: str,
        transaction: Any,
        expected_result: str,
        state: Callable[[], dict[str, Any]],
        assertions: Callable[
            [dict[str, Any], dict[str, Any], dict[str, Any]], list[StateAssertion]
        ],
        single_wallet: Wallet | None = None,
        multisign_wallets: list[Wallet] | None = None,
        authorization_checks: Callable[[dict[str, Any]], list[StateAssertion]] | None = None,
    ) -> dict[str, Any]:
        if (single_wallet is None) == (multisign_wallets is None):
            raise ValueError("choose exactly one signing mode")

        self.sequence += 1
        output = self.transactions_dir / f"{self.sequence:02d}-{label}.json"
        before = state()
        ledger_before = ledger_header()
        submission: dict[str, Any] | None = None
        lookup: dict[str, Any] | None = None
        tx_hash: str | None = None
        signing_evidence: dict[str, Any] | None = None
        client_exception: dict[str, str] | None = None
        transport_failure: dict[str, str] | None = None

        try:
            signer_count = 0 if multisign_wallets is None else len(multisign_wallets)
            filled = autofill(transaction, self.client, signers_count=signer_count)
            if multisign_wallets is None:
                signed = sign(filled, cast(Wallet, single_wallet))
            else:
                partials = [sign(filled, wallet, multisign=True) for wallet in multisign_wallets]
                signed = multisign(filled, partials)
            tx_hash = signed.get_hash()
            signed_json = signed.to_xrpl()
            signing_evidence = public_signing_evidence(signed_json, tx_hash)
            response = submit(signed, self.client, fail_hard=True)
            submission = sanitize_response(response.result)
        except Exception as exc:  # noqa: BLE001 - evidence must retain client failures
            client_exception = {"type": type(exc).__name__, "message": str(exc)}

        if client_exception is None:
            try:
                self.close(f"validate {label} or establish absence")
                if tx_hash is not None:
                    lookup = sanitize_response(rpc("tx", transaction=tx_hash, binary=False))
            except RpcFailure as exc:
                transport_failure = {"type": type(exc).__name__, "message": str(exc)}

        after = state()
        ledger_after = ledger_header()
        checks: list[StateAssertion] = []
        if signing_evidence is not None:
            checks.extend(assertions(before, after, signing_evidence))
            if authorization_checks is not None:
                checks.extend(authorization_checks(signing_evidence))

        unchanged = before == after
        resolution = resolve_transaction_result(
            submission_response=submission,
            submission_hash_if_any=tx_hash,
            validated_lookup=lookup,
            client_exception=client_exception,
            transport_or_server_failure=transport_failure,
        )
        classification = classify_transaction(
            expected_result=expected_result,
            resolution=resolution,
            before_state=before,
            after_state=after,
            assertions=checks,
            state_unchanged=unchanged if not expected_result.startswith("tes") else None,
        )
        record = {
            "experiment": experiment,
            "label": label,
            "expected_result": expected_result,
            "submission_result": resolution["submission_result"],
            "submission_hash_if_any": resolution["submission_hash_if_any"],
            "validated_lookup_status": resolution["validated_lookup_status"],
            "validated_result_if_any": resolution["validated_result_if_any"],
            "before_state": before,
            "after_state": after,
            "state_assertions": checks,
            "relevant_state_unchanged": unchanged,
            "classification": classification,
            "result_resolution": resolution,
            "public_signing_evidence": signing_evidence,
            "submission_response": submission,
            "validated_transaction_response": lookup,
            "ledger_before": ledger_before,
            "ledger_after": ledger_after,
        }
        write_json(output, record)
        self.records.append(record)
        self.write_evidence("experiment-summary.json", self.records)
        if classification["status"] != "PASS":
            self.write_evidence("unexpected-result.json", record)
            raise CheckpointStop(
                f"{label}: expected {expected_result}, observed "
                f"{resolution['authoritative_result']}; result, evidence, or state assertion failed"
            )
        return record


def execute_checkpoint(evidence_dir: Path) -> int:
    """Execute the unchanged E5 experiment into a prepared destination."""

    root_seed = os.environ.get(ROOT_SEED_ENV)
    if not root_seed:
        print(f"Set {ROOT_SEED_ENV} for this process; it is never written to evidence.")
        return 2

    transactions_dir = evidence_dir / "transactions"
    transactions_dir.mkdir()
    client = JsonRpcClient(RPC_URL)
    runner = Runner(
        client=client,
        evidence_dir=evidence_dir,
        transactions_dir=transactions_dir,
    )

    provenance = {
        "git_commit_at_run_start": git_head(),
        "files": {
            "E5-ASSERTIONS.md": sha256_file(ASSERTIONS_PATH),
            "scripts/e5_oracle.py": sha256_file(ORACLE_PATH),
            "scripts/run_checkpoint_1d.py": sha256_file(RUNNER_PATH),
            "tests/test_checkpoint_1d.py": sha256_file(TEST_PATH),
            "tests/test_checkpoint_1d_destination.py": sha256_file(DESTINATION_TEST_PATH),
        },
        "evidence_directory": evidence_reference(evidence_dir),
        "signer_list_parser_binding": {
            "path": "scripts/e5_oracle.py",
            "sha256": sha256_file(ORACLE_PATH),
        },
        "independent_audit_commit": "593eb79694e86238af44785bf8d63e9a3e8086c9",
        "independent_audit_verdict": "PASS WITH CONDITIONS",
    }
    write_json(evidence_dir / "provenance.json", provenance)

    server_info = sanitize_response(rpc_success("server_info")["info"])
    server_info.pop("pubkey_node", None)
    feature = sanitize_response(rpc_success("feature"))
    amendments = sanitize_response(rpc_success("ledger_entry", **amendments_ledger_entry_params()))
    feature_validation = validate_feature_response({"result": feature})
    ledger_validation = validate_amendments_ledger_entry({"result": amendments})
    amendment_cross_check = cross_check_amendment_representations(
        feature_validation, ledger_validation
    )
    environment = {
        "provenance": provenance,
        "server_info": server_info,
        "network_id": server_info.get("network_id"),
        "ledger": ledger_header(),
        "required_amendments": sorted(REQUIRED_AMENDMENTS),
        "feature_validation": feature_validation,
        "amendments_ledger_validation": ledger_validation,
        "amendment_cross_check": amendment_cross_check,
        "classification": "frozen E1 invariant verification, not a new E1 experiment",
    }
    write_json(evidence_dir / "feature-raw.json", feature)
    write_json(evidence_dir / "amendments-ledger-entry.json", amendments)
    write_json(evidence_dir / "environment-start.json", environment)
    if server_info.get("network_id") != NETWORK_ID:
        write_json(evidence_dir / "unexpected-result.json", environment)
        raise CheckpointStop("fresh runtime network ID is not 21337")

    root = Wallet.from_seed(root_seed)
    borrower = Wallet.create()
    research = Wallet.create()
    policy = Wallet.create()
    recipient = Wallet.create()
    actors = {
        "borrower": {"account": borrower.address, "public_key_hex": borrower.public_key},
        "research_bot_signer": {
            "account": research.address,
            "public_key_hex": research.public_key,
        },
        "credence_policy_signer": {
            "account": policy.address,
            "public_key_hex": policy.public_key,
        },
        "payment_recipient": {
            "account": recipient.address,
            "public_key_hex": recipient.public_key,
        },
    }
    write_json(evidence_dir / "actors-public.json", actors)

    for name, wallet in (
        ("borrower", borrower),
        ("research_bot_signer", research),
        ("credence_policy_signer", policy),
        ("payment_recipient", recipient),
    ):
        runner.execute(
            experiment="SETUP",
            label=f"fund_{name}",
            transaction=Payment(
                account=root.address,
                destination=wallet.address,
                amount="1000000000",
            ),
            single_wallet=root,
            expected_result="tesSUCCESS",
            state=lambda wallet=wallet: {"account": account_data(wallet.address)},
            assertions=lambda _before, after, _evidence, wallet=wallet, name=name: [
                state_assertion(
                    f"{name} AccountRoot identity",
                    (after.get("account") or {}).get("Account"),
                    wallet.address,
                ),
                state_assertion(
                    f"{name} initial XRP balance",
                    (after.get("account") or {}).get("Balance"),
                    "1000000000",
                ),
            ],
        )

    def setup_state() -> dict[str, Any]:
        return authorization_state(borrower.address, recipient.address)

    signer_setup = runner.execute(
        experiment="SETUP",
        label="borrower_set_2_of_2_signer_list",
        transaction=SignerListSet(
            account=borrower.address,
            signer_quorum=2,
            signer_entries=[
                SignerEntry(account=research.address, signer_weight=1),
                SignerEntry(account=policy.address, signer_weight=1),
            ],
        ),
        single_wallet=borrower,
        expected_result="tesSUCCESS",
        state=setup_state,
        assertions=lambda before, after, _evidence: [
            *signer_list_assertions(
                after,
                research_account=research.address,
                policy_account=policy.address,
            ),
            *regular_key_assertions(after),
            state_assertion(
                "SignerListSet borrower sequence delta",
                int((after["borrower_account"] or {}).get("Sequence", 0))
                - int((before["borrower_account"] or {}).get("Sequence", 0)),
                1,
            ),
        ],
    )
    write_json(
        evidence_dir / "signer-list-state.json",
        {
            "validated_transaction": signer_setup["validated_transaction_response"],
            "validated_state": setup_state(),
            "assertions": signer_setup["state_assertions"],
        },
    )

    def auth_checks(expected: dict[str, str]) -> Callable[[dict[str, Any]], list[StateAssertion]]:
        return lambda evidence: authorization_assertions(
            evidence,
            borrower_account=borrower.address,
            expected_signers=expected,
            borrower_master_public_key=borrower.public_key,
        )

    def negative_checks(
        before: dict[str, Any], after: dict[str, Any], _evidence: dict[str, Any]
    ) -> list[StateAssertion]:
        return [
            *negative_payment_assertions(before, after),
            *signer_list_assertions(
                after,
                research_account=research.address,
                policy_account=policy.address,
            ),
            *regular_key_assertions(after),
        ]

    e5_a = runner.execute(
        experiment="E5-A",
        label="research_bot_alone_rejected",
        transaction=Payment(account=borrower.address, destination=recipient.address, amount="1000"),
        multisign_wallets=[research],
        expected_result="tefBAD_QUORUM",
        state=setup_state,
        assertions=negative_checks,
        authorization_checks=auth_checks({research.address: research.public_key}),
    )
    e5_b = runner.execute(
        experiment="E5-B",
        label="credence_policy_alone_rejected",
        transaction=Payment(account=borrower.address, destination=recipient.address, amount="1000"),
        multisign_wallets=[policy],
        expected_result="tefBAD_QUORUM",
        state=setup_state,
        assertions=negative_checks,
        authorization_checks=auth_checks({policy.address: policy.public_key}),
    )

    def success_checks(
        before: dict[str, Any], after: dict[str, Any], evidence: dict[str, Any]
    ) -> list[StateAssertion]:
        fields = evidence["transaction_fields"]
        return [
            *successful_payment_assertions(
                before,
                after,
                amount=int(fields["Amount"]),
                fee=int(fields["Fee"]),
            ),
            *signer_list_assertions(
                after,
                research_account=research.address,
                policy_account=policy.address,
            ),
            *regular_key_assertions(after),
        ]

    e5_c = runner.execute(
        experiment="E5-C",
        label="research_bot_and_credence_multisign_payment",
        transaction=Payment(account=borrower.address, destination=recipient.address, amount="1000"),
        multisign_wallets=[research, policy],
        expected_result="tesSUCCESS",
        state=setup_state,
        assertions=success_checks,
        authorization_checks=auth_checks(
            {research.address: research.public_key, policy.address: policy.public_key}
        ),
    )
    e5_d = runner.execute(
        experiment="E5-D",
        label="owner_master_key_payment",
        transaction=Payment(account=borrower.address, destination=recipient.address, amount="2000"),
        single_wallet=borrower,
        expected_result="tesSUCCESS",
        state=setup_state,
        assertions=success_checks,
        authorization_checks=auth_checks({}),
    )

    final_state = setup_state()
    e5_e_checks = [
        *regular_key_assertions(final_state),
        *signer_list_assertions(
            final_state,
            research_account=research.address,
            policy_account=policy.address,
        ),
    ]
    e5_e = {
        "experiment": "E5-E",
        "result": "PASS" if all(item.passed for item in e5_e_checks) else "FAIL",
        "validated_state": final_state,
        "state_assertions": e5_e_checks,
        "transaction_submitted": False,
        "precise_claim": "No regular key is configured in the tested borrower state.",
    }
    write_json(evidence_dir / "e5-e-regular-key-absence.json", e5_e)
    if e5_e["result"] != "PASS":
        write_json(evidence_dir / "unexpected-result.json", e5_e)
        raise CheckpointStop("E5-E regular-key or signer-list assertion failed")

    result = {
        "checkpoint": "1D",
        "outcome": "PASS",
        "e1": "PASS/FROZEN",
        "e2": "PASS/FROZEN",
        "e3": "PASS/RETIRED within tested scope",
        "e4": "PASS/RETIRED within tested scope",
        "e5": "PASS",
        "e6_through_e9": "NOT_ATTEMPTED",
        "no_lending_transaction_submitted": True,
        "steps": {
            "E5-A": e5_a,
            "E5-B": e5_b,
            "E5-C": e5_c,
            "E5-D": e5_d,
            "E5-E": e5_e,
        },
        "transaction_count": runner.sequence,
        "close_count": len(runner.closes),
        "evidence_directory": evidence_reference(evidence_dir),
        "provenance": provenance,
    }
    write_json(evidence_dir / "checkpoint-result.json", result)
    finalize_evidence(evidence_dir)
    print("Checkpoint 1D completed: E5 PASS. E6-E9 were not attempted.")
    return 0


def run_checkpoint(evidence_dir: str | Path | None) -> int:
    """Prepare the explicit output directory and preserve a manifest on STOP."""

    destination = prepare_evidence_destination(evidence_dir)
    try:
        return execute_checkpoint(destination)
    except CheckpointStop:
        finalize_evidence(destination)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--evidence-dir",
        help="required output directory under spike/xrpl/evidence/",
    )
    args = parser.parse_args(argv)
    try:
        return run_checkpoint(args.evidence_dir)
    except CheckpointStop as exc:
        print(f"STOP: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
