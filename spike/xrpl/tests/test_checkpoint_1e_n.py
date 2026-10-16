"""Prospective frozen-oracle tests for Checkpoint 1E-N."""

from __future__ import annotations

import hashlib
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

from xrpl.core.binarycodec import encode_for_signing_counterparty
from xrpl.core.keypairs import sign as sign_message
from xrpl.models.transactions import LoanSet
from xrpl.transaction import multisign, sign
from xrpl.wallet import Wallet

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from e6_negative_oracle import (  # noqa: E402
    NEGATIVE_CASES,
    PROSPECTIVE_TIMING,
    NegativePreflightError,
    case_definitions,
    live_binding_assertions,
    n5_mutation_assertions,
    negative_authorization_assertions,
    require_live_bindings,
    require_static_preflight,
    static_preflight_assertions,
)
from e6_oracle import negative_state_assertions, public_signing_evidence  # noqa: E402
from run_checkpoint_1e_n import n6_state_assertions, preflight_payload  # noqa: E402


def _all_pass(checks: list[Any]) -> bool:
    return all(check.passed for check in checks)


def _broker_id() -> str:
    return hashlib.sha256(b"Checkpoint 1E-N LoanBroker").hexdigest().upper()


def _base_body(wallets: dict[str, Wallet], *, principal: str = "1000") -> dict[str, Any]:
    return LoanSet(
        account=wallets["borrower"].address,
        loan_broker_id=_broker_id(),
        counterparty=wallets["broker_owner"].address,
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
        fee="40",
        sequence=7,
        network_id=21_337,
        signing_pub_key="",
    ).to_xrpl()


def _wallets() -> dict[str, Wallet]:
    return {
        "borrower": Wallet.create(),
        "research_bot": Wallet.create(),
        "credence_policy": Wallet.create(),
        "broker_owner": Wallet.create(),
    }


def _signed_case(
    *,
    signer_roles: tuple[str, ...],
    broker_mode: str,
    principal: str = "1000",
    broker_signed_principal: str | None = None,
) -> tuple[dict[str, Any], dict[str, Wallet], dict[str, Any], dict[str, Any] | None]:
    wallets = _wallets()
    body = _base_body(wallets, principal=principal)
    body_x: dict[str, Any] | None = None
    if broker_mode != "missing":
        body_x = dict(body)
        if broker_signed_principal is not None:
            body_x["PrincipalRequested"] = broker_signed_principal
        payload = bytes.fromhex(encode_for_signing_counterparty(body_x))
        signature = sign_message(payload, wallets["broker_owner"].private_key)
        if broker_mode == "invalid":
            signature = "00"
        body["CounterpartySignature"] = {
            "SigningPubKey": wallets["broker_owner"].public_key,
            "TxnSignature": signature,
        }
    counterparty_signed = LoanSet.from_xrpl(body)
    partials = [sign(counterparty_signed, wallets[role], multisign=True) for role in signer_roles]
    signed = multisign(counterparty_signed, partials)
    transaction = signed.to_xrpl()
    evidence = public_signing_evidence(transaction, signed.get_hash())
    return transaction, wallets, evidence, body_x


def _terms(transaction: dict[str, Any]) -> dict[str, Any]:
    return {
        field: transaction.get(field)
        for field in (
            "LoanBrokerID",
            "Counterparty",
            "PrincipalRequested",
            "LoanOriginationFee",
            "LoanServiceFee",
            "LatePaymentFee",
            "ClosePaymentFee",
            "OverpaymentFee",
            "InterestRate",
            "LateInterestRate",
            "CloseInterestRate",
            "OverpaymentInterestRate",
            "PaymentTotal",
            "PaymentInterval",
            "GracePeriod",
        )
    }


