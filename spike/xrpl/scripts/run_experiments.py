#!/usr/bin/env python3
"""Run the Credence XRPL lending composition spike against standalone rippled.

All identities are generated in memory for each run except the standalone genesis
account, whose seed must be supplied through the environment. Evidence deliberately
omits signing material, public keys, signatures, and the supplied seed.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from xrpl.clients import JsonRpcClient
from xrpl.models.amounts import IssuedCurrencyAmount
from xrpl.models.currencies import IssuedCurrency
from xrpl.models.transactions import (
    AccountSet,
    LoanBrokerCoverDeposit,
    LoanBrokerSet,
    LoanPay,
    LoanSet,
    Payment,
    SignerListSet,
    TrustSet,
    VaultCreate,
    VaultDeposit,
    VaultWithdraw,
)
from xrpl.models.transactions.account_set import AccountSetAsfFlag
from xrpl.models.transactions.signer_list_set import SignerEntry
from xrpl.models.transactions.vault_create import VaultKind, WithdrawalPolicy
from xrpl.transaction import autofill, multisign, sign, submit
from xrpl.transaction.counterparty_signer import compute_signature
from xrpl.wallet import Wallet

RPC_URL = os.environ.get("XRPL_RPC_URL", "http://127.0.0.1:5005")
ROOT_SEED_ENV = "XRPL_STANDALONE_GENESIS_SEED"
NETWORK_ID = 21_337
EVIDENCE_DIR = Path(__file__).resolve().parents[1] / "evidence"
RESULTS_PATH = Path(__file__).resolve().parents[1] / "RESULTS.json"
TRAIL_PATH = EVIDENCE_DIR / "transactions.json"
STATE_PATH = EVIDENCE_DIR / "ledger-state.json"
FAILURE_PATH = EVIDENCE_DIR / "unexpected-result.json"

# XRPL issued-currency wire code for the six-character label tRLUSD.
TR_USD = b"tRLUSD".hex().upper().ljust(40, "0")

AMENDMENTS = {
    # These non-matching IDs are retained as evidence of the fail-closed gate.
    "MPTokensV1": "950AE2EA72CE4C967DEB64B3492BF98B1075D6F9540A6AF2FC63A5F365DB6326",  # gitleaks:allow
    "SingleAssetVault": "81BD1A4416F5B0C5E4C4A79E3B5A8C1739558C6F34C1B24412EC6B98A76C53A7",
    "LendingProtocol": "565B7D380202CE28816F28DE00BFA6F5CDA5B5F2AAC1FB57A62DD6B3EAA3F6A9",
    "LendingProtocolV1_1": "A3600D5E65DB05A4DBA339DE9F8E0B60C231ED73753B0D3B3ED9007067F2CC55",
    "fixCleanup3_1_3": "303A3142B599971FB89E8D021F2C66B1DEB781AB63205B9B7A8B1AB8E85A5E7A",
    "fixCleanup3_2_0": "21B8A1C08D6640A0C8F175A3E0A435F8F34FD24E25B3FB0A0B779C67DCDF8B1C",
    "fixCleanup3_3_0": "3298A9DA0A2F2D7A541F25A518DB3680152E6397B43E8B363C28CC5CFF5F2D76",
    "fixCleanup3_4_0": "9843AB4A5D9A5D01C6A7C73D89AEE6E6B8C9434AC51D8C7612E226CF853D6F63",
}

TF_LOAN_OVERPAYMENT = 0x0001_0000
TF_LOAN_LATE_PAYMENT = 0x0004_0000


class UnexpectedLedgerResult(RuntimeError):
    """Raised when the ledger contradicts the experiment's expected result."""


