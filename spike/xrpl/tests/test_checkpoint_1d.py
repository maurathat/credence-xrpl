"""Synthetic regression tests for the frozen Checkpoint 1D E5 oracle."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from xrpl.core.binarycodec import decode
from xrpl.models.transactions import Payment
from xrpl.transaction import multisign, sign
from xrpl.wallet import Wallet

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
ROOT = SCRIPTS.parent
CHECKPOINT_EVIDENCE = ROOT / "evidence" / "checkpoint-1d"
sys.path.insert(0, str(SCRIPTS))

from e5_oracle import (  # noqa: E402
    LSF_DISABLE_MASTER,
    SignerListResponseError,
    authorization_assertions,
    negative_payment_assertions,
    parse_account_info_signer_state,
    public_signing_evidence,
    regular_key_assertions,
    signer_list_assertions,
    successful_payment_assertions,
    validate_expected_signer_list_response,
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


def committed_signer_fixture() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    record = json.loads(
        (CHECKPOINT_EVIDENCE / "transactions/05-borrower_set_2_of_2_signer_list.json").read_text()
    )
    actors = json.loads((CHECKPOINT_EVIDENCE / "actors-public.json").read_text())
    account_info_result = {"account_data": copy.deepcopy(record["after_state"]["borrower_account"])}
    return record, actors, account_info_result


def validate_committed_fixture(result: dict[str, Any]) -> dict[str, Any]:
    _record, actors, _result = committed_signer_fixture()
    return validate_expected_signer_list_response(
        result,
        research_account=actors["research_bot_signer"]["account"],
        policy_account=actors["credence_policy_signer"]["account"],
    )


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


def test_s1_committed_response_reproduces_false_block_and_now_passes() -> None:
    record, actors, result = committed_signer_fixture()
    legacy_checks = signer_list_assertions(
        record["after_state"],
        research_account=actors["research_bot_signer"]["account"],
        policy_account=actors["credence_policy_signer"]["account"],
    )
    assert record["after_state"]["signer_lists"] == []
    assert not all(item.passed for item in legacy_checks)

    state = validate_committed_fixture(result)
    assert state["signer_lists"][0]["SignerQuorum"] == 2
    assert all(
        item.passed
        for item in signer_list_assertions(
            state,
            research_account=actors["research_bot_signer"]["account"],
            policy_account=actors["credence_policy_signer"]["account"],
        )
    )


def test_s2_missing_account_data_fails() -> None:
    _record, _actors, result = committed_signer_fixture()
    with pytest.raises(SignerListResponseError, match="no account_data"):
        validate_committed_fixture({"signer_lists": result["account_data"]["signer_lists"]})


def test_s3_missing_signer_lists_fails() -> None:
    _record, _actors, result = committed_signer_fixture()
    top_level_lists = result["account_data"].pop("signer_lists")
    result["signer_lists"] = top_level_lists
    with pytest.raises(SignerListResponseError, match="no signer_lists"):
        validate_committed_fixture(result)


def test_s4_wrong_quorum_fails() -> None:
    _record, _actors, result = committed_signer_fixture()
    result["account_data"]["signer_lists"][0]["SignerQuorum"] = 1
    with pytest.raises(SignerListResponseError, match="SignerQuorum"):
        validate_committed_fixture(result)


def test_s5_missing_research_bot_fails() -> None:
    _record, actors, result = committed_signer_fixture()
    entries = result["account_data"]["signer_lists"][0]["SignerEntries"]
    entries[:] = [
        row
        for row in entries
        if row["SignerEntry"]["Account"] != actors["research_bot_signer"]["account"]
    ]
    with pytest.raises(SignerListResponseError, match="signer entries"):
        validate_committed_fixture(result)


def test_s6_missing_credence_signer_fails() -> None:
    _record, actors, result = committed_signer_fixture()
    entries = result["account_data"]["signer_lists"][0]["SignerEntries"]
    entries[:] = [
        row
        for row in entries
        if row["SignerEntry"]["Account"] != actors["credence_policy_signer"]["account"]
    ]
    with pytest.raises(SignerListResponseError, match="signer entries"):
        validate_committed_fixture(result)


def test_s7_wrong_signer_weight_fails() -> None:
    _record, _actors, result = committed_signer_fixture()
    result["account_data"]["signer_lists"][0]["SignerEntries"][0]["SignerEntry"]["SignerWeight"] = 2
    with pytest.raises(SignerListResponseError, match="signer entries"):
        validate_committed_fixture(result)


def test_s8_unexpected_extra_signer_fails() -> None:
    _record, actors, result = committed_signer_fixture()
    result["account_data"]["signer_lists"][0]["SignerEntries"].append(
        {
            "SignerEntry": {
                "Account": actors["payment_recipient"]["account"],
                "SignerWeight": 1,
            }
        }
    )
    with pytest.raises(SignerListResponseError, match="signer entries"):
        validate_committed_fixture(result)


def test_s9_reordered_signer_entries_pass() -> None:
    _record, _actors, result = committed_signer_fixture()
    result["account_data"]["signer_lists"][0]["SignerEntries"].reverse()
    validate_committed_fixture(result)


def test_s10_duplicate_signer_identity_fails() -> None:
    _record, _actors, result = committed_signer_fixture()
    entries = result["account_data"]["signer_lists"][0]["SignerEntries"]
    entries[1] = copy.deepcopy(entries[0])
    with pytest.raises(SignerListResponseError, match="signer entries"):
        validate_committed_fixture(result)


def test_s11_absent_regular_key_is_distinct_from_configured() -> None:
    _record, actors, result = committed_signer_fixture()
    state = parse_account_info_signer_state(result)
    assert all(item.passed for item in regular_key_assertions(state))

    state["borrower_account"]["RegularKey"] = actors["research_bot_signer"]["account"]
    assert not all(item.passed for item in regular_key_assertions(state))


def test_s12_clear_master_key_flag_is_distinct_from_set() -> None:
    _record, _actors, result = committed_signer_fixture()
    state = parse_account_info_signer_state(result)
    assert all(item.passed for item in regular_key_assertions(state))

    state["borrower_account"]["Flags"] |= LSF_DISABLE_MASTER
    assert not all(item.passed for item in regular_key_assertions(state))


def test_repaired_parser_is_bound_into_future_run_provenance() -> None:
    source = (SCRIPTS / "run_checkpoint_1d.py").read_text()
    assert '"signer_list_parser_binding"' in source
    assert '"path": "scripts/e5_oracle.py"' in source
    assert '"sha256": sha256_file(ORACLE_PATH)' in source