def _auth_checks(
    transaction: dict[str, Any],
    wallets: dict[str, Wallet],
    evidence: dict[str, Any],
    signer_roles: tuple[str, ...],
    broker_mode: str,
) -> list[Any]:
    return negative_authorization_assertions(
        evidence,
        borrower_account=wallets["borrower"].address,
        expected_borrower_signers={
            wallets[role].address: wallets[role].public_key for role in signer_roles
        },
        broker_owner_account=wallets["broker_owner"].address,
        broker_owner_public_key=wallets["broker_owner"].public_key,
        expected_terms=_terms(transaction),
        broker_mode=broker_mode,  # type: ignore[arg-type]
    )


def test_n_preflight_defines_exact_n1_n6_order_and_results() -> None:
    require_static_preflight()
    assert _all_pass(static_preflight_assertions())
    assert [case.case_id for case in NEGATIVE_CASES] == [
        "N1",
        "N2",
        "N3",
        "N4",
        "N5",
        "N6",
    ]


def test_n_preflight_payload_excludes_prohibited_execution() -> None:
    value = preflight_payload()
    assert value["result"] == "PASS"
    assert value["new_xrpl_transactions"] == 0
    assert value["xrpld_started"] is False
    assert value["positive_loanset_included"] is False
    assert value["loan_pay_included"] is False
    assert value["e7_e8_e9_included"] is False


def test_n_prospective_timing_uses_applying_view_not_lookup_date() -> None:
    assert PROSPECTIVE_TIMING["StartDate"] == (
        "applying view header closeTime read during LoanSet::doApply"
    )
    assert PROSPECTIVE_TIMING["NextPaymentDueDate"] == "StartDate + PaymentInterval"
    assert PROSPECTIVE_TIMING["PaymentInterval"] == 60
    assert PROSPECTIVE_TIMING["GracePeriod"] == 60
    assert "lookup" not in PROSPECTIVE_TIMING["StartDate"]


def test_n1_researchbot_only_and_valid_broker_evidence_passes_preflight() -> None:
    transaction, wallets, evidence, _ = _signed_case(
        signer_roles=("research_bot",), broker_mode="valid"
    )
    assert _all_pass(_auth_checks(transaction, wallets, evidence, ("research_bot",), "valid"))


def test_n2_credence_only_and_valid_broker_evidence_passes_preflight() -> None:
    transaction, wallets, evidence, _ = _signed_case(
        signer_roles=("credence_policy",), broker_mode="valid"
    )
    assert _all_pass(_auth_checks(transaction, wallets, evidence, ("credence_policy",), "valid"))


def test_n3_two_borrower_signers_and_missing_broker_evidence_passes_preflight() -> None:
    roles = ("research_bot", "credence_policy")
    transaction, wallets, evidence, _ = _signed_case(signer_roles=roles, broker_mode="missing")
    assert _all_pass(_auth_checks(transaction, wallets, evidence, roles, "missing"))


def test_n4_two_borrower_signers_and_invalid_broker_evidence_passes_preflight() -> None:
    roles = ("research_bot", "credence_policy")
    transaction, wallets, evidence, _ = _signed_case(signer_roles=roles, broker_mode="invalid")
    assert _all_pass(_auth_checks(transaction, wallets, evidence, roles, "invalid"))


def test_n5_only_principal_differs_and_broker_signature_binds_x_not_y() -> None:
    roles = ("research_bot", "credence_policy")
    transaction, wallets, evidence, body_x = _signed_case(
        signer_roles=roles,
        broker_mode="valid_for_x_only",
        principal="1001",
        broker_signed_principal="1000",
    )
    assert body_x is not None
    checks = _auth_checks(transaction, wallets, evidence, roles, "valid_for_x_only")
    unsigned_y = {
        key: value
        for key, value in transaction.items()
        if key not in {"CounterpartySignature", "Signers", "TxnSignature"}
    }
    counterparty = transaction["CounterpartySignature"]
    checks.extend(
        n5_mutation_assertions(
            broker_body_x=body_x,
            submitted_body_y=unsigned_y,
            broker_public_key=counterparty["SigningPubKey"],
            broker_signature=counterparty["TxnSignature"],
        )
    )
    assert _all_pass(checks)