def rpc(method: str, **params: Any) -> dict[str, Any]:
    response = httpx.post(
        RPC_URL,
        json={"method": method, "params": [params]},
        timeout=20,
    )
    response.raise_for_status()
    body = response.json()
    result = body.get("result", {})
    if result.get("status") == "error":
        raise RuntimeError(f"RPC {method} failed: {result}")
    return result


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def sanitize(value: Any) -> Any:
    """Remove all signing material before evidence reaches disk."""
    forbidden = {
        "SigningPubKey",
        "TxnSignature",
        "Signers",
        "CounterpartySignature",
        "public_key",
        "private_key",
        "seed",
    }
    if isinstance(value, dict):
        return {key: sanitize(item) for key, item in value.items() if key not in forbidden}
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    return value


def engine_result(response: Any) -> str:
    result = response.result
    return str(result.get("engine_result") or result.get("engine_result_code") or "")


def extract_created(meta: dict[str, Any], ledger_type: str) -> str:
    for wrapper in meta.get("AffectedNodes", []):
        created = wrapper.get("CreatedNode")
        if created and created.get("LedgerEntryType") == ledger_type:
            return str(created["LedgerIndex"])
    raise RuntimeError(f"No created {ledger_type} node in transaction metadata")


@dataclass
class Runner:
    client: JsonRpcClient
    transactions: list[dict[str, Any]]
    states: dict[str, Any]

    def close(self) -> dict[str, Any]:
        rpc("ledger_accept")
        return rpc("ledger", ledger_index="validated", transactions=False, expand=False)

    def record_failure(
        self,
        label: str,
        expected: str,
        actual: str,
        classification: str,
        tx_json: dict[str, Any],
    ) -> None:
        write_json(
            FAILURE_PATH,
            {
                "actual": actual,
                "classification": classification,
                "expected": expected,
                "label": label,
                "sanitized_transaction": sanitize(tx_json),
            },
        )

    def send(
        self,
        label: str,
        signed_tx: Any,
        expected: str,
        classification_if_unexpected: str,
    ) -> dict[str, Any]:
        response = submit(signed_tx, self.client, fail_hard=True)
        preliminary = engine_result(response)
        tx_hash = signed_tx.get_hash()
        record: dict[str, Any] = {
            "label": label,
            "expected": expected,
            "preliminary_result": preliminary,
            "transaction_hash": tx_hash,
            "transaction": sanitize(signed_tx.to_xrpl()),
        }
        if preliminary != expected:
            self.transactions.append(record)
            write_json(TRAIL_PATH, self.transactions)
            self.record_failure(
                label,
                expected,
                preliminary,
                classification_if_unexpected,
                signed_tx.to_xrpl(),
            )
            raise UnexpectedLedgerResult(
                f"{label}: expected {expected}, got {preliminary} ({classification_if_unexpected})"
            )

        if preliminary.startswith(("tes", "tec")):
            self.close()
            final = rpc("tx", transaction=tx_hash, binary=False)
            meta = final.get("meta", {})
            final_result = str(meta.get("TransactionResult", ""))
            record["final_result"] = final_result
            record["ledger_index"] = final.get("ledger_index")
            record["validated"] = final.get("validated")
            record["metadata"] = sanitize(meta)
            if final_result != expected:
                self.transactions.append(record)
                write_json(TRAIL_PATH, self.transactions)
                self.record_failure(
                    label,
                    expected,
                    final_result,
                    classification_if_unexpected,
                    signed_tx.to_xrpl(),
                )
                raise UnexpectedLedgerResult(
                    f"{label}: preliminary {expected}, validated as {final_result} "
                    f"({classification_if_unexpected})"
                )

        self.transactions.append(record)
        write_json(TRAIL_PATH, self.transactions)
        return record

    def single(
        self,
        label: str,
        transaction: Any,
        wallet: Wallet,
        expected: str = "tesSUCCESS",
        classification_if_unexpected: str = "D. misunderstood protocol precondition",
    ) -> dict[str, Any]:
        filled = autofill(transaction, self.client)
        return self.send(
            label,
            sign(filled, wallet),
            expected,
            classification_if_unexpected,
        )

    def borrower_multisign(self, transaction: Any, signers: list[Wallet]) -> Any:
        filled = autofill(transaction, self.client, signers_count=len(signers))
        partials = [sign(filled, signer, multisign=True) for signer in signers]
        return multisign(filled, partials)

    def loan_set(
        self,
        transaction: LoanSet,
        borrower_signers: list[Wallet],
        broker: Wallet | None,
        expected: str,
        label: str,
        stale_broker_signature_from: LoanSet | None = None,
    ) -> dict[str, Any]:
        # Total fee is base plus one unit per borrower signature and one for the
        # role-prefixed broker counterparty signature.
        extra_signature_count = len(borrower_signers) + (1 if broker else 0)
        filled = autofill(transaction, self.client, signers_count=extra_signature_count)
        broker_signing_source = stale_broker_signature_from or filled
        broker_signature = None
        if broker is not None:
            broker_signature = compute_signature(
                broker_signing_source.to_xrpl(), broker.private_key
            )
        partials = [sign(filled, signer, multisign=True) for signer in borrower_signers]
        signed = multisign(filled, partials)
        payload = signed.to_xrpl()
        if broker is not None and broker_signature is not None:
            payload["CounterpartySignature"] = {
                "SigningPubKey": broker.public_key,
                "TxnSignature": broker_signature,
            }
        final = LoanSet.from_xrpl(payload)
        return self.send(
            label,
            final,
            expected,
            "E. interaction defect / unsupported composition",
        )

    def snapshot(self, label: str, **entries: Any) -> None:
        self.states[label] = sanitize(entries)
        write_json(STATE_PATH, self.states)


