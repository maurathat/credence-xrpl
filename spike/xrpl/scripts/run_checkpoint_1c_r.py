#!/usr/bin/env python3
"""Run the bounded Checkpoint 1C-R E3/E4 recovery experiment once."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from amendment_validation import REQUIRED_AMENDMENTS, validate_feature_response
from harness import (
    StateAssertion,
    classify_transaction,
    cover_transition_assertions,
    create_manifest,
    loan_broker_create_transition_assertions,
    predicate_assertion,
    public_transaction,
    resolve_transaction_result,
    sanitize_response,
    state_assertion,
    vault_create_transition_assertions,
    write_json,
)
from xrpl.clients import JsonRpcClient
from xrpl.models.amounts import IssuedCurrencyAmount
from xrpl.models.currencies import IssuedCurrency
from xrpl.models.transactions import (
    AccountSet,
    LoanBrokerCoverDeposit,
    LoanBrokerSet,
    Payment,
    TrustSet,
    VaultCreate,
    VaultDeposit,
    VaultWithdraw,
)
from xrpl.models.transactions.account_set import AccountSetAsfFlag
from xrpl.models.transactions.vault_create import VaultKind, WithdrawalPolicy
from xrpl.transaction import autofill, sign, submit
from xrpl.wallet import Wallet

RPC_URL = os.environ.get("XRPL_RPC_URL", "http://127.0.0.1:5005")
ROOT_SEED_ENV = "XRPL_STANDALONE_GENESIS_SEED"
NETWORK_ID = 21_337
TR_USD = b"tRLUSD".hex().upper().ljust(40, "0")
ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence" / "checkpoint-1c-r"
TRANSACTIONS = EVIDENCE / "transactions"


class CheckpointStop(RuntimeError):
    """A fail-closed stop after evidence has been written."""


class RpcFailure(RuntimeError):
    """A transport, server, or malformed RPC response."""


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


def ledger_node(**selector: Any) -> dict[str, Any] | None:
    result = rpc("ledger_entry", ledger_index="validated", **selector)
    if result.get("status") == "error":
        return None
    node = result.get("node")
    return sanitize_response(node) if isinstance(node, dict) else None


def account_data(account: str) -> dict[str, Any] | None:
    result = rpc("account_info", account=account, ledger_index="validated")
    if result.get("status") == "error":
        return None
    value = result.get("account_data")
    return sanitize_response(value) if isinstance(value, dict) else None


def account_objects(account: str) -> list[dict[str, Any]]:
    result = rpc_success("account_objects", account=account, ledger_index="validated")
    return sanitize_response(result.get("account_objects", []))


def trust_line(account: str, issuer: str) -> dict[str, Any] | None:
    result = rpc_success("account_lines", account=account, ledger_index="validated")
    for line in result.get("lines", []):
        if line.get("account") == issuer and line.get("currency") == TR_USD:
            return sanitize_response(line)
    return None


def line_balance(account: str, issuer: str) -> str:
    line = trust_line(account, issuer)
    return "0" if line is None else str(line["balance"])


def amount(issuer: str, value: str | int) -> IssuedCurrencyAmount:
    return IssuedCurrencyAmount(currency=TR_USD, issuer=issuer, value=str(value))


def extract_created(meta: dict[str, Any], ledger_type: str) -> str:
    for wrapper in meta.get("AffectedNodes", []):
        created = wrapper.get("CreatedNode")
        if isinstance(created, dict) and created.get("LedgerEntryType") == ledger_type:
            return str(created["LedgerIndex"])
    raise CheckpointStop(f"validated metadata has no created {ledger_type} node")


def phase(close_time: int, subscription: int, redemption: int) -> str:
    if close_time <= subscription:
        return "Subscription"
    if close_time < redemption:
        return "Investment"
    return "Redemption"


def numeric(node: dict[str, Any] | None, field_name: str) -> int:
    return int((node or {}).get(field_name, "0"))


@dataclass
class Runner:
    client: JsonRpcClient
    records: list[dict[str, Any]] = field(default_factory=list)
    closes: list[dict[str, Any]] = field(default_factory=list)
    sequence: int = 0

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
        write_json(EVIDENCE / "ledger-closes.json", self.closes)
        return after

    def execute(
        self,
        *,
        experiment: str,
        label: str,
        transaction: Any,
        wallet: Wallet,
        expected_result: str,
        state: Callable[[], dict[str, Any]],
        assertions: Callable[[dict[str, Any], dict[str, Any]], list[StateAssertion]],
    ) -> dict[str, Any]:
        self.sequence += 1
        output = TRANSACTIONS / f"{self.sequence:02d}-{label}.json"
        before = state()
        ledger_before = ledger_header()
        signed = None
        submission: dict[str, Any] | None = None
        tx_hash: str | None = None
        client_exception: dict[str, str] | None = None
        transport_failure: dict[str, Any] | None = None
        lookup: dict[str, Any] | None = None
        public_tx: dict[str, Any] | None = None

        try:
            filled = autofill(transaction, self.client)
            signed = sign(filled, wallet)
            tx_hash = signed.get_hash()
            public_tx = public_transaction(signed.to_xrpl())
            response = submit(signed, self.client, fail_hard=True)
            submission = sanitize_response(response.result)
        except Exception as exc:  # noqa: BLE001 - preserve client failure as evidence
            client_exception = {"type": type(exc).__name__, "message": str(exc)}

        if client_exception is None:
            try:
                self.close(f"validate {label} or establish absence from validated ledger")
                if tx_hash is not None:
                    lookup = sanitize_response(rpc("tx", transaction=tx_hash, binary=False))
            except RpcFailure as exc:
                transport_failure = {"type": type(exc).__name__, "message": str(exc)}

        after = state()
        ledger_after = ledger_header()
        checks = assertions(before, after)
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
            "state_assertion": classification["state_assertion"],
            "state_assertions": checks,
            "relevant_state_unchanged": unchanged,
            "classification": classification,
            "result_resolution": resolution,
            "canonical_public_transaction": public_tx,
            "submission_response": submission,
            "validated_transaction_response": lookup,
            "ledger_before": ledger_before,
            "ledger_after": ledger_after,
        }
        write_json(output, record)
        self.records.append(record)
        write_json(EVIDENCE / "experiment-summary.json", self.records)
        if classification["status"] != "PASS":
            write_json(EVIDENCE / "unexpected-result.json", record)
            observed = resolution["authoritative_result"]
            raise CheckpointStop(
                f"{label}: expected {expected_result}, observed {observed}; "
                "result or state assertion failed"
            )
        return record

    def advance_to(
        self,
        *,
        label: str,
        subscription: int,
        redemption: int,
        wanted_phase: str,
        vault_id: str,
    ) -> dict[str, Any]:
        observations = []
        while True:
            header = ledger_header()
            observed_phase = phase(header["close_time"], subscription, redemption)
            observations.append({**header, "phase": observed_phase})
            if observed_phase == wanted_phase:
                break
            self.close(f"{label}: scripted phase transition")
        result = {
            "target_phase": wanted_phase,
            "subscription_date": subscription,
            "redemption_date": redemption,
            "observations": observations,
            "entry": observations[-1],
            "vault": ledger_node(index=vault_id),
        }
        write_json(EVIDENCE / f"{label}.json", result)
        return result


def vault_state(vault_id: str, lender: str, issuer: str) -> dict[str, Any]:
    vault = ledger_node(index=vault_id)
    issuance_id = None if vault is None else vault.get("ShareMPTID")
    issuance = None if issuance_id is None else ledger_node(mpt_issuance=issuance_id)
    holder = (
        None
        if issuance_id is None
        else ledger_node(mptoken={"mpt_issuance_id": issuance_id, "account": lender})
    )
    return {
        "vault": vault,
        "share_issuance": issuance,
        "lender_share": holder,
        "lender_tRLUSD_balance": line_balance(lender, issuer),
    }


def vault_checks(
    *,
    asset_delta: int,
    balance_delta: int,
    share_delta: int,
) -> Callable[[dict[str, Any], dict[str, Any]], list[StateAssertion]]:
    def checks(before: dict[str, Any], after: dict[str, Any]) -> list[StateAssertion]:
        return [
            state_assertion(
                "vault AssetsTotal delta",
                numeric(after["vault"], "AssetsTotal") - numeric(before["vault"], "AssetsTotal"),
                asset_delta,
            ),
            state_assertion(
                "vault AssetsAvailable delta",
                numeric(after["vault"], "AssetsAvailable")
                - numeric(before["vault"], "AssetsAvailable"),
                asset_delta,
            ),
            state_assertion(
                "lender tRLUSD balance delta",
                int(after["lender_tRLUSD_balance"]) - int(before["lender_tRLUSD_balance"]),
                balance_delta,
            ),
            state_assertion(
                "share issuance delta",
                numeric(after["share_issuance"], "OutstandingAmount")
                - numeric(before["share_issuance"], "OutstandingAmount"),
                share_delta,
            ),
            state_assertion(
                "lender share delta",
                numeric(after["lender_share"], "MPTAmount")
                - numeric(before["lender_share"], "MPTAmount"),
                share_delta,
            ),
        ]

    return checks


def unchanged_checks(before: dict[str, Any], after: dict[str, Any]) -> list[StateAssertion]:
    return [state_assertion("all relevant ledger state unchanged", after, before)]


def setup_fixture(runner: Runner, root: Wallet) -> dict[str, Wallet]:
    """Create prerequisites on the fresh ledger without reclassifying frozen E2."""

    actors = {
        "issuer": Wallet.create(),
        "lender": Wallet.create(),
        "borrower": Wallet.create(),
        "loan_broker": Wallet.create(),
    }
    write_json(
        EVIDENCE / "actors-public.json",
        {
            "purpose": "fresh-ledger prerequisite fixture; not an E2 rerun",
            **{name: wallet.address for name, wallet in actors.items()},
        },
    )
    for name, wallet in actors.items():
        runner.execute(
            experiment="SETUP",
            label=f"fund_{name}",
            transaction=Payment(
                account=root.address, destination=wallet.address, amount="1000000000"
            ),
            wallet=root,
            expected_result="tesSUCCESS",
            state=lambda wallet=wallet: {"account": account_data(wallet.address)},
            assertions=lambda _before, after, wallet=wallet, name=name: [
                state_assertion(
                    f"{name} AccountRoot created",
                    (after["account"] or {}).get("Account"),
                    wallet.address,
                )
            ],
        )

    issuer = actors["issuer"]
    runner.execute(
        experiment="SETUP",
        label="issuer_enable_default_ripple",
        transaction=AccountSet(
            account=issuer.address, set_flag=AccountSetAsfFlag.ASF_DEFAULT_RIPPLE
        ),
        wallet=issuer,
        expected_result="tesSUCCESS",
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

    for name in ("lender", "borrower", "loan_broker"):
        holder = actors[name]
        runner.execute(
            experiment="SETUP",
            label=f"{name}_trust_tRLUSD",
            transaction=TrustSet(
                account=holder.address, limit_amount=amount(issuer.address, "1000000")
            ),
            wallet=holder,
            expected_result="tesSUCCESS",
            state=lambda holder=holder: {"trust_line": trust_line(holder.address, issuer.address)},
            assertions=lambda _before, after, name=name: [
                state_assertion(
                    f"{name} tRLUSD trust line limit",
                    (after["trust_line"] or {}).get("limit"),
                    "1000000",
                )
            ],
        )

    for name, value in (("lender", "200000"), ("loan_broker", "20000")):
        holder = actors[name]
        runner.execute(
            experiment="SETUP",
            label=f"issue_tRLUSD_to_{name}",
            transaction=Payment(
                account=issuer.address,
                destination=holder.address,
                amount=amount(issuer.address, value),
            ),
            wallet=issuer,
            expected_result="tesSUCCESS",
            state=lambda holder=holder: {"balance": line_balance(holder.address, issuer.address)},
            assertions=lambda _before, after, value=value, name=name: [
                state_assertion(f"{name} tRLUSD fixture balance", after["balance"], value)
            ],
        )

    write_json(
        EVIDENCE / "fixture-final-state.json",
        {
            "classification": "SETUP, not E2",
            "ledger": ledger_header(),
            "balances": {
                name: line_balance(wallet.address, issuer.address)
                for name, wallet in actors.items()
                if name != "issuer"
            },
            "issuer": account_data(issuer.address),
        },
    )
    return actors


def create_closed_vault(
    runner: Runner,
    *,
    experiment: str,
    label: str,
    owner: Wallet,
    issuer: Wallet,
    subscription: int,
    redemption: int,
) -> tuple[str, dict[str, Any]]:
    record = runner.execute(
        experiment=experiment,
        label=label,
        transaction=VaultCreate(
            account=owner.address,
            asset=IssuedCurrency(currency=TR_USD, issuer=issuer.address),
            withdrawal_policy=WithdrawalPolicy.VAULT_STRATEGY_FIRST_COME_FIRST_SERVE,
            vault_kind=VaultKind.CLOSED,
            subscription_date=subscription,
            redemption_date=redemption,
        ),
        wallet=owner,
        expected_result="tesSUCCESS",
        state=lambda: {"objects": account_objects(owner.address)},
        assertions=lambda before, after: vault_create_transition_assertions(
            before_objects=before["objects"],
            after_objects=after["objects"],
            vault_index=None,
            owner=owner.address,
            asset={"currency": TR_USD, "issuer": issuer.address},
            vault_kind=1,
            subscription_date=subscription,
            redemption_date=redemption,
            withdrawal_policy=1,
        ),
    )
    meta = record["validated_transaction_response"]["meta"]
    vault_id = extract_created(meta, "Vault")
    node = ledger_node(index=vault_id)
    configuration_checks = [
        state_assertion("vault Owner", (node or {}).get("Owner"), owner.address),
        state_assertion("vault kind", (node or {}).get("VaultKind"), 1),
        state_assertion("subscription date", (node or {}).get("SubscriptionDate"), subscription),
        state_assertion("redemption date", (node or {}).get("RedemptionDate"), redemption),
        state_assertion(
            "vault asset",
            (node or {}).get("Asset"),
            {"currency": TR_USD, "issuer": issuer.address},
        ),
    ]
    if not all(check.passed for check in configuration_checks):
        detail = {"vault_id": vault_id, "vault": node, "assertions": configuration_checks}
        write_json(EVIDENCE / "unexpected-result.json", detail)
        raise CheckpointStop(f"{label}: created vault configuration mismatch")
    return vault_id, {"vault": node, "assertions": configuration_checks}


def run_e3(runner: Runner, actors: dict[str, Wallet]) -> dict[str, Any]:
    issuer = actors["issuer"]
    lender = actors["lender"]
    broker = actors["loan_broker"]
    now = ledger_header()["close_time"]
    subscription = now + 30
    redemption = subscription + 180
    vault_id, creation = create_closed_vault(
        runner,
        experiment="E3",
        label="e3_create_closed_ended_vault",
        owner=broker,
        issuer=issuer,
        subscription=subscription,
        redemption=redemption,
    )

    def state() -> dict[str, Any]:
        return vault_state(vault_id, lender.address, issuer.address)

    runner.execute(
        experiment="E3",
        label="e3_subscription_deposit",
        transaction=VaultDeposit(
            account=lender.address,
            vault_id=vault_id,
            amount=amount(issuer.address, "100000"),
        ),
        wallet=lender,
        expected_result="tesSUCCESS",
        state=state,
        assertions=vault_checks(
            asset_delta=100000,
            balance_delta=-100000,
            share_delta=100000000000,
        ),
    )
    runner.execute(
        experiment="E3",
        label="e3_subscription_withdraw",
        transaction=VaultWithdraw(
            account=lender.address,
            vault_id=vault_id,
            amount=amount(issuer.address, "1000"),
        ),
        wallet=lender,
        expected_result="tesSUCCESS",
        state=state,
        assertions=vault_checks(
            asset_delta=-1000,
            balance_delta=1000,
            share_delta=-1000000000,
        ),
    )
    investment = runner.advance_to(
        label="e3-investment-entry",
        subscription=subscription,
        redemption=redemption,
        wanted_phase="Investment",
        vault_id=vault_id,
    )
    runner.execute(
        experiment="E3",
        label="e3_investment_deposit_rejected",
        transaction=VaultDeposit(
            account=lender.address, vault_id=vault_id, amount=amount(issuer.address, "1")
        ),
        wallet=lender,
        expected_result="tecEXPIRED",
        state=state,
        assertions=unchanged_checks,
    )
    runner.execute(
        experiment="E3",
        label="e3_investment_withdraw_rejected",
        transaction=VaultWithdraw(
            account=lender.address, vault_id=vault_id, amount=amount(issuer.address, "1")
        ),
        wallet=lender,
        expected_result="tecTOO_SOON",
        state=state,
        assertions=unchanged_checks,
    )
    redemption_entry = runner.advance_to(
        label="e3-redemption-entry",
        subscription=subscription,
        redemption=redemption,
        wanted_phase="Redemption",
        vault_id=vault_id,
    )
    runner.execute(
        experiment="E3",
        label="e3_redemption_deposit_rejected",
        transaction=VaultDeposit(
            account=lender.address, vault_id=vault_id, amount=amount(issuer.address, "1")
        ),
        wallet=lender,
        expected_result="tecEXPIRED",
        state=state,
        assertions=unchanged_checks,
    )
    runner.execute(
        experiment="E3",
        label="e3_redemption_withdraw",
        transaction=VaultWithdraw(
            account=lender.address,
            vault_id=vault_id,
            amount=amount(issuer.address, "1000"),
        ),
        wallet=lender,
        expected_result="tesSUCCESS",
        state=state,
        assertions=vault_checks(
            asset_delta=-1000,
            balance_delta=1000,
            share_delta=-1000000000,
        ),
    )
    result = {
        "vault_id": vault_id,
        "creation": creation,
        "subscription_date": subscription,
        "redemption_date": redemption,
        "investment_entry": investment["entry"],
        "redemption_entry": redemption_entry["entry"],
        "final_state": state(),
        "loan_origination_not_exercised": True,
    }
    write_json(EVIDENCE / "e3-result.json", result)
    return result


def run_e4(runner: Runner, actors: dict[str, Wallet]) -> dict[str, Any]:
    issuer = actors["issuer"]
    borrower = actors["borrower"]
    broker = actors["loan_broker"]
    now = ledger_header()["close_time"]
    subscription = now + 30
    redemption = subscription + 180
    vault_id, creation = create_closed_vault(
        runner,
        experiment="E4",
        label="e4_create_fresh_closed_ended_vault",
        owner=broker,
        issuer=issuer,
        subscription=subscription,
        redemption=redemption,
    )

    def relationship_state() -> dict[str, Any]:
        return {
            "vault": ledger_node(index=vault_id),
            "broker_owner_objects": account_objects(broker.address),
            "borrower": account_data(borrower.address),
        }

    runner.execute(
        experiment="E4",
        label="e4_non_owner_loan_broker_set_rejected",
        transaction=LoanBrokerSet(
            account=borrower.address,
            vault_id=vault_id,
            debt_maximum="50000",
            cover_rate_minimum=10000,
            cover_rate_liquidation=25000,
            management_fee_rate=0,
        ),
        wallet=borrower,
        expected_result="tecNO_PERMISSION",
        state=relationship_state,
        assertions=unchanged_checks,
    )
    broker_record = runner.execute(
        experiment="E4",
        label="e4_owner_create_loan_broker",
        transaction=LoanBrokerSet(
            account=broker.address,
            vault_id=vault_id,
            debt_maximum="50000",
            cover_rate_minimum=10000,
            cover_rate_liquidation=25000,
            management_fee_rate=0,
        ),
        wallet=broker,
        expected_result="tesSUCCESS",
        state=lambda: {"objects": account_objects(broker.address)},
        assertions=lambda before, after: loan_broker_create_transition_assertions(
            before_objects=before["objects"],
            after_objects=after["objects"],
            broker_index=None,
            owner=broker.address,
            vault_id=vault_id,
            debt_maximum="50000",
            cover_rate_minimum=10000,
            cover_rate_liquidation=25000,
        ),
    )
    broker_id = extract_created(
        broker_record["validated_transaction_response"]["meta"], "LoanBroker"
    )

    def cover_state() -> dict[str, Any]:
        return {
            "loan_broker": ledger_node(index=broker_id),
            "owner_tRLUSD_balance": line_balance(broker.address, issuer.address),
        }

    runner.execute(
        experiment="E4",
        label="e4_deposit_first_loss_cover",
        transaction=LoanBrokerCoverDeposit(
            account=broker.address,
            loan_broker_id=broker_id,
            amount=amount(issuer.address, "10000"),
        ),
        wallet=broker,
        expected_result="tesSUCCESS",
        state=cover_state,
        assertions=lambda before, after: cover_transition_assertions(
            before_broker=before["loan_broker"],
            after_broker=after["loan_broker"],
            broker_index=broker_id,
            owner=broker.address,
            vault_id=vault_id,
            before_owner_balance=before["owner_tRLUSD_balance"],
            after_owner_balance=after["owner_tRLUSD_balance"],
            cover_delta=10000,
        ),
    )
    final = cover_state()
    broker_node = final["loan_broker"] or {}
    exact_checks = [
        state_assertion("LoanBroker Owner", broker_node.get("Owner"), broker.address),
        state_assertion("LoanBroker VaultID", broker_node.get("VaultID"), vault_id),
        state_assertion("DebtMaximum", broker_node.get("DebtMaximum"), "50000"),
        state_assertion("DebtTotal", broker_node.get("DebtTotal", "0"), "0"),
        state_assertion("CoverAvailable", broker_node.get("CoverAvailable"), "10000"),
        state_assertion("CoverRateMinimum", broker_node.get("CoverRateMinimum"), 10000),
        state_assertion("CoverRateLiquidation", broker_node.get("CoverRateLiquidation"), 25000),
    ]
    if not all(check.passed for check in exact_checks):
        detail = {"loan_broker_id": broker_id, "state": final, "assertions": exact_checks}
        write_json(EVIDENCE / "unexpected-result.json", detail)
        raise CheckpointStop("e4 LoanBroker fields do not match submitted configuration")
    result = {
        "vault_id": vault_id,
        "loan_broker_id": broker_id,
        "creation": creation,
        "vault_owner": broker.address,
        "loan_broker_owner": broker_node.get("Owner"),
        "ownership_relationship_observed": "LoanBrokerSet Account equals Vault Owner",
        "final_state": final,
        "field_assertions": exact_checks,
        "cover_findings": {
            "OBSERVED": {
                "cover_available": broker_node.get("CoverAvailable"),
                "cover_rate_minimum": broker_node.get("CoverRateMinimum"),
                "cover_rate_liquidation": broker_node.get("CoverRateLiquidation"),
                "debt_maximum": broker_node.get("DebtMaximum"),
                "debt_total": broker_node.get("DebtTotal", "0"),
            },
            "DERIVED": {
                "protocol_default_cover_bound": (
                    "minimum of rate-bounded DebtTotal, DefaultAmount, and CoverAvailable; "
                    "the rate calculation uses protocol tenth-basis-point scaling"
                )
            },
            "UNRESOLVED": [
                "No loan, default, loss, cover clawback, or liquidation was executed.",
                "No realized-loss behavior or production accounting conversion was tested.",
            ],
        },
    }
    write_json(EVIDENCE / "e4-result.json", result)
    return result


def main() -> int:
    if EVIDENCE.exists():
        raise CheckpointStop(
            "checkpoint-1c-r evidence directory already exists; refusing to retry the experiment"
        )
    root_seed = os.environ.get(ROOT_SEED_ENV)
    if not root_seed:
        print(f"Set {ROOT_SEED_ENV} for this process; it is never written to evidence.")
        return 2
    EVIDENCE.mkdir(parents=True)
    TRANSACTIONS.mkdir()
    client = JsonRpcClient(RPC_URL)
    runner = Runner(client=client)

    public_server_info = sanitize_response(rpc_success("server_info")["info"])
    public_server_info.pop("pubkey_node", None)
    start = {
        "server_info": public_server_info,
        "ledger": ledger_header(),
        "feature": sanitize_response(rpc_success("feature")),
    }
    feature_validation = validate_feature_response({"result": start["feature"]})
    if start["server_info"].get("network_id") != NETWORK_ID or not feature_validation["valid"]:
        write_json(EVIDENCE / "unexpected-result.json", start)
        raise CheckpointStop("frozen E1 invariants are absent on the fresh ledger")
    start["frozen_e1_cross_check"] = {
        "network_id": NETWORK_ID,
        "required_amendment_names": sorted(REQUIRED_AMENDMENTS),
        "valid": True,
        "classification": "FROZEN invariant verification, not a new E1 experiment",
    }
    write_json(EVIDENCE / "environment-start.json", start)
    write_json(
        EVIDENCE / "source-findings.json",
        {
            "pinned_source_commit": "4a4fded2eba11427c48ce3f24d9c1aea5e7a9d17",
            "findings": [
                {
                    "path": "src/libxrpl/tx/transactors/lending/LoanBrokerSet.cpp",
                    "lines": "88-98",
                    "finding": "LoanBrokerSet requires its Account to equal the Vault Owner.",
                },
                {
                    "path": "src/test/app/lending/LoanBroker_test.cpp",
                    "lines": "665-669",
                    "finding": "The pinned test expects tecNO_PERMISSION for a non-owner.",
                },
                {
                    "path": "src/libxrpl/ledger/helpers/VaultHelpers.cpp",
                    "lines": "290-305",
                    "finding": (
                        "Subscription includes equality at SubscriptionDate; Investment is "
                        "strictly later and before RedemptionDate; Redemption begins at expiry."
                    ),
                },
                {
                    "path": "include/xrpl/protocol/Protocol.h",
                    "lines": "361-368",
                    "finding": "The minimum Investment interval is 180 seconds.",
                },
            ],
        },
    )

    actors = setup_fixture(runner, Wallet.from_seed(root_seed))
    e3 = run_e3(runner, actors)
    e4 = run_e4(runner, actors)
    summary = {
        "checkpoint": "1C-R",
        "outcome": "PASS",
        "e1": "PASS/FROZEN",
        "e2": "PASS/FROZEN",
        "e3": "PASS",
        "e4": "PASS",
        "e5_through_e9": "NOT_ATTEMPTED",
        "e3_result": e3,
        "e4_result": e4,
        "transaction_count": len(runner.records),
        "close_count": len(runner.closes),
    }
    write_json(EVIDENCE / "checkpoint-result.json", summary)
    create_manifest(EVIDENCE, EVIDENCE / "MANIFEST.json")
    print("Checkpoint 1C-R completed: E3 PASS, E4 PASS. E5-E9 were not attempted.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CheckpointStop as exc:
        if EVIDENCE.exists():
            create_manifest(EVIDENCE, EVIDENCE / "MANIFEST.json")
        print(f"STOP: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
