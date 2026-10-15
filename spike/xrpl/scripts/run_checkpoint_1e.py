#!/usr/bin/env python3
"""Execute the frozen Checkpoint 1E E6 LoanSet composition experiment once."""

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

from amendment_validation import (
    REQUIRED_AMENDMENTS,
    amendments_ledger_entry_params,
    cross_check_amendment_representations,
    validate_amendments_ledger_entry,
    validate_feature_response,
)
from e5_oracle import parse_account_info_signer_state, signer_list_assertions
from e6_oracle import (
    LOAN_TERM_FIELDS,
    authorization_assertions,
    live_binding_assertions,
    loan_index,
    negative_state_assertions,
    positive_post_state_assertions,
    public_signing_evidence,
    require_live_bindings,
)
from harness import (
    StateAssertion,
    create_manifest,
    predicate_assertion,
    sanitize_response,
    sha256_file,
    state_assertion,
    write_json,
)
from xrpl.clients import JsonRpcClient
from xrpl.core.binarycodec import encode, encode_for_signing_counterparty
from xrpl.core.keypairs import sign as sign_message
from xrpl.models.amounts import IssuedCurrencyAmount
from xrpl.models.currencies import IssuedCurrency
from xrpl.models.transactions import (
    AccountSet,
    LoanBrokerCoverDeposit,
    LoanBrokerSet,
    LoanSet,
    Payment,
    SignerListSet,
    TrustSet,
    VaultCreate,
    VaultDeposit,
)
from xrpl.models.transactions.account_set import AccountSetAsfFlag
from xrpl.models.transactions.signer_list_set import SignerEntry
from xrpl.models.transactions.vault_create import VaultKind, WithdrawalPolicy
from xrpl.transaction import autofill, multisign, sign
from xrpl.wallet import Wallet

RPC_URL = os.environ.get("XRPL_RPC_URL", "http://127.0.0.1:5005")
ROOT_SEED_ENV = "XRPL_STANDALONE_GENESIS_SEED"
NETWORK_ID = 21_337
SOURCE_COMMIT = "4a4fded2eba11427c48ce3f24d9c1aea5e7a9d17"
TR_USD = b"tRLUSD".hex().upper().ljust(40, "0")
ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parents[1]
EVIDENCE_ROOT = ROOT / "evidence"
ASSERTIONS_PATH = ROOT / "E6-ASSERTIONS.md"
ORACLE_PATH = ROOT / "scripts" / "e6_oracle.py"
RUNNER_PATH = Path(__file__).resolve()
TEST_PATH = ROOT / "tests" / "test_checkpoint_1e.py"


class CheckpointStop(RuntimeError):
    """Fail closed after retaining every safe artifact available at the stop."""


class RpcFailure(RuntimeError):
    """A transport, server, or malformed RPC response."""


def rpc(method: str, **params: Any) -> dict[str, Any]:
    """Call the local standalone server without hiding error-shaped results."""

    import httpx

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
    return cast(dict[str, Any], sanitize_response(result))


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


def ledger_node(**selector: Any) -> dict[str, Any] | None:
    result = rpc("ledger_entry", ledger_index="validated", **selector)
    if result.get("status") == "error":
        return None
    node = result.get("node")
    return cast(dict[str, Any], node) if isinstance(node, dict) else None


def account_info(account: str, *, signer_lists: bool = False) -> dict[str, Any]:
    return rpc_success(
        "account_info",
        account=account,
        ledger_index="validated",
        signer_lists=signer_lists,
    )


def account_data(account: str) -> dict[str, Any] | None:
    result = rpc("account_info", account=account, ledger_index="validated")
    value = result.get("account_data")
    return cast(dict[str, Any], value) if isinstance(value, dict) else None


def account_objects(account: str) -> list[dict[str, Any]]:
    value = rpc_success("account_objects", account=account, ledger_index="validated").get(
        "account_objects", []
    )
    if not isinstance(value, list):
        raise RpcFailure("account_objects: response has no account_objects list")
    return cast(list[dict[str, Any]], value)


def trust_line(account: str, issuer: str) -> dict[str, Any] | None:
    result = rpc_success("account_lines", account=account, ledger_index="validated")
    for line in result.get("lines", []):
        if line.get("account") == issuer and line.get("currency") == TR_USD:
            return cast(dict[str, Any], line)
    return None


