"""Synthetic regression tests for the frozen Checkpoint 1D E5 oracle."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from xrpl.core.binarycodec import decode
from xrpl.models.transactions import Payment
from xrpl.transaction import multisign, sign
from xrpl.wallet import Wallet

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from e5_oracle import (  # noqa: E402
    authorization_assertions,
    negative_payment_assertions,
    public_signing_evidence,
    regular_key_assertions,
    signer_list_assertions,
    successful_payment_assertions,
)


def signer_state(
    borrower: Wallet,
    recipient: Wallet,
    research: Wallet,
    policy: Wallet,
    *,
    quorum: int = 2,
    regular_key: str | None = None,
) -> dict[str, Any]:
    account: dict[str, Any] = {
        "Account": borrower.address,
        "Balance": "1000000",
        "Flags": 0,
        "Sequence": 2,
    }
    if regular_key is not None:
        account["RegularKey"] = regular_key
    return {
        "borrower_account": account,
        "recipient_account": {
            "Account": recipient.address,
            "Balance": "1000000",
            "Flags": 0,
            "Sequence": 1,
        },
        "signer_lists": [
            {
                "SignerQuorum": quorum,
                "SignerEntries": [
                    {
                        "SignerEntry": {
                            "Account": research.address,
                            "SignerWeight": 1,
                        }
                    },
                    {
                        "SignerEntry": {
                            "Account": policy.address,
                            "SignerWeight": 1,
                        }
                    },
                ],
            }
        ],
    }


def wallets() -> tuple[Wallet, Wallet, Wallet, Wallet]:
    return Wallet.create(), Wallet.create(), Wallet.create(), Wallet.create()


def multisigned_payment(
    borrower: Wallet, recipient: Wallet, signers: list[Wallet]
) -> tuple[Payment, dict[str, Any]]:
    payment = Payment(
        account=borrower.address,
        destination=recipient.address,
        amount="1000",
        fee=str(10 * (1 + len(signers))),
        sequence=2,
        signing_pub_key="",
    )
    signed = multisign(payment, [sign(payment, wallet, multisign=True) for wallet in signers])
    evidence = public_signing_evidence(signed.to_xrpl(), signed.get_hash())
    return signed, evidence


def test_d1_exact_two_of_two_signer_list_passes() -> None:
    borrower, recipient, research, policy = wallets()
    checks = signer_list_assertions(
        signer_state(borrower, recipient, research, policy),
        research_account=research.address,
        policy_account=policy.address,
    )
    assert all(item.passed for item in checks)


def test_d2_wrong_signer_quorum_fails() -> None:
    borrower, recipient, research, policy = wallets()
    checks = signer_list_assertions(
        signer_state(borrower, recipient, research, policy, quorum=1),
        research_account=research.address,
        policy_account=policy.address,
    )
    assert not all(item.passed for item in checks)


def test_d3_regular_key_absence_and_enabled_master_pass() -> None:
    borrower, recipient, research, policy = wallets()
    checks = regular_key_assertions(signer_state(borrower, recipient, research, policy))
    assert all(item.passed for item in checks)


def test_d4_unexpected_regular_key_fails() -> None:
    borrower, recipient, research, policy = wallets()
    checks = regular_key_assertions(
        signer_state(
            borrower,
            recipient,
            research,
            policy,
            regular_key=research.address,
        )
    )
    assert not all(item.passed for item in checks)


def test_d5_research_only_public_authorization_is_exact() -> None:
    borrower, recipient, research, policy = wallets()
    _signed, evidence = multisigned_payment(borrower, recipient, [research])
    checks = authorization_assertions(
        evidence,
        borrower_account=borrower.address,
        expected_signers={research.address: research.public_key},
        borrower_master_public_key=borrower.public_key,
    )
    assert all(item.passed for item in checks)


def test_d6_policy_only_public_authorization_is_exact() -> None:
    borrower, recipient, research, policy = wallets()
    _signed, evidence = multisigned_payment(borrower, recipient, [policy])
    checks = authorization_assertions(
        evidence,
        borrower_account=borrower.address,
        expected_signers={policy.address: policy.public_key},
        borrower_master_public_key=borrower.public_key,
    )
    assert all(item.passed for item in checks)


def test_d7_two_signer_public_authorization_and_blob_are_exact() -> None:
    borrower, recipient, research, policy = wallets()
    signed, evidence = multisigned_payment(borrower, recipient, [research, policy])
    checks = authorization_assertions(
        evidence,
        borrower_account=borrower.address,
        expected_signers={
            research.address: research.public_key,
            policy.address: policy.public_key,
        },
        borrower_master_public_key=borrower.public_key,
    )
    assert all(item.passed for item in checks)
    assert decode(evidence["signed_transaction_blob"]) == signed.to_xrpl()


def test_d8_owner_master_public_authorization_is_exact() -> None:
    borrower, recipient, research, policy = wallets()
    payment = Payment(
        account=borrower.address,
        destination=recipient.address,
        amount="2000",
        fee="10",
        sequence=2,
    )
    signed = sign(payment, borrower)
    evidence = public_signing_evidence(signed.to_xrpl(), signed.get_hash())
    checks = authorization_assertions(
        evidence,
        borrower_account=borrower.address,
        expected_signers={},
        borrower_master_public_key=borrower.public_key,
    )
    assert all(item.passed for item in checks)
    assert decode(evidence["signed_transaction_blob"]) == signed.to_xrpl()


def test_d9_missing_expected_signer_fails() -> None:
    borrower, recipient, research, policy = wallets()
    _signed, evidence = multisigned_payment(borrower, recipient, [research])
    checks = authorization_assertions(
        evidence,
        borrower_account=borrower.address,
        expected_signers={
            research.address: research.public_key,
            policy.address: policy.public_key,
        },
        borrower_master_public_key=borrower.public_key,
    )
    assert not all(item.passed for item in checks)


def test_d10_negative_payment_requires_exact_protected_state() -> None:
    borrower, recipient, research, policy = wallets()
    state = signer_state(borrower, recipient, research, policy)
    assert all(item.passed for item in negative_payment_assertions(state, state))


def test_d11_negative_payment_mutation_fails() -> None:
    borrower, recipient, research, policy = wallets()
    before = signer_state(borrower, recipient, research, policy)
    after = signer_state(borrower, recipient, research, policy)
    after["recipient_account"]["Balance"] = "1001000"
    assert not all(item.passed for item in negative_payment_assertions(before, after))


def test_d12_successful_payment_checks_exact_amount_fee_and_sequences() -> None:
    borrower, recipient, research, policy = wallets()
    before = signer_state(borrower, recipient, research, policy)
    after = signer_state(borrower, recipient, research, policy)
    after["borrower_account"]["Balance"] = "998970"
    after["borrower_account"]["Sequence"] = 3
    after["recipient_account"]["Balance"] = "1001000"
    checks = successful_payment_assertions(before, after, amount=1000, fee=30)
    assert all(item.passed for item in checks)


def test_d13_wrong_successful_payment_delta_fails() -> None:
    borrower, recipient, research, policy = wallets()
    before = signer_state(borrower, recipient, research, policy)
    after = signer_state(borrower, recipient, research, policy)
    after["borrower_account"]["Balance"] = "998971"
    after["borrower_account"]["Sequence"] = 3
    after["recipient_account"]["Balance"] = "1001000"
    checks = successful_payment_assertions(before, after, amount=1000, fee=30)
    assert not all(item.passed for item in checks)


def test_d14_public_evidence_contains_no_private_material() -> None:
    borrower, recipient, research, _policy = wallets()
    _signed, evidence = multisigned_payment(borrower, recipient, [research])
    rendered = str(evidence).lower()
    assert "private" not in rendered
    assert "seed" not in rendered


def test_d15_checkpoint_scope_excludes_lending_transactions() -> None:
    source = (SCRIPTS / "run_checkpoint_1d.py").read_text()
    assert "LoanSet(" not in source
    assert "LoanPay(" not in source
    assert "LoanManage(" not in source