def amount(issuer: str, value: str | int) -> IssuedCurrencyAmount:
    return IssuedCurrencyAmount(currency=TR_USD, issuer=issuer, value=str(value))


def line_balance(account: str, issuer: str) -> str:
    lines = rpc("account_lines", account=account, ledger_index="validated").get("lines", [])
    for line in lines:
        if line.get("account") == issuer and line.get("currency") == TR_USD:
            return str(line["balance"])
    return "0"


def ledger_entry(index: str) -> dict[str, Any]:
    return rpc("ledger_entry", index=index, ledger_index="validated").get("node", {})


def validated_close_time() -> int:
    ledger = rpc("ledger", ledger_index="validated", transactions=False, expand=False)["ledger"]
    return int(ledger["close_time"])


def wait_until(runner: Runner, target: int, label: str) -> None:
    while validated_close_time() <= target:
        remaining = target - validated_close_time() + 1
        print(f"{label}: waiting for ledger close time ({remaining}s remaining)", flush=True)
        time.sleep(min(4, max(1, remaining)))
        runner.close()


def loan_template(borrower: str, broker: str, broker_id: str, principal: str) -> LoanSet:
    return LoanSet(
        account=borrower,
        counterparty=broker,
        loan_broker_id=broker_id,
        principal_requested=principal,
        payment_total=2,
        payment_interval=60,
        interest_rate=0,
        late_interest_rate=0,
        overpayment_interest_rate=0,
        loan_service_fee="0",
        late_payment_fee="0",
        close_payment_fee="0",
        loan_origination_fee="0",
        overpayment_fee=0,
    )


