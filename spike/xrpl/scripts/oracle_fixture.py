#!/usr/bin/env python3
"""Synthetic A-K fixture dry run for the frozen XRPL oracle bindings."""

from __future__ import annotations

import argparse
from copy import deepcopy
from pathlib import Path
from typing import Any

from frozen_oracle import ORACLE_ORDER, OracleSession
from harness import write_json

OWNER = "rSyntheticOwner"
LENDER = "rSyntheticLender"
NON_OWNER = "rSyntheticNonOwner"
ISSUER = "rSyntheticIssuer"
VAULT_ACCOUNT = "rSyntheticVaultPseudo"
BROKER_ACCOUNT = "rSyntheticBrokerPseudo"
ASSET = {"currency": "74524C5553440000000000000000000000000000", "issuer": ISSUER}
VAULT_ID = "SYNTHETIC-VAULT-1"
E4_VAULT_ID = "SYNTHETIC-VAULT-2"
SHARE_ID = "SYNTHETIC-SHARE-1"
BROKER_ID = "SYNTHETIC-BROKER-1"
SUBSCRIPTION_DATE = 130
REDEMPTION_DATE = 310


def _header(index: int, close_time: int) -> dict[str, Any]:
    return {"ledger_index": index, "close_time": close_time, "closed": True}


def _vault(
    *,
    vault_id: str = VAULT_ID,
    assets: int = 0,
    account: str = VAULT_ACCOUNT,
) -> dict[str, Any]:
    return {
        "LedgerEntryType": "Vault",
        "index": vault_id,
        "Owner": OWNER,
        "Asset": ASSET,
        "VaultKind": 1,
        "SubscriptionDate": SUBSCRIPTION_DATE,
        "RedemptionDate": REDEMPTION_DATE,
        "WithdrawalPolicy": 1,
        "Flags": 0,
        "ShareMPTID": SHARE_ID,
        "Account": account,
        "AssetsTotal": str(assets),
        "AssetsAvailable": str(assets),
    }


def _issuance(outstanding: int) -> dict[str, Any]:
    return {
        "LedgerEntryType": "MPTokenIssuance",
        "index": "SYNTHETIC-ISSUANCE-INDEX",
        "mpt_issuance_id": SHARE_ID,
        "OutstandingAmount": str(outstanding),
        "AssetScale": 6,
    }


def _holder(amount: int, account: str = LENDER) -> dict[str, Any]:
    return {
        "LedgerEntryType": "MPToken",
        "index": f"SYNTHETIC-HOLDER-{account}",
        "Account": account,
        "MPTokenIssuanceID": SHARE_ID,
        "MPTAmount": str(amount),
        "Flags": 0,
    }


def _broker(cover: int = 0) -> dict[str, Any]:
    return {
        "LedgerEntryType": "LoanBroker",
        "index": BROKER_ID,
        "Owner": OWNER,
        "VaultID": E4_VAULT_ID,
        "Account": BROKER_ACCOUNT,
        "DebtMaximum": "50000",
        "DebtTotal": "0",
        "CoverAvailable": str(cover),
        "CoverRateMinimum": 10000,
        "CoverRateLiquidation": 25000,
        "ManagementFeeRate": 0,
    }


def _lookup(result: str, created: tuple[tuple[str, str], ...] = ()) -> dict[str, Any]:
    return {
        "validated": True,
        "meta": {
            "TransactionResult": result,
            "AffectedNodes": [
                {
                    "CreatedNode": {
                        "LedgerEntryType": ledger_type,
                        "LedgerIndex": index,
                    }
                }
                for ledger_type, index in created
            ],
        },
    }


def _observation(
    *,
    step: str,
    result: str,
    before: dict[str, Any],
    after: dict[str, Any],
    context: dict[str, Any],
    ledger_index: int,
    close_time: int,
    created: tuple[tuple[str, str], ...] = (),
    validated: bool | None = None,
) -> dict[str, Any]:
    is_success = result.startswith("tes")
    has_validated_result = is_success if validated is None else validated
    lookup = _lookup(result, created) if has_validated_result else {"validated": False}
    return {
        "pre_state": before,
        "submission_response": {"engine_result": result},
        "submission_hash_if_any": f"synthetic-{step.lower()}-hash",
        "client_exception": None,
        "validated_lookup": lookup,
        "transport_or_server_failure": None,
        "post_state": after,
        "ledger_before": _header(ledger_index - 1, close_time),
        "ledger_after": _header(ledger_index, close_time + 1),
        "context": context,
        "synthetic": True,
    }