def test_n_negative_protected_state_mutation_fails() -> None:
    before = {
        "loan": None,
        "vault": {"AssetsAvailable": "100000"},
        "borrower_account": {"Sequence": 7},
    }
    after = deepcopy(before)
    after["vault"]["AssetsAvailable"] = "99999"
    checks = negative_state_assertions(
        observed_result="tefBAD_QUORUM",
        expected_result="tefBAD_QUORUM",
        before=before,
        after=after,
    )
    assert not _all_pass(checks)


def test_n6_allows_only_exact_fee_and_sequence_effects() -> None:
    before = {
        "loan": None,
        "borrower_account": {
            "Account": "borrower",
            "Balance": "1000",
            "OwnerCount": 2,
            "Sequence": 7,
        },
        "vault": {"AssetsAvailable": "100000"},
    }
    after = deepcopy(before)
    after["borrower_account"]["Balance"] = "960"
    after["borrower_account"]["Sequence"] = 8
    assert _all_pass(
        n6_state_assertions(before=before, after=after, observed="tecTOO_SOON", fee=40)
    )


def test_n6_wrong_fee_or_lending_mutation_fails() -> None:
    before = {
        "loan": None,
        "borrower_account": {
            "Account": "borrower",
            "Balance": "1000",
            "OwnerCount": 2,
            "Sequence": 7,
        },
        "vault": {"AssetsAvailable": "100000"},
    }
    after = deepcopy(before)
    after["borrower_account"]["Balance"] = "959"
    after["borrower_account"]["Sequence"] = 8
    after["vault"]["AssetsAvailable"] = "99999"
    assert not _all_pass(
        n6_state_assertions(before=before, after=after, observed="tecTOO_SOON", fee=40)
    )


def _complete_bindings() -> dict[str, Any]:
    expected = {
        "N1": "tefBAD_QUORUM",
        "N2": "tefBAD_QUORUM",
        "N3": "temBAD_SIGNER",
        "N4": "local invalidTransaction",
        "N5": "local invalidTransaction",
        "N6": "tecTOO_SOON",
    }
    cases = {
        case_id: {
            "expected_loan_absent_before": True,
            "authorization_assertions_pass": True,
            "expected_result": expected[case_id],
        }
        for case_id in ("N1", "N2", "N3", "N4", "N5", "N6")
    }
    return {
        "actors": {"borrower": "bound"},
        "amendments": ["bound"],
        "assertion_spec_hash": "bound",
        "case_constructions": cases,
        "evidence_destination": "bound",
        "git_commit": "bound",
        "main_fixture": {"vault": "bound"},
        "negative_oracle_hash": "bound",
        "network_id": 21_337,
        "preflight_hash": "bound",
        "rippled_source_commit": "bound",
        "rippled_version": "3.4.0",
        "runner_hash": "bound",
        "tests_hash": "bound",
        "timing_spec_hash": "bound",
        "wrong_phase_fixture": {"vault": "bound"},
    }


def test_n_live_binding_accepts_complete_negative_only_program() -> None:
    bindings = _complete_bindings()
    assert _all_pass(live_binding_assertions(bindings))
    require_live_bindings(bindings)


def test_n_live_binding_rejects_existing_loan_dependency() -> None:
    bindings = _complete_bindings()
    bindings["case_constructions"]["N4"]["expected_loan_absent_before"] = False
    assert not _all_pass(live_binding_assertions(bindings))
    try:
        require_live_bindings(bindings)
    except NegativePreflightError as exc:
        assert "existing Loan" in str(exc)
    else:
        raise AssertionError("existing Loan dependency was accepted")


def test_n_case_definitions_are_json_serializable_complete_records() -> None:
    definitions = case_definitions()
    assert len(definitions) == 6
    assert all(
        set(row)
        == {
            "case_id",
            "borrower_signers",
            "broker_mode",
            "principal",
            "broker_signed_principal",
            "expected_result",
            "expect_validated",
            "phase",
        }
        for row in definitions
    )