def line_balance(account: str, issuer: str) -> str:
    line = trust_line(account, issuer)
    return "0" if line is None else str(line["balance"])


def amount(issuer: str, value: str | int) -> IssuedCurrencyAmount:
    return IssuedCurrencyAmount(currency=TR_USD, issuer=issuer, value=str(value))


def extract_created(lookup: dict[str, Any], ledger_type: str) -> str:
    meta = lookup.get("meta") or {}
    for wrapper in meta.get("AffectedNodes", []):
        created = wrapper.get("CreatedNode")
        if isinstance(created, dict) and created.get("LedgerEntryType") == ledger_type:
            return str(created["LedgerIndex"])
    raise CheckpointStop(f"validated metadata has no created {ledger_type} node")


def git_head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def resolve_destination(value: str | Path | None) -> Path:
    if value is None:
        raise CheckpointStop("an explicit evidence destination is required")
    raw = Path(value)
    if not raw.parts or any(part == ".." for part in raw.parts):
        raise CheckpointStop("evidence destination path traversal is not allowed")
    root = EVIDENCE_ROOT.resolve()
    destination = (root / raw).resolve() if len(raw.parts) == 1 else (REPO_ROOT / raw).resolve()
    if destination == root or root not in destination.parents:
        raise CheckpointStop("evidence destination must be under spike/xrpl/evidence")
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise CheckpointStop(f"evidence destination is not empty: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    return destination


def public_actor(wallet: Wallet) -> dict[str, str]:
    return {"account": wallet.address, "public_key_hex": wallet.public_key}


def terms_from(transaction: dict[str, Any]) -> dict[str, Any]:
    return {field_name: transaction.get(field_name) for field_name in LOAN_TERM_FIELDS}


def all_pass(checks: list[StateAssertion]) -> bool:
    return all(check.passed for check in checks)


@dataclass
class Runner:
    client: JsonRpcClient
    evidence_dir: Path
    transactions_dir: Path
    records: list[dict[str, Any]] = field(default_factory=list)
    closes: list[dict[str, Any]] = field(default_factory=list)
    sequence: int = 0

    def write(self, filename: str, value: Any) -> None:
        write_json(self.evidence_dir / filename, value)

    def close(self, reason: str) -> dict[str, Any]:
        before = ledger_header()
        response = rpc_success("ledger_accept")
        after = ledger_header()
        record = {
            "reason": reason,
            "before": before,
            "ledger_accept_response": response,
            "after": after,
        }
        self.closes.append(record)
        self.write("ledger-closes.json", self.closes)
        return after

    def setup_transaction(
        self,
        *,
        label: str,
        transaction: Any,
        wallet: Wallet,
        state: Callable[[], dict[str, Any]],
        assertions: Callable[[dict[str, Any], dict[str, Any]], list[StateAssertion]],
    ) -> dict[str, Any]:
        self.sequence += 1
        before = state()
        filled = autofill(transaction, self.client)
        signed = sign(filled, wallet)
        tx_hash = signed.get_hash()
        submission = rpc("submit", tx_blob=encode(signed.to_xrpl()), fail_hard=True)
        self.close(f"validate prerequisite {label}")
        lookup = rpc("tx", transaction=tx_hash, binary=False)
        after = state()
        checks = assertions(before, after)
        validated_result = (lookup.get("meta") or {}).get("TransactionResult")
        record = {
            "experiment": "SETUP",
            "label": label,
            "expected_result": "tesSUCCESS",
            "submission_response": submission,
            "validated_transaction_response": lookup,
            "validated_result": validated_result,
            "before_state": before,
            "after_state": after,
            "state_assertions": checks,
            "classification": {
                "status": (
                    "PASS" if validated_result == "tesSUCCESS" and all_pass(checks) else "FAIL"
                )
            },
            "public_transaction": sanitize_response(signed.to_xrpl()),
        }
        write_json(self.transactions_dir / f"{self.sequence:02d}-{label}.json", record)
        self.records.append(record)
        self.write("experiment-summary.json", self.records)
        if record["classification"]["status"] != "PASS":
            self.write("unexpected-result.json", record)
            raise CheckpointStop(
                f"{label}: expected validated tesSUCCESS; result or state assertion failed"
            )
        return record

    def advance_to_investment(self, subscription: int, redemption: int) -> dict[str, Any]:
        observations: list[dict[str, Any]] = []
        while True:
            header = ledger_header()
            phase = (
                "Subscription"
                if header["close_time"] <= subscription
                else "Investment"
                if header["close_time"] < redemption
                else "Redemption"
            )
            observations.append({**header, "phase": phase})
            if phase == "Investment":
                self.write("investment-entry.json", observations)
                return observations[-1]
            if phase == "Redemption":
                self.write("unexpected-result.json", {"phase_observations": observations})
                raise CheckpointStop("prerequisite Vault reached Redemption before Investment")
            self.close("advance prerequisite Vault to Investment")


def signer_state(borrower: str) -> dict[str, Any]:
    return parse_account_info_signer_state(account_info(borrower, signer_lists=True))


def create_actors(runner: Runner, root: Wallet) -> dict[str, Wallet]:
    actors = {
        "issuer": Wallet.create(),
        "lender": Wallet.create(),
        "borrower": Wallet.create(),
        "research_bot": Wallet.create(),
        "credence_policy": Wallet.create(),
        "broker_owner": Wallet.create(),
    }
    runner.write(
        "actors-public.json", {name: public_actor(wallet) for name, wallet in actors.items()}
    )
    for name, wallet in actors.items():
        runner.setup_transaction(
            label=f"fund_{name}",
            transaction=Payment(
                account=root.address, destination=wallet.address, amount="1000000000"
            ),
            wallet=root,
            state=lambda wallet=wallet: {"account": account_data(wallet.address)},
            assertions=lambda _before, after, wallet=wallet, name=name: [
                state_assertion(
                    f"{name} AccountRoot identity",
                    (after["account"] or {}).get("Account"),
                    wallet.address,
                )
            ],
        )

    issuer = actors["issuer"]
    runner.setup_transaction(
        label="issuer_enable_default_ripple",
        transaction=AccountSet(
            account=issuer.address,
            set_flag=AccountSetAsfFlag.ASF_DEFAULT_RIPPLE,
        ),
        wallet=issuer,
        state=lambda: {"account": account_data(issuer.address)},
        assertions=lambda _before, after: [
            predicate_assertion(
                "issuer DefaultRipple enabled",
                bool(int((after["account"] or {}).get("Flags", 0)) & 0x00800000),
                True,
                bool(int((after["account"] or {}).get("Flags", 0)) & 0x00800000),
            )
        ],
    )
    for name in ("lender", "borrower", "broker_owner"):
        holder = actors[name]
        runner.setup_transaction(
            label=f"{name}_trust_tRLUSD",
            transaction=TrustSet(
                account=holder.address,
                limit_amount=amount(issuer.address, "1000000"),
            ),
            wallet=holder,
            state=lambda holder=holder: {"line": trust_line(holder.address, issuer.address)},
            assertions=lambda _before, after, name=name: [
                state_assertion(
                    f"{name} tRLUSD trust limit", (after["line"] or {}).get("limit"), "1000000"
                )
            ],
        )
    for name, value in (("lender", "200000"), ("broker_owner", "20000")):
        holder = actors[name]
        runner.setup_transaction(
            label=f"issue_tRLUSD_to_{name}",
            transaction=Payment(
                account=issuer.address,
                destination=holder.address,
                amount=amount(issuer.address, value),
            ),
            wallet=issuer,
            state=lambda holder=holder: {"balance": line_balance(holder.address, issuer.address)},
            assertions=lambda _before, after, name=name, value=value: [
                state_assertion(f"{name} tRLUSD balance", after["balance"], value)
            ],
        )
    borrower = actors["borrower"]
    research = actors["research_bot"]
    policy = actors["credence_policy"]
    runner.setup_transaction(
        label="borrower_set_2_of_2_signer_list",
        transaction=SignerListSet(
            account=borrower.address,
            signer_quorum=2,
            signer_entries=[
                SignerEntry(account=research.address, signer_weight=1),
                SignerEntry(account=policy.address, signer_weight=1),
            ],
        ),
        wallet=borrower,
        state=lambda: signer_state(borrower.address),
        assertions=lambda _before, after: signer_list_assertions(
            after,
            research_account=research.address,
            policy_account=policy.address,
        ),
    )
    return actors


def create_lending_fixture(
    runner: Runner,
    actors: dict[str, Wallet],
    *,
    label: str,
    subscription_offset: int,
) -> dict[str, Any]:
    issuer = actors["issuer"]
    lender = actors["lender"]
    owner = actors["broker_owner"]
    now = ledger_header()["close_time"]
    subscription = now + subscription_offset
    redemption = subscription + 300
    before_vaults = {
        item.get("index")
        for item in account_objects(owner.address)
        if item.get("LedgerEntryType") == "Vault"
    }

    def owner_objects_state() -> dict[str, Any]:
        return {"objects": account_objects(owner.address)}

    vault_record = runner.setup_transaction(
        label=f"{label}_create_closed_vault",
        transaction=VaultCreate(
            account=owner.address,
            asset=IssuedCurrency(currency=TR_USD, issuer=issuer.address),
            withdrawal_policy=WithdrawalPolicy.VAULT_STRATEGY_FIRST_COME_FIRST_SERVE,
            vault_kind=VaultKind.CLOSED,
            subscription_date=subscription,
            redemption_date=redemption,
        ),
        wallet=owner,
        state=owner_objects_state,
        assertions=lambda _before, after: [
            state_assertion(
                "one new Vault semantic identity",
                len(
                    [
                        item
                        for item in after["objects"]
                        if item.get("LedgerEntryType") == "Vault"
                        and item.get("index") not in before_vaults
                    ]
                ),
                1,
            )
        ],
    )
    vault_id = extract_created(vault_record["validated_transaction_response"], "Vault")
    vault = ledger_node(index=vault_id)
    vault_account = str((vault or {}).get("Account", ""))
    if not vault_account:
        raise CheckpointStop(f"{label}: Vault pseudo-account is absent")

    def vault_funding_state() -> dict[str, Any]:
        return {
            "vault": ledger_node(index=vault_id),
            "lender_balance": line_balance(lender.address, issuer.address),
            "vault_balance": line_balance(vault_account, issuer.address),
        }

    runner.setup_transaction(
        label=f"{label}_deposit_vault_assets",
        transaction=VaultDeposit(
            account=lender.address,
            vault_id=vault_id,
            amount=amount(issuer.address, "100000"),
        ),
        wallet=lender,
        state=vault_funding_state,
        assertions=lambda before, after: [
            state_assertion(
                "Vault AssetsAvailable deposit delta",
                int((after["vault"] or {})["AssetsAvailable"])
                - int((before["vault"] or {}).get("AssetsAvailable", "0")),
                100000,
            ),
            state_assertion(
                "Vault pseudo-account tRLUSD deposit delta",
                int(after["vault_balance"]) - int(before["vault_balance"]),
                100000,
            ),
        ],
    )
    before_brokers = {
        item.get("index")
        for item in account_objects(owner.address)
        if item.get("LedgerEntryType") == "LoanBroker"
    }
    broker_record = runner.setup_transaction(
        label=f"{label}_create_loan_broker",
        transaction=LoanBrokerSet(
            account=owner.address,
            vault_id=vault_id,
            debt_maximum="50000",
            cover_rate_minimum=10000,
            cover_rate_liquidation=25000,
            management_fee_rate=0,
        ),
        wallet=owner,
        state=owner_objects_state,
        assertions=lambda _before, after: [
            state_assertion(
                "one new LoanBroker semantic identity",
                len(
                    [
                        item
                        for item in after["objects"]
                        if item.get("LedgerEntryType") == "LoanBroker"
                        and item.get("index") not in before_brokers
                        and item.get("VaultID") == vault_id
                    ]
                ),
                1,
            )
        ],
    )
    broker_id = extract_created(broker_record["validated_transaction_response"], "LoanBroker")

    def cover_state() -> dict[str, Any]:
        return {
            "loan_broker": ledger_node(index=broker_id),
            "owner_balance": line_balance(owner.address, issuer.address),
        }

    runner.setup_transaction(
        label=f"{label}_deposit_first_loss_cover",
        transaction=LoanBrokerCoverDeposit(
            account=owner.address,
            loan_broker_id=broker_id,
            amount=amount(issuer.address, "10000"),
        ),
        wallet=owner,
        state=cover_state,
        assertions=lambda before, after: [
            state_assertion(
                "LoanBroker CoverAvailable delta",
                int((after["loan_broker"] or {}).get("CoverAvailable", "0"))
                - int((before["loan_broker"] or {}).get("CoverAvailable", "0")),
                10000,
            )
        ],
    )
    return {
        "vault_id": vault_id,
        "vault_account": vault_account,
        "loan_broker_id": broker_id,
        "subscription_date": subscription,
        "redemption_date": redemption,
    }


def loan_state(
    *,
    actors: dict[str, Wallet],
    fixture: dict[str, Any],
    expected_loan_index: str,
    include_borrower_xrp: bool = True,
) -> dict[str, Any]:
    issuer = actors["issuer"]
    borrower = actors["borrower"]
    owner = actors["broker_owner"]
    borrower_root = account_data(borrower.address) or {}
    projected_root = {
        "Account": borrower_root.get("Account"),
        "OwnerCount": borrower_root.get("OwnerCount", 0),
        "Sequence": borrower_root.get("Sequence"),
    }
    if include_borrower_xrp:
        projected_root["Balance"] = borrower_root.get("Balance")
    signer = signer_state(borrower.address)
    return {
        "loan": ledger_node(index=expected_loan_index),
        "borrower_account": projected_root,
        "borrower_balance": line_balance(borrower.address, issuer.address),
        "broker_owner_balance": line_balance(owner.address, issuer.address),
        "vault_pseudo_balance": line_balance(fixture["vault_account"], issuer.address),
        "signer_lists": signer.get("signer_lists"),
        "regular_key": signer.get("regular_key"),
        "vault": ledger_node(index=fixture["vault_id"]),
        "loan_broker": ledger_node(index=fixture["loan_broker_id"]),
    }


def loan_template(
    *,
    borrower: str,
    broker_owner: str,
    broker_id: str,
    principal: str = "1000",
) -> LoanSet:
    return LoanSet(
        account=borrower,
        loan_broker_id=broker_id,
        counterparty=broker_owner,
        principal_requested=principal,
        loan_origination_fee="0",
        loan_service_fee="0",
        late_payment_fee="0",
        close_payment_fee="0",
        overpayment_fee=0,
        interest_rate=0,
        late_interest_rate=0,
        close_interest_rate=0,
        overpayment_interest_rate=0,
        payment_total=1,
        payment_interval=60,
        grace_period=60,
        signing_pub_key="",
    )


def finalize_and_sign(
    client: JsonRpcClient,
    template: LoanSet,
    *,
    broker_owner: Wallet | None,
    borrower_signers: list[Wallet],
    invalid_broker_signature: bool = False,
    broker_signed_principal: str | None = None,
) -> tuple[LoanSet, dict[str, Any], dict[str, Any]]:
    filled = autofill(template, client, signers_count=len(borrower_signers))
    body = filled.to_xrpl()
    broker_body = dict(body)
    if broker_signed_principal is not None:
        broker_body["PrincipalRequested"] = broker_signed_principal
    if broker_owner is not None:
        payload = bytes.fromhex(encode_for_signing_counterparty(broker_body))
        signature = sign_message(payload, broker_owner.private_key)
        if invalid_broker_signature:
            signature = "00"
        body["CounterpartySignature"] = {
            "SigningPubKey": broker_owner.public_key,
            "TxnSignature": signature,
        }
    counterparty_signed = LoanSet.from_xrpl(body)
    if borrower_signers:
        partials = [
            sign(counterparty_signed, wallet, multisign=True) for wallet in borrower_signers
        ]
        signed = multisign(counterparty_signed, partials)
    else:
        signed = counterparty_signed
    public = signed.to_xrpl()
    return signed, public_signing_evidence(public, signed.get_hash()), filled.to_xrpl()


def submit_case(
    runner: Runner,
    *,
    label: str,
    signed: LoanSet,
    signing_evidence: dict[str, Any],
    expected_result: str,
    state: Callable[[], dict[str, Any]],
    checks: Callable[
        [dict[str, Any], dict[str, Any], str | None, dict[str, Any] | None], list[StateAssertion]
    ],
    expect_validated: bool,
) -> dict[str, Any]:
    runner.sequence += 1
    before = state()
    ledger_before = ledger_header()
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
    assertions = checks(before, after, cast(str | None, observed), lookup)
    validated = lookup is not None and lookup.get("validated") is True
    status = (
        "PASS"
        if observed == expected_result
        and all_pass(assertions)
        and (validated if expect_validated else True)
        else "FAIL"
    )
    record = {
        "experiment": "E6",
        "label": label,
        "expected_result": expected_result,
        "observed_result": observed,
        "local_validation_result": local_error,
        "submission_result": submission_result,
        "validated_result": (
            (lookup.get("meta") or {}).get("TransactionResult") if lookup is not None else None
        ),
        "submission_hash_if_any": tx_hash,
        "before_state": before,
        "after_state": after,
        "state_assertions": assertions,
        "classification": {"status": status},
        "public_signing_evidence": signing_evidence,
        "submission_response": submission,
        "validated_transaction_response": lookup,
        "ledger_before": ledger_before,
        "ledger_after": ledger_header(),
    }
    write_json(runner.transactions_dir / f"{runner.sequence:02d}-{label}.json", record)
    runner.records.append(record)
    runner.write("experiment-summary.json", runner.records)
    if status != "PASS":
        runner.write("unexpected-result.json", record)
        raise CheckpointStop(
            f"{label}: expected {expected_result}, observed {observed}; result or frozen assertion failed"
        )
    return record


def execute_checkpoint(evidence_dir: Path) -> int:
    root_seed = os.environ.get(ROOT_SEED_ENV)
    if not root_seed:
        print(f"Set {ROOT_SEED_ENV} for this process; it is never written to evidence.")
        return 2
    transactions_dir = evidence_dir / "transactions"
    transactions_dir.mkdir()
    runner = Runner(JsonRpcClient(RPC_URL), evidence_dir, transactions_dir)

    xrpl_package = Path(__import__("xrpl").__file__).resolve().parent
    provenance = {
        "git_commit_at_run_start": git_head(),
        "rippled_source_commit": SOURCE_COMMIT,
        "evidence_directory": "evidence/checkpoint-1e-live-01",
        "files": {
            "E6-ASSERTIONS.md": sha256_file(ASSERTIONS_PATH),
            "scripts/e6_oracle.py": sha256_file(ORACLE_PATH),
            "scripts/run_checkpoint_1e.py": sha256_file(RUNNER_PATH),
            "tests/test_checkpoint_1e.py": sha256_file(TEST_PATH),
            "xrpl/core/binarycodec/__init__.py": sha256_file(
                xrpl_package / "core" / "binarycodec" / "__init__.py"
            ),
            "xrpl/core/keypairs/main.py": sha256_file(
                xrpl_package / "core" / "keypairs" / "main.py"
            ),
            "xrpl/asyncio/transaction/main.py": sha256_file(
                xrpl_package / "asyncio" / "transaction" / "main.py"
            ),
        },
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
        "ledger": ledger_header(),
        "network_id": server_info.get("network_id"),
        "required_amendments": sorted(REQUIRED_AMENDMENTS),
        "feature_validation": feature_validation,
        "amendments_ledger_validation": ledger_validation,
        "amendment_cross_check": cross_check,
        "classification": "frozen E1 invariant verification only",
    }
    runner.write("feature-raw.json", feature)
    runner.write("amendments-ledger-entry.json", amendments)
    runner.write("environment-start.json", environment)
    if (
        server_info.get("network_id") != NETWORK_ID
        or not feature_validation["valid"]
        or not ledger_validation["valid"]
        or not cross_check["valid"]
    ):
        runner.write("unexpected-result.json", environment)
        raise CheckpointStop("frozen E1 invariants are absent from the fresh environment")

    actors = create_actors(runner, Wallet.from_seed(root_seed))
    fixture = create_lending_fixture(runner, actors, label="positive", subscription_offset=60)
    investment = runner.advance_to_investment(
        fixture["subscription_date"], fixture["redemption_date"]
    )
    borrower = actors["borrower"]
    research = actors["research_bot"]
    policy = actors["credence_policy"]
    owner = actors["broker_owner"]
    broker = ledger_node(index=fixture["loan_broker_id"]) or {}
    pre_sequence = int(broker["LoanSequence"])
    positive_index = loan_index(fixture["loan_broker_id"], pre_sequence)
    positive_signed, positive_auth, finalized_body = finalize_and_sign(
        runner.client,
        loan_template(
            borrower=borrower.address,
            broker_owner=owner.address,
            broker_id=fixture["loan_broker_id"],
        ),
        broker_owner=owner,
        borrower_signers=[research, policy],
    )
    bindings = {
        "amendments": sorted(REQUIRED_AMENDMENTS),
        "assertion_spec_hash": provenance["files"]["E6-ASSERTIONS.md"],
        "borrower_account": borrower.address,
        "broker_owner_account": owner.address,
        "credence_policy_account": policy.address,
        "evidence_destination": provenance["evidence_directory"],
        "exact_loan_set_body": finalized_body,
        "expected_loan_index": positive_index,
        "git_commit": provenance["git_commit_at_run_start"],
        "loan_broker_id": fixture["loan_broker_id"],
        "network_id": NETWORK_ID,
        "pre_loan_sequence": pre_sequence,
        "research_bot_account": research.address,
        "rippled_source_commit": SOURCE_COMMIT,
        "rippled_version": server_info.get("build_version"),
        "runner_hash": provenance["files"]["scripts/run_checkpoint_1e.py"],
        "tests_hash": provenance["files"]["tests/test_checkpoint_1e.py"],
        "vault_id": fixture["vault_id"],
    }
    binding_checks = live_binding_assertions(bindings)
    require_live_bindings(bindings)
    if not all_pass(binding_checks):
        raise CheckpointStop("D12 live binding failed before E6 submission")
    runner.write(
        "d12-live-bindings.json",
        {"result": "PASS", "bindings": bindings, "assertions": binding_checks},
    )
    runner.write(
        "prerequisite-state.json",
        {
            "fixture": fixture,
            "investment_entry": investment,
            "state": loan_state(
                actors=actors,
                fixture=fixture,
                expected_loan_index=positive_index,
            ),
        },
    )
    expected_terms = terms_from(positive_signed.to_xrpl())
    auth_checks = authorization_assertions(
        positive_auth,
        borrower_account=borrower.address,
        expected_borrower_signers={
            research.address: research.public_key,
            policy.address: policy.public_key,
        },
        broker_owner_account=owner.address,
        broker_owner_public_key=owner.public_key,
        expected_terms=expected_terms,
    )
    if not all_pass(auth_checks):
        runner.write(
            "unexpected-result.json",
            {"stage": "pre-submit authorization proof", "assertions": auth_checks},
        )
        raise CheckpointStop("frozen positive authorization proof failed before submission")

    def state_positive() -> dict[str, Any]:
        return loan_state(
            actors=actors,
            fixture=fixture,
            expected_loan_index=positive_index,
        )

    positive = submit_case(
        runner,
        label="e6_positive_dual_domain_loanset",
        signed=positive_signed,
        signing_evidence=positive_auth,
        expected_result="tesSUCCESS",
        state=state_positive,
        expect_validated=True,
        checks=lambda before, after, observed, lookup: [
            *auth_checks,
            *positive_post_state_assertions(
                validated_result=observed,
                before=before,
                after=after,
                terms={**expected_terms, "VaultID": fixture["vault_id"]},
                expected_start_date=int((lookup or {}).get("date", -1)),
            ),
        ],
    )

    negative_sequence = int((ledger_node(index=fixture["loan_broker_id"]) or {})["LoanSequence"])
    negative_index = loan_index(fixture["loan_broker_id"], negative_sequence)

    def state_negative() -> dict[str, Any]:
        return loan_state(
            actors=actors,
            fixture=fixture,
            expected_loan_index=negative_index,
        )

    def run_negative(
        label: str,
        expected: str,
        *,
        signers: list[Wallet],
        broker_signer: Wallet | None = owner,
        invalid_broker: bool = False,
        broker_signed_principal: str | None = None,
        principal: str = "1000",
    ) -> dict[str, Any]:
        signed, auth, _body = finalize_and_sign(
            runner.client,
            loan_template(
                borrower=borrower.address,
                broker_owner=owner.address,
                broker_id=fixture["loan_broker_id"],
                principal=principal,
            ),
            broker_owner=broker_signer,
            borrower_signers=signers,
            invalid_broker_signature=invalid_broker,
            broker_signed_principal=broker_signed_principal,
        )
        return submit_case(
            runner,
            label=label,
            signed=signed,
            signing_evidence=auth,
            expected_result=expected,
            state=state_negative,
            expect_validated=False,
            checks=lambda before, after, observed, _lookup: negative_state_assertions(
                observed_result=observed,
                expected_result=expected,
                before=before,
                after=after,
            ),
        )

    n1 = run_negative("n1_research_bot_alone", "tefBAD_QUORUM", signers=[research])
    n2 = run_negative("n2_credence_policy_alone", "tefBAD_QUORUM", signers=[policy])
    n3 = run_negative(
        "n3_missing_counterparty_signature",
        "temBAD_SIGNER",
        signers=[research, policy],
        broker_signer=None,
    )
    n4 = run_negative(
        "n4_invalid_counterparty_signature",
        "local invalidTransaction",
        signers=[research, policy],
        invalid_broker=True,
    )
    n5 = run_negative(
        "n5_altered_principal_reuses_counterparty_signature",
        "local invalidTransaction",
        signers=[research, policy],
        broker_signed_principal="1000",
        principal="1001",
    )

    n6_fixture = create_lending_fixture(
        runner,
        actors,
        label="n6_subscription",
        subscription_offset=60,
    )
    n6_broker = ledger_node(index=n6_fixture["loan_broker_id"]) or {}
    n6_index = loan_index(n6_fixture["loan_broker_id"], int(n6_broker["LoanSequence"]))
    n6_signed, n6_auth, _n6_body = finalize_and_sign(
        runner.client,
        loan_template(
            borrower=borrower.address,
            broker_owner=owner.address,
            broker_id=n6_fixture["loan_broker_id"],
        ),
        broker_owner=owner,
        borrower_signers=[research, policy],
    )

    def state_n6_full() -> dict[str, Any]:
        return loan_state(
            actors=actors,
            fixture=n6_fixture,
            expected_loan_index=n6_index,
            include_borrower_xrp=True,
        )

    def n6_checks(
        before: dict[str, Any],
        after: dict[str, Any],
        observed: str | None,
        _lookup: dict[str, Any] | None,
    ) -> list[StateAssertion]:
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
            state_assertion(
                "N6 XRP fee debit",
                before_balance - after_balance,
                int(n6_signed.to_xrpl()["Fee"]),
            ),
        ]

    n6 = submit_case(
        runner,
        label="n6_subscription_phase",
        signed=n6_signed,
        signing_evidence=n6_auth,
        expected_result="tecTOO_SOON",
        state=state_n6_full,
        checks=n6_checks,
        expect_validated=True,
    )
    result = {
        "checkpoint": "1E-LIVE-01",
        "outcome": "PASS",
        "e1": "PASS/FROZEN",
        "e2": "PASS/FROZEN",
        "e3": "PASS/RETIRED within tested scope",
        "e4": "PASS/RETIRED within tested scope",
        "e5": "PASS",
        "e6": "PASS",
        "e7_through_e9": "NOT_ATTEMPTED",
        "loan_pay_submitted": False,
        "d12_live_binding": "PASS",
        "positive": positive,
        "negatives": {"N1": n1, "N2": n2, "N3": n3, "N4": n4, "N5": n5, "N6": n6},
        "transaction_count_including_prerequisites": runner.sequence,
        "close_count": len(runner.closes),
    }
    runner.write("checkpoint-result.json", result)
    create_manifest(evidence_dir, evidence_dir / "MANIFEST.json")
    print("Checkpoint 1E-LIVE-01 completed: E6 PASS. LoanPay and E7-E9 were not attempted.")
    return 0


def run_checkpoint(destination: str | Path | None) -> int:
    evidence_dir = resolve_destination(destination)
    try:
        return execute_checkpoint(evidence_dir)
    except CheckpointStop:
        create_manifest(evidence_dir, evidence_dir / "MANIFEST.json")
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", required=True)
    args = parser.parse_args(argv)
    try:
        return run_checkpoint(args.evidence_dir)
    except CheckpointStop as exc:
        print(f"STOP: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