def synthetic_observations() -> dict[str, dict[str, Any]]:
    """Return source-shaped synthetic observations, explicitly not protocol evidence."""

    owner_trustline = {"account": ISSUER, "currency": ASSET["currency"], "balance": "100"}
    vault_a = _vault()
    owner_token = _holder(0, OWNER)
    a_before_objects = [{"LedgerEntryType": "RippleState", "index": "SYNTHETIC-OWNER-LINE"}]
    a_after_objects = [*a_before_objects, vault_a, owner_token]
    a_context = {
        "vault_index": VAULT_ID,
        "owner": OWNER,
        "asset": ASSET,
        "vault_kind": 1,
        "subscription_date": SUBSCRIPTION_DATE,
        "redemption_date": REDEMPTION_DATE,
        "withdrawal_policy": 1,
        "flags": 0,
    }
    observations: dict[str, dict[str, Any]] = {
        "A": _observation(
            step="A",
            result="tesSUCCESS",
            before={"owner_objects": a_before_objects, "owner_trustline": owner_trustline},
            after={
                "owner_objects": a_after_objects,
                "owner_trustline": owner_trustline,
                "vault": vault_a,
                "owner_mptoken": owner_token,
                "share_issuance": _issuance(0),
                "vault_pseudo_account": {
                    "LedgerEntryType": "AccountRoot",
                    "Account": VAULT_ACCOUNT,
                },
            },
            context=a_context,
            ledger_index=11,
            close_time=100,
            created=(("Vault", VAULT_ID),),
        )
    }

    b_before = {
        "vault": _vault(assets=0),
        "share_issuance": _issuance(0),
        "lender_share": None,
        "lender_balance": "1000",
        "unrelated_state": {"borrower_balance": "0", "owner_balance": "100"},
    }
    b_after = {
        "vault": _vault(assets=100),
        "share_issuance": _issuance(100000000),
        "lender_share": _holder(100000000),
        "lender_balance": "900",
        "unrelated_state": b_before["unrelated_state"],
    }
    lifecycle_context = {
        "lender": LENDER,
        "subscription_date": SUBSCRIPTION_DATE,
        "redemption_date": REDEMPTION_DATE,
        "asset_delta": 100,
        "balance_delta": -100,
        "share_delta": 100000000,
    }
    observations["B"] = _observation(
        step="B",
        result="tesSUCCESS",
        before=b_before,
        after=b_after,
        context=lifecycle_context,
        ledger_index=12,
        close_time=110,
    )

    c_after = {
        "vault": _vault(assets=90),
        "share_issuance": _issuance(90000000),
        "lender_share": _holder(90000000),
        "lender_balance": "910",
        "unrelated_state": b_after["unrelated_state"],
    }
    observations["C"] = _observation(
        step="C",
        result="tesSUCCESS",
        before=b_after,
        after=c_after,
        context={
            **lifecycle_context,
            "asset_delta": -10,
            "balance_delta": 10,
            "share_delta": -10000000,
        },
        ledger_index=13,
        close_time=120,
    )

    investment_protected = {
        "vault": c_after["vault"],
        "share_issuance": c_after["share_issuance"],
        "lender_share": c_after["lender_share"],
        "lender_balance": c_after["lender_balance"],
        "lender_account_sequence": 7,
    }
    negative_context = {
        "subscription_date": SUBSCRIPTION_DATE,
        "redemption_date": REDEMPTION_DATE,
    }
    for step, result, index in (("D", "tecEXPIRED", 14), ("E", "tecTOO_SOON", 15)):
        observations[step] = _observation(
            step=step,
            result=result,
            before={"protected": investment_protected},
            after={"protected": investment_protected},
            context=negative_context,
            ledger_index=index,
            close_time=200,
            validated=False,
        )

    f_after = {
        "vault": _vault(assets=80),
        "share_issuance": _issuance(80000000),
        "lender_share": _holder(80000000),
        "lender_balance": "920",
        "unrelated_state": c_after["unrelated_state"],
    }
    observations["F"] = _observation(
        step="F",
        result="tesSUCCESS",
        before=c_after,
        after=f_after,
        context={
            **lifecycle_context,
            "asset_delta": -10,
            "balance_delta": 10,
            "share_delta": -10000000,
        },
        ledger_index=16,
        close_time=310,
    )
    redemption_protected = {
        "vault": f_after["vault"],
        "share_issuance": f_after["share_issuance"],
        "lender_share": f_after["lender_share"],
        "lender_balance": f_after["lender_balance"],
        "lender_account_sequence": 8,
    }
    observations["G"] = _observation(
        step="G",
        result="tecEXPIRED",
        before={"protected": redemption_protected},
        after={"protected": redemption_protected},
        context=negative_context,
        ledger_index=17,
        close_time=311,
        validated=False,
    )

    e4_vault = _vault(vault_id=E4_VAULT_ID, account="rSyntheticE4VaultPseudo")
    h_before_objects = [e4_vault, owner_token]
    broker = _broker()
    h_after_objects = [*h_before_objects, broker]
    h_before = {
        "owner_objects": h_before_objects,
        "vault": e4_vault,
        "owner_balance": "100",
        "owner_account": {"Account": OWNER, "OwnerCount": 3},
    }
    h_after = {
        "owner_objects": h_after_objects,
        "vault": e4_vault,
        "owner_balance": "100",
        "owner_account": {"Account": OWNER, "OwnerCount": 5},
        "loan_broker": broker,
        "broker_pseudo_account": {
            "LedgerEntryType": "AccountRoot",
            "Account": BROKER_ACCOUNT,
        },
    }
    broker_context = {
        "loan_broker_index": BROKER_ID,
        "owner": OWNER,
        "vault_id": E4_VAULT_ID,
        "debt_maximum": "50000",
        "cover_rate_minimum": 10000,
        "cover_rate_liquidation": 25000,
        "owner_count_delta": 2,
    }
    observations["H"] = _observation(
        step="H",
        result="tesSUCCESS",
        before=h_before,
        after=h_after,
        context=broker_context,
        ledger_index=18,
        close_time=320,
        created=(("LoanBroker", BROKER_ID),),
    )
    observations["I"] = _observation(
        step="I",
        result="tesSUCCESS",
        before={"vault": e4_vault},
        after={"vault": e4_vault, "loan_broker": broker},
        context=broker_context,
        ledger_index=18,
        close_time=321,
    )

    j_before = {
        "loan_broker": broker,
        "owner_balance": "100",
        "broker_holding": "0",
        "vault": e4_vault,
        "unrelated_state": {"borrower_balance": "0"},
    }
    j_after = {
        "loan_broker": _broker(cover=10),
        "owner_balance": "90",
        "broker_holding": "10",
        "vault": e4_vault,
        "unrelated_state": j_before["unrelated_state"],
    }
    observations["J"] = _observation(
        step="J",
        result="tesSUCCESS",
        before=j_before,
        after=j_after,
        context={**broker_context, "cover_delta": 10},
        ledger_index=19,
        close_time=322,
    )

    k_protected = {
        "vault": e4_vault,
        "owner_account_sequence": 9,
        "non_owner_account_sequence": 4,
        "owner_objects": h_after_objects,
        "non_owner_objects": [],
        "owner_balance": "90",
        "non_owner_balance": "0",
    }
    observations["K"] = _observation(
        step="K",
        result="tecNO_PERMISSION",
        before={"protected": k_protected, "non_owner_objects": []},
        after={"protected": k_protected, "non_owner_objects": []},
        context={"non_owner": NON_OWNER, "vault_id": E4_VAULT_ID},
        ledger_index=20,
        close_time=323,
        validated=False,
    )
    return observations


def fixture_providers(
    observations: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    values = synthetic_observations() if observations is None else observations
    return {step: (lambda value=value: deepcopy(value)) for step, value in values.items()}


def run_fixture_dry_run() -> dict[str, Any]:
    session = OracleSession()
    records = session.run(fixture_providers())
    session.require_complete()
    return {
        "checkpoint": "1C-B",
        "classification": "SYNTHETIC HARNESS EVIDENCE — NOT XRPL PROTOCOL EVIDENCE",
        "environment_started": False,
        "protocol_transactions_submitted": 0,
        "order": [record["step"] for record in records],
        "expected_order": list(ORACLE_ORDER),
        "outcome": "PASS",
        "records": records,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    result = run_fixture_dry_run()
    write_json(args.output, result)
    print("Synthetic frozen-oracle dry run completed A-K; no XRPL calls were made.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