def main() -> int:
    root_seed = os.environ.get(ROOT_SEED_ENV)
    if not root_seed:
        print(f"Set {ROOT_SEED_ENV} in the process environment; it is never written to disk.")
        return 2

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    for stale in (TRAIL_PATH, STATE_PATH, FAILURE_PATH, RESULTS_PATH):
        if stale.exists():
            stale.unlink()

    client = JsonRpcClient(RPC_URL)
    runner = Runner(client=client, transactions=[], states={})
    root = Wallet.from_seed(root_seed)
    actors = {
        "issuer": Wallet.create(),
        "lender": Wallet.create(),
        "borrower": Wallet.create(),
        "loan_broker": Wallet.create(),
        "research_bot_signer": Wallet.create(),
        "credence_policy_signer": Wallet.create(),
        "payment_recipient": Wallet.create(),
    }
    accounts = {name: wallet.address for name, wallet in actors.items()}

    server_info = rpc("server_info")["info"]
    feature = rpc("feature")
    amendment_state = {
        name: {
            "id": amendment_id,
            "enabled": bool(feature.get(amendment_id, {}).get("enabled")),
        }
        for name, amendment_id in AMENDMENTS.items()
    }
    if server_info.get("network_id") != NETWORK_ID or not all(
        item["enabled"] for item in amendment_state.values()
    ):
        write_json(
            FAILURE_PATH,
            {
                "classification": "B. amendment unavailable/not enabled",
                "network_id": server_info.get("network_id"),
                "amendments": amendment_state,
            },
        )
        raise UnexpectedLedgerResult("Required network/amendment state is absent")

    runner.snapshot(
        "e1_initial_server",
        server_info=server_info,
        amendments=amendment_state,
        currency={"human_label": "tRLUSD", "xrpl_wire_code": TR_USD},
        accounts=accounts,
    )

    # Fund every runtime-generated actor with disposable standalone XRP.
    for name, wallet in actors.items():
        runner.single(
            f"fund_{name}",
            Payment(account=root.address, destination=wallet.address, amount="1000000000"),
            root,
        )

    issuer = actors["issuer"]
    lender = actors["lender"]
    borrower = actors["borrower"]
    broker = actors["loan_broker"]
    research = actors["research_bot_signer"]
    policy = actors["credence_policy_signer"]
    recipient = actors["payment_recipient"]

    runner.single(
        "issuer_enable_default_ripple",
        AccountSet(account=issuer.address, set_flag=AccountSetAsfFlag.ASF_DEFAULT_RIPPLE),
        issuer,
    )
    for label, holder in (("lender", lender), ("borrower", borrower), ("loan_broker", broker)):
        runner.single(
            f"{label}_trust_tRLUSD",
            TrustSet(account=holder.address, limit_amount=amount(issuer.address, "1000000")),
            holder,
        )
    runner.single(
        "issue_tRLUSD_to_lender",
        Payment(
            account=issuer.address,
            destination=lender.address,
            amount=amount(issuer.address, "200000"),
        ),
        issuer,
    )
    runner.single(
        "issue_tRLUSD_to_broker",
        Payment(
            account=issuer.address,
            destination=broker.address,
            amount=amount(issuer.address, "20000"),
        ),
        issuer,
    )
    runner.snapshot(
        "e2_accounts_and_asset",
        accounts=accounts,
        balances={
            "lender_tRLUSD": line_balance(lender.address, issuer.address),
            "borrower_tRLUSD": line_balance(borrower.address, issuer.address),
            "loan_broker_tRLUSD": line_balance(broker.address, issuer.address),
        },
        issuer_account=rpc("account_info", account=issuer.address, ledger_index="validated")[
            "account_data"
        ],
    )

    runner.single(
        "borrower_set_2_of_2",
        SignerListSet(
            account=borrower.address,
            signer_quorum=2,
            signer_entries=[
                SignerEntry(account=research.address, signer_weight=1),
                SignerEntry(account=policy.address, signer_weight=1),
            ],
        ),
        borrower,
    )
    payment = Payment(account=borrower.address, destination=recipient.address, amount="1000")
    runner.send(
        "borrower_payment_research_only",
        runner.borrower_multisign(payment, [research]),
        "tefBAD_QUORUM",
        "E. interaction defect / unsupported composition",
    )
    runner.send(
        "borrower_payment_policy_only",
        runner.borrower_multisign(payment, [policy]),
        "tefBAD_QUORUM",
        "E. interaction defect / unsupported composition",
    )
    runner.send(
        "borrower_payment_research_and_policy",
        runner.borrower_multisign(payment, [research, policy]),
        "tesSUCCESS",
        "E. interaction defect / unsupported composition",
    )
    borrower_info = rpc(
        "account_info",
        account=borrower.address,
        ledger_index="validated",
        signer_lists=True,
    )
    runner.snapshot(
        "e5_borrower_authorization",
        account_data=borrower_info["account_data"],
        signer_lists=borrower_info.get("signer_lists", []),
        master_key_disabled=False,
        payment_recipient_xrp_drops=rpc(
            "account_info", account=recipient.address, ledger_index="validated"
        )["account_data"]["Balance"],
    )

    now = validated_close_time()
    subscription_date = now + 20
    redemption_date = subscription_date + 300
    vault_record = runner.single(
        "create_closed_ended_vault",
        VaultCreate(
            account=broker.address,
            asset=IssuedCurrency(currency=TR_USD, issuer=issuer.address),
            withdrawal_policy=WithdrawalPolicy.VAULT_STRATEGY_FIRST_COME_FIRST_SERVE,
            vault_kind=VaultKind.CLOSED,
            subscription_date=subscription_date,
            redemption_date=redemption_date,
        ),
        broker,
    )
    vault_id = extract_created(vault_record["metadata"], "Vault")
    runner.single(
        "subscription_deposit",
        VaultDeposit(
            account=lender.address, vault_id=vault_id, amount=amount(issuer.address, "100000")
        ),
        lender,
    )
    runner.single(
        "subscription_withdraw",
        VaultWithdraw(
            account=lender.address, vault_id=vault_id, amount=amount(issuer.address, "1000")
        ),
        lender,
    )
    broker_record = runner.single(
        "create_loan_broker",
        LoanBrokerSet(
            account=broker.address,
            vault_id=vault_id,
            debt_maximum="50000",
            cover_rate_minimum=10000,
            cover_rate_liquidation=25000,
            management_fee_rate=0,
        ),
        broker,
    )
    broker_id = extract_created(broker_record["metadata"], "LoanBroker")
    runner.single(
        "deposit_first_loss_cover",
        LoanBrokerCoverDeposit(
            account=broker.address,
            loan_broker_id=broker_id,
            amount=amount(issuer.address, "10000"),
        ),
        broker,
    )
    runner.snapshot(
        "subscription_state",
        close_time=validated_close_time(),
        vault=ledger_entry(vault_id),
        loan_broker=ledger_entry(broker_id),
    )

    base_subscription_loan = loan_template(borrower.address, broker.address, broker_id, "2000")
    runner.loan_set(
        base_subscription_loan,
        [research, policy],
        broker,
        "tecTOO_SOON",
        "subscription_loan_rejected",
    )

    wait_until(runner, subscription_date, "enter investment")
    runner.snapshot(
        "investment_phase_entry",
        close_time=validated_close_time(),
        vault=ledger_entry(vault_id),
    )
    runner.single(
        "investment_deposit_rejected",
        VaultDeposit(account=lender.address, vault_id=vault_id, amount=amount(issuer.address, "1")),
        lender,
        expected="tecEXPIRED",
    )
    runner.single(
        "investment_withdraw_rejected",
        VaultWithdraw(
            account=lender.address, vault_id=vault_id, amount=amount(issuer.address, "1")
        ),
        lender,
        expected="tecTOO_SOON",
    )

    # LoanSet authorization negative matrix, all using the borrower as Account
    # and the Loan Broker owner as the separately signed Counterparty.
    negative_template = loan_template(borrower.address, broker.address, broker_id, "2000")
    runner.loan_set(
        negative_template,
        [research],
        broker,
        "tefBAD_QUORUM",
        "loan_set_research_only",
    )
    runner.loan_set(
        negative_template,
        [policy],
        broker,
        "tefBAD_QUORUM",
        "loan_set_policy_only",
    )
    runner.loan_set(
        negative_template,
        [research, policy],
        None,
        "temBAD_SIGNER",
        "loan_set_missing_counterparty_signature",
    )

    # Build one fixed-sequence transaction, have the broker sign its original
    # terms, then alter PrincipalRequested and create fresh borrower signatures.
    original = autofill(negative_template, client, signers_count=3)
    altered_json = original.to_xrpl()
    altered_json["PrincipalRequested"] = "2001"
    altered = LoanSet.from_xrpl(altered_json)
    runner.loan_set(
        altered,
        [research, policy],
        broker,
        "tefBAD_SIGNATURE",
        "loan_set_altered_terms",
        stale_broker_signature_from=original,
    )

    before_a = {
        "borrower_balance": line_balance(borrower.address, issuer.address),
        "vault": ledger_entry(vault_id),
        "loan_broker": ledger_entry(broker_id),
    }
    loan_a_tx = loan_template(borrower.address, broker.address, broker_id, "2000")
    loan_a_tx.flags = TF_LOAN_OVERPAYMENT
    loan_a_record = runner.loan_set(
        loan_a_tx,
        [research, policy],
        broker,
        "tesSUCCESS",
        "investment_loan_a_created",
    )
    loan_a_id = extract_created(loan_a_record["metadata"], "Loan")

    loan_b_tx = loan_template(borrower.address, broker.address, broker_id, "1000")
    loan_b_tx.payment_total = 1
    loan_b_record = runner.loan_set(
        loan_b_tx,
        [research, policy],
        broker,
        "tesSUCCESS",
        "investment_loan_b_created",
    )
    loan_b_id = extract_created(loan_b_record["metadata"], "Loan")
    runner.snapshot(
        "e6_e7_valid_loans",
        before=before_a,
        after={
            "borrower_balance": line_balance(borrower.address, issuer.address),
            "vault": ledger_entry(vault_id),
            "loan_broker": ledger_entry(broker_id),
            "loan_a": ledger_entry(loan_a_id),
            "loan_b": ledger_entry(loan_b_id),
        },
    )

    runner.single(
        "loan_a_early_partial_rejected",
        LoanPay(
            account=borrower.address,
            loan_id=loan_a_id,
            amount=amount(issuer.address, "500"),
            flags=TF_LOAN_OVERPAYMENT,
        ),
        borrower,
        expected="tecINSUFFICIENT_PAYMENT",
    )
    runner.single(
        "loan_a_early_overpayment",
        LoanPay(
            account=borrower.address,
            loan_id=loan_a_id,
            amount=amount(issuer.address, "1100"),
            flags=TF_LOAN_OVERPAYMENT,
        ),
        borrower,
    )
    loan_a_after_overpay = ledger_entry(loan_a_id)
    runner.snapshot(
        "loan_a_after_early_tests",
        close_time=validated_close_time(),
        loan=loan_a_after_overpay,
        borrower_balance=line_balance(borrower.address, issuer.address),
        vault=ledger_entry(vault_id),
    )

    next_due = int(loan_a_after_overpay["NextPaymentDueDate"])
    wait_until(runner, next_due - 1, "scheduled payment due")
    current_loan_a = ledger_entry(loan_a_id)
    scheduled_amount = str(current_loan_a["PeriodicPayment"])
    runner.single(
        "loan_a_scheduled_payment",
        LoanPay(
            account=borrower.address,
            loan_id=loan_a_id,
            amount=amount(issuer.address, scheduled_amount),
        ),
        borrower,
    )
    runner.snapshot(
        "e8_after_scheduled_payment",
        close_time=validated_close_time(),
        loan_a=ledger_entry(loan_a_id),
        borrower_balance=line_balance(borrower.address, issuer.address),
        vault=ledger_entry(vault_id),
        loan_broker=ledger_entry(broker_id),
    )

    wait_until(runner, redemption_date, "enter redemption")
    runner.snapshot(
        "redemption_phase_entry",
        close_time=validated_close_time(),
        vault=ledger_entry(vault_id),
        loan_b=ledger_entry(loan_b_id),
    )
    runner.loan_set(
        loan_template(borrower.address, broker.address, broker_id, "100"),
        [research, policy],
        broker,
        "tecEXPIRED",
        "redemption_loan_rejected",
    )
    runner.single(
        "redemption_deposit_rejected",
        VaultDeposit(account=lender.address, vault_id=vault_id, amount=amount(issuer.address, "1")),
        lender,
        expected="tecEXPIRED",
    )
    loan_b_state = ledger_entry(loan_b_id)
    runner.single(
        "redemption_late_repayment",
        LoanPay(
            account=borrower.address,
            loan_id=loan_b_id,
            amount=amount(issuer.address, str(loan_b_state["PeriodicPayment"])),
            flags=TF_LOAN_LATE_PAYMENT,
        ),
        borrower,
    )
    runner.single(
        "redemption_withdraw",
        VaultWithdraw(
            account=lender.address, vault_id=vault_id, amount=amount(issuer.address, "1000")
        ),
        lender,
    )
    runner.snapshot(
        "e9_final_state",
        close_time=validated_close_time(),
        vault=ledger_entry(vault_id),
        loan_broker=ledger_entry(broker_id),
        borrower_balance=line_balance(borrower.address, issuer.address),
        lender_balance=line_balance(lender.address, issuer.address),
    )

    by_label = {record["label"]: record for record in runner.transactions}
    experiment_labels = {
        "E1": [],
        "E2": [
            "issuer_enable_default_ripple",
            "lender_trust_tRLUSD",
            "borrower_trust_tRLUSD",
            "loan_broker_trust_tRLUSD",
            "issue_tRLUSD_to_lender",
            "issue_tRLUSD_to_broker",
        ],
        "E3": [
            "create_closed_ended_vault",
            "subscription_deposit",
            "subscription_withdraw",
            "subscription_loan_rejected",
            "investment_deposit_rejected",
            "investment_withdraw_rejected",
            "redemption_loan_rejected",
            "redemption_deposit_rejected",
            "redemption_withdraw",
        ],
        "E4": ["create_loan_broker", "deposit_first_loss_cover"],
        "E5": [
            "borrower_set_2_of_2",
            "borrower_payment_research_only",
            "borrower_payment_policy_only",
            "borrower_payment_research_and_policy",
        ],
        "E6": [
            "loan_set_research_only",
            "loan_set_policy_only",
            "loan_set_missing_counterparty_signature",
            "loan_set_altered_terms",
            "investment_loan_a_created",
        ],
        "E7": ["investment_loan_a_created", "investment_loan_b_created"],
        "E8": [
            "loan_a_early_partial_rejected",
            "loan_a_early_overpayment",
            "loan_a_scheduled_payment",
        ],
        "E9": [
            "redemption_loan_rejected",
            "redemption_late_repayment",
            "redemption_withdraw",
        ],
    }
    claims = {
        "E1": "Pinned standalone rippled starts, closes, and enables the required amendments.",
        "E2": "Runtime-generated actors can hold and transfer locally issued tRLUSD.",
        "E3": "Closed-ended vault operations are gated by subscription, investment, and redemption phase.",
        "E4": "A Loan Broker with first-loss cover attaches to the closed-ended vault.",
        "E5": "The borrower account enforces ResearchBot plus Credence policy signer 2-of-2 while retaining its master key.",
        "E6": "Borrower 2-of-2 account authorization composes with distinct Loan Broker counterparty authorization over exact LoanSet terms.",
        "E7": "Valid LoanSet creates loan state and disburses principal with corresponding vault and broker state changes.",
        "E8": "Scheduled, early partial, and overpayment behavior is observable with exact results and loan state.",
        "E9": "Redemption rejects new loans while allowing repayment and lender withdrawal.",
    }
    results: dict[str, Any] = {
        "schema_version": 1,
        "overall": "PASS",
        "experiments": {},
    }
    for experiment, labels in experiment_labels.items():
        selected = [by_label[label] for label in labels]
        results["experiments"][experiment] = {
            "claim_tested": claims[experiment],
            "result": "PASS",
            "evidence_paths": [
                "evidence/ledger-state.json",
                "evidence/transactions.json",
            ],
            "transaction_hashes": [record["transaction_hash"] for record in selected],
            "ledger_indices": sorted(
                {record["ledger_index"] for record in selected if "ledger_index" in record}
            ),
            "notes": "See REPORT.md for observed, derived, and unresolved distinctions.",
        }
    write_json(RESULTS_PATH, results)
    print("All E1-E9 experiments completed with expected results.", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except UnexpectedLedgerResult as exc:
        print(f"STOP: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1) from exc
