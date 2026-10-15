"""Synthetic and source-derived tests for the frozen Checkpoint 1E E6 oracle."""

from __future__ import annotations

import hashlib
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from xrpl.core.binarycodec import encode_for_signing_counterparty
from xrpl.core.keypairs import sign as sign_message
from xrpl.models.transactions import LoanSet
from xrpl.transaction import multisign, sign
from xrpl.wallet import Wallet

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from e6_oracle import (  # noqa: E402
    BORROWER_MULTISIGN_PREFIX_HEX,
    COUNTERPARTY_SIGN_PREFIX_HEX,
    LOAN_TERM_FIELDS,
    REQUIRED_LIVE_BINDINGS,
    E6BindingError,
    authorization_assertions,
    live_binding_assertions,
    loan_index,
    negative_state_assertions,
    positive_post_state_assertions,
    public_signing_evidence,
    require_live_bindings,
)


def _all_pass(checks: list[Any]) -> bool:
    return all(check.passed for check in checks)


def _broker_id() -> str:
    return hashlib.sha256(b"synthetic E6 LoanBroker identity").hexdigest().upper()


def _vault_id() -> str:
    return hashlib.sha256(b"synthetic E6 Vault identity").hexdigest().upper()


def _signed_fixture(
    borrower_signer_names: tuple[str, ...] = ("research", "policy"),
) -> tuple[dict[str, Any], dict[str, Wallet], dict[str, Any]]:
    wallets = {
        "borrower": Wallet.create(),
        "research": Wallet.create(),
        "policy": Wallet.create(),
        "broker_owner": Wallet.create(),
    }
    borrower = wallets["borrower"]
    broker_owner = wallets["broker_owner"]
    transaction = LoanSet(
        account=borrower.address,
        loan_broker_id=_broker_id(),
        counterparty=broker_owner.address,
        principal_requested="1000",
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
    )
    tx_json = transaction.to_xrpl()
    counterparty_payload = bytes.fromhex(encode_for_signing_counterparty(tx_json))
    counterparty_signature = sign_message(counterparty_payload, broker_owner.private_key)
    tx_json["CounterpartySignature"] = {
        "SigningPubKey": broker_owner.public_key,
        "TxnSignature": counterparty_signature,
    }
    counterparty_signed = LoanSet.from_xrpl(tx_json)
    partials = [
        sign(counterparty_signed, wallets[name], multisign=True) for name in borrower_signer_names
    ]
    signed = multisign(counterparty_signed, partials)
    public = signed.to_xrpl()
    evidence = public_signing_evidence(public, signed.get_hash())
    return public, wallets, evidence


def _expected_terms(transaction: dict[str, Any]) -> dict[str, Any]:
    return {field: transaction.get(field) for field in LOAN_TERM_FIELDS}


def _authorization_checks(
    transaction: dict[str, Any], wallets: dict[str, Wallet], evidence: dict[str, Any]
) -> list[Any]:
    return authorization_assertions(
        evidence,
        borrower_account=wallets["borrower"].address,
        expected_borrower_signers={
            wallets["research"].address: wallets["research"].public_key,
            wallets["policy"].address: wallets["policy"].public_key,
        },
        broker_owner_account=wallets["broker_owner"].address,
        broker_owner_public_key=wallets["broker_owner"].public_key,
        expected_terms=_expected_terms(transaction),
    )


def _positive_fixture(
    wallets: dict[str, Wallet],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    broker_id = _broker_id()
    vault_id = _vault_id()
    signer_lists = [
        {
            "SignerQuorum": 2,
            "SignerEntries": [
                {"SignerEntry": {"Account": wallets["research"].address, "SignerWeight": 1}},
                {"SignerEntry": {"Account": wallets["policy"].address, "SignerWeight": 1}},
            ],
        }
    ]
    before = {
        "loan": None,
        "borrower_account": {
            "Account": wallets["borrower"].address,
            "OwnerCount": 2,
            "Sequence": 7,
        },
        "borrower_balance": "0",
        "broker_owner_balance": "10000",
        "vault_pseudo_balance": "5000",
        "signer_lists": signer_lists,
        "vault": {
            "LedgerEntryType": "Vault",
            "index": vault_id,
            "LEVersion": 1,
            "AssetsAvailable": "5000",
            "AssetsTotal": "5000",
        },
        "loan_broker": {
            "LedgerEntryType": "LoanBroker",
            "index": broker_id,
            "Owner": wallets["broker_owner"].address,
            "VaultID": vault_id,
            "LoanSequence": 8,
            "OwnerCount": 0,
            "DebtTotal": "0",
            "CoverAvailable": "10000",
        },
    }
    after = deepcopy(before)
    after.update(
        {
            "borrower_balance": "1000",
            "broker_owner_balance": "10000",
            "vault_pseudo_balance": "4000",
        }
    )
    after["borrower_account"]["OwnerCount"] = 3
    after["borrower_account"]["Sequence"] = 8
    after["vault"]["AssetsAvailable"] = "4000"
    after["loan_broker"].update({"DebtTotal": "1000", "LoanSequence": 9, "OwnerCount": 1})
    after["loan"] = {
        "LedgerEntryType": "Loan",
        "index": loan_index(broker_id, 8),
        "LoanBrokerID": broker_id,
        "LoanSequence": 8,
        "Borrower": wallets["borrower"].address,
        "LoanOriginationFee": "0",
        "LoanServiceFee": "0",
        "LatePaymentFee": "0",
        "ClosePaymentFee": "0",
        "OverpaymentFee": 0,
        "InterestRate": 0,
        "LateInterestRate": 0,
        "CloseInterestRate": 0,
        "OverpaymentInterestRate": 0,
        "GracePeriod": 60,
        "PrincipalOutstanding": "1000",
        "StartDate": 900,
        "PaymentInterval": 60,
        "PaymentRemaining": 1,
        "PreviousPaymentDueDate": 0,
        "NextPaymentDueDate": 960,
        "PeriodicPayment": "1000",
        "TotalValueOutstanding": "1000",
        "ManagementFeeOutstanding": "0",
    }
    terms = {
        "Account": wallets["borrower"].address,
        "Counterparty": wallets["broker_owner"].address,
        "VaultID": vault_id,
        "LoanBrokerID": broker_id,
        "PrincipalRequested": "1000",
        "LoanOriginationFee": "0",
        "LoanServiceFee": "0",
        "LatePaymentFee": "0",
        "ClosePaymentFee": "0",
        "OverpaymentFee": 0,
        "InterestRate": 0,
        "LateInterestRate": 0,
        "CloseInterestRate": 0,
        "OverpaymentInterestRate": 0,
        "PaymentTotal": 1,
        "PaymentInterval": 60,
        "GracePeriod": 60,
    }
    return before, after, terms


def test_d1_missing_researchbot_borrower_signer_fails() -> None:
    transaction, wallets, evidence = _signed_fixture(("policy",))
    assert not _all_pass(_authorization_checks(transaction, wallets, evidence))


def test_d2_missing_credence_borrower_signer_fails() -> None:
    transaction, wallets, evidence = _signed_fixture(("research",))
    assert not _all_pass(_authorization_checks(transaction, wallets, evidence))


def test_d3_missing_broker_counterparty_authorization_fails() -> None:
    transaction, wallets, _evidence = _signed_fixture()
    transaction.pop("CounterpartySignature")
    evidence = public_signing_evidence(transaction, "0" * 64)
    assert not _all_pass(_authorization_checks(transaction, wallets, evidence))


def test_d4_invalid_counterparty_signature_fails() -> None:
    transaction, wallets, _evidence = _signed_fixture()
    transaction["CounterpartySignature"]["TxnSignature"] = "00"
    evidence = public_signing_evidence(transaction, "0" * 64)
    assert not _all_pass(_authorization_checks(transaction, wallets, evidence))


def test_d5_changed_signed_principal_invalidates_authorization() -> None:
    transaction, wallets, _evidence = _signed_fixture()
    transaction["PrincipalRequested"] = "1001"
    evidence = public_signing_evidence(transaction, "0" * 64)
    checks = _authorization_checks(transaction, wallets, evidence)
    broker_check = next(
        check for check in checks if check.name == "broker counterparty signature valid"
    )
    assert not broker_check.passed


def test_d6_tessuccess_without_loan_post_state_fails() -> None:
    _transaction, wallets, _evidence = _signed_fixture()
    before, after, terms = _positive_fixture(wallets)
    assert _all_pass(
        positive_post_state_assertions(
            validated_result="tesSUCCESS",
            before=before,
            after=after,
            terms=terms,
            expected_start_date=900,
        )
    )
    after["loan"] = None
    checks = positive_post_state_assertions(
        validated_result="tesSUCCESS",
        before=before,
        after=after,
        terms=terms,
        expected_start_date=900,
    )
    assert not _all_pass(checks)


def test_d7_wrong_loan_borrower_fails() -> None:
    _transaction, wallets, _evidence = _signed_fixture()
    before, after, terms = _positive_fixture(wallets)
    after["loan"]["Borrower"] = wallets["policy"].address
    assert not _all_pass(
        positive_post_state_assertions(
            validated_result="tesSUCCESS",
            before=before,
            after=after,
            terms=terms,
            expected_start_date=900,
        )
    )


def test_d8_wrong_vault_relationship_fails() -> None:
    _transaction, wallets, _evidence = _signed_fixture()
    before, after, terms = _positive_fixture(wallets)
    after["loan_broker"]["VaultID"] = _broker_id()
    assert not _all_pass(
        positive_post_state_assertions(
            validated_result="tesSUCCESS",
            before=before,
            after=after,
            terms=terms,
            expected_start_date=900,
        )
    )


def test_d9_wrong_loan_broker_relationship_fails() -> None:
    _transaction, wallets, _evidence = _signed_fixture()
    before, after, terms = _positive_fixture(wallets)
    after["loan"]["LoanBrokerID"] = _vault_id()
    assert not _all_pass(
        positive_post_state_assertions(
            validated_result="tesSUCCESS",
            before=before,
            after=after,
            terms=terms,
            expected_start_date=900,
        )
    )


def test_d10_negative_protected_state_mutation_fails() -> None:
    before = {"borrower": {"Sequence": 7}, "loan": None, "vault": {"AssetsAvailable": "5000"}}
    after = deepcopy(before)
    after["vault"]["AssetsAvailable"] = "4999"
    checks = negative_state_assertions(
        observed_result="tefBAD_QUORUM",
        expected_result="tefBAD_QUORUM",
        before=before,
        after=after,
    )
    assert not _all_pass(checks)


def test_d11_borrower_and_broker_authorization_are_independently_captured() -> None:
    transaction, wallets, evidence = _signed_fixture()
    assert _all_pass(_authorization_checks(transaction, wallets, evidence))
    borrower = evidence["borrower_authorization"]
    broker = evidence["broker_counterparty_authorization"]
    assert {row["signing_prefix_hex"] for row in borrower["signers"]} == {
        BORROWER_MULTISIGN_PREFIX_HEX
    }
    assert broker["signing_prefix_hex"] == COUNTERPARTY_SIGN_PREFIX_HEX
    assert all(
        row["serialized_body_hex"] == broker["serialized_body_hex"] for row in borrower["signers"]
    )


def test_d12_positive_execution_requires_every_frozen_binding() -> None:
    complete = {name: f"bound:{name}" for name in REQUIRED_LIVE_BINDINGS}
    assert _all_pass(live_binding_assertions(complete))
    require_live_bindings(complete)
    incomplete = dict(complete)
    incomplete.pop("expected_loan_index")
    assert not _all_pass(live_binding_assertions(incomplete))
    with pytest.raises(E6BindingError, match="expected_loan_index"):
        require_live_bindings(incomplete)
