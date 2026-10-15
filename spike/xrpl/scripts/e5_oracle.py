"""Frozen semantic assertions and public authorization evidence for Checkpoint 1D."""

from __future__ import annotations

from typing import Any

from harness import StateAssertion, predicate_assertion, state_assertion
from xrpl.core.binarycodec import encode

LSF_DISABLE_MASTER = 0x00100000


class SignerListResponseError(ValueError):
    """The pinned account_info response cannot prove the expected SignerList state."""


def parse_account_info_signer_state(result: dict[str, Any]) -> dict[str, Any]:
    """Read the API-v1 SignerList location emitted by pinned rippled 3.4.0.

    The Checkpoint 1D request did not select API v2. Pinned source places the
    requested list under ``account_data.signer_lists`` for API v1. Deliberately
    do not guess at the API-v2 top-level location.
    """

    account = result.get("account_data")
    if not isinstance(account, dict):
        raise SignerListResponseError("account_info result has no account_data object")
    if "signer_lists" not in account:
        raise SignerListResponseError("account_info account_data has no signer_lists member")
    lists = account["signer_lists"]
    if not isinstance(lists, list):
        raise SignerListResponseError("account_data.signer_lists is not an array")
    return {"borrower_account": account, "signer_lists": lists}


def public_signing_evidence(transaction: dict[str, Any], tx_hash: str) -> dict[str, Any]:
    """Retain public signature material and a canonical signed blob, never private material."""

    signer_rows = []
    for wrapper in transaction.get("Signers", []):
        signer = wrapper.get("Signer", {})
        signer_rows.append(
            {
                "account": signer.get("Account"),
                "public_key_hex": signer.get("SigningPubKey"),
                "txn_signature_hex": signer.get("TxnSignature"),
            }
        )
    top_signature = transaction.get("TxnSignature")
    fields = {
        key: value
        for key, value in transaction.items()
        if key not in {"Signers", "SigningPubKey", "TxnSignature"}
    }
    return {
        "mode": "multisign" if signer_rows else "master_single_sign",
        "account": transaction.get("Account"),
        "account_sequence": transaction.get("Sequence"),
        "transaction_hash": tx_hash,
        "signed_transaction_blob": encode(transaction),
        "transaction_fields": fields,
        "top_level_signing_public_key": transaction.get("SigningPubKey"),
        "top_level_transaction_signature_present": isinstance(top_signature, str)
        and bool(top_signature),
        "top_level_txn_signature_hex": top_signature,
        "signers_array_present": bool(signer_rows),
        "signers": signer_rows,
    }


def _signer_map(state: dict[str, Any]) -> dict[str, int] | None:
    lists = state.get("signer_lists", [])
    if not isinstance(lists, list) or len(lists) != 1 or not isinstance(lists[0], dict):
        return None
    rows = lists[0].get("SignerEntries", [])
    if not isinstance(rows, list):
        return None
    result: dict[str, int] = {}
    for wrapper in rows:
        if not isinstance(wrapper, dict):
            return None
        entry = wrapper.get("SignerEntry", {})
        if not isinstance(entry, dict):
            return None
        account = entry.get("Account")
        weight = entry.get("SignerWeight")
        if not isinstance(account, str) or not isinstance(weight, int) or account in result:
            return None
        result[account] = weight
    return result


def signer_list_assertions(
    state: dict[str, Any],
    *,
    research_account: str,
    policy_account: str,
) -> list[StateAssertion]:
    lists = state.get("signer_lists", [])
    if not isinstance(lists, list):
        lists = []
    signer_list = lists[0] if len(lists) == 1 and isinstance(lists[0], dict) else {}
    return [
        state_assertion("borrower SignerList count", len(lists), 1),
        state_assertion("borrower SignerQuorum", signer_list.get("SignerQuorum"), 2),
        state_assertion(
            "borrower signer entries",
            _signer_map(state),
            {research_account: 1, policy_account: 1},
        ),
    ]


def validate_expected_signer_list_response(
    result: dict[str, Any],
    *,
    research_account: str,
    policy_account: str,
) -> dict[str, Any]:
    """Parse and fail closed unless the response proves the exact 2-of-2 list."""

    state = parse_account_info_signer_state(result)
    failures = [
        assertion.name
        for assertion in signer_list_assertions(
            state,
            research_account=research_account,
            policy_account=policy_account,
        )
        if not assertion.passed
    ]
    if failures:
        raise SignerListResponseError(
            "account_info SignerList validation failed: " + ", ".join(failures)
        )
    return state


def regular_key_assertions(state: dict[str, Any]) -> list[StateAssertion]:
    account = state.get("borrower_account") or {}
    flags = int(account.get("Flags", 0))
    return [
        predicate_assertion(
            "no RegularKey configured",
            "RegularKey" not in account,
            False,
            "RegularKey" in account,
        ),
        predicate_assertion(
            "borrower master key remains enabled",
            flags & LSF_DISABLE_MASTER == 0,
            0,
            flags & LSF_DISABLE_MASTER,
        ),
    ]


def authorization_assertions(
    evidence: dict[str, Any],
    *,
    borrower_account: str,
    expected_signers: dict[str, str],
    borrower_master_public_key: str,
) -> list[StateAssertion]:
    observed_signers = {
        row.get("account"): row.get("public_key_hex") for row in evidence.get("signers", [])
    }
    multisign = bool(expected_signers)
    checks = [
        state_assertion("authorization account", evidence.get("account"), borrower_account),
        state_assertion(
            "authorization mode",
            evidence.get("mode"),
            "multisign" if multisign else "master_single_sign",
        ),
        state_assertion(
            "authorization signer identities and keys", observed_signers, expected_signers
        ),
        predicate_assertion(
            "signed transaction blob retained",
            isinstance(evidence.get("signed_transaction_blob"), str)
            and bool(evidence.get("signed_transaction_blob")),
            True,
            bool(evidence.get("signed_transaction_blob")),
        ),
        predicate_assertion(
            "transaction hash retained",
            isinstance(evidence.get("transaction_hash"), str)
            and len(evidence.get("transaction_hash", "")) == 64,
            True,
            evidence.get("transaction_hash"),
        ),
    ]
    if multisign:
        checks.extend(
            [
                state_assertion(
                    "multisign top-level SigningPubKey",
                    evidence.get("top_level_signing_public_key"),
                    "",
                ),
                state_assertion(
                    "multisign top-level TxnSignature absent",
                    evidence.get("top_level_transaction_signature_present"),
                    False,
                ),
                state_assertion(
                    "multisign Signers present", evidence.get("signers_array_present"), True
                ),
            ]
        )
    else:
        checks.extend(
            [
                state_assertion(
                    "master SigningPubKey",
                    evidence.get("top_level_signing_public_key"),
                    borrower_master_public_key,
                ),
                state_assertion(
                    "master TxnSignature present",
                    evidence.get("top_level_transaction_signature_present"),
                    True,
                ),
                state_assertion(
                    "master Signers absent", evidence.get("signers_array_present"), False
                ),
            ]
        )
    for row in evidence.get("signers", []):
        checks.extend(
            [
                predicate_assertion(
                    f"{row.get('account')} public key retained",
                    isinstance(row.get("public_key_hex"), str) and bool(row.get("public_key_hex")),
                    True,
                    bool(row.get("public_key_hex")),
                ),
                predicate_assertion(
                    f"{row.get('account')} signature retained",
                    isinstance(row.get("txn_signature_hex"), str)
                    and bool(row.get("txn_signature_hex")),
                    True,
                    bool(row.get("txn_signature_hex")),
                ),
            ]
        )
    return checks


def negative_payment_assertions(
    before: dict[str, Any], after: dict[str, Any]
) -> list[StateAssertion]:
    return [
        state_assertion("negative protected state", after, before),
        state_assertion(
            "negative borrower sequence",
            (after.get("borrower_account") or {}).get("Sequence"),
            (before.get("borrower_account") or {}).get("Sequence"),
        ),
        state_assertion(
            "negative recipient balance",
            (after.get("recipient_account") or {}).get("Balance"),
            (before.get("recipient_account") or {}).get("Balance"),
        ),
    ]


def successful_payment_assertions(
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    amount: int,
    fee: int,
) -> list[StateAssertion]:
    before_borrower = before.get("borrower_account") or {}
    after_borrower = after.get("borrower_account") or {}
    before_recipient = before.get("recipient_account") or {}
    after_recipient = after.get("recipient_account") or {}
    return [
        state_assertion(
            "recipient exact Payment delta",
            int(after_recipient.get("Balance", 0)) - int(before_recipient.get("Balance", 0)),
            amount,
        ),
        state_assertion(
            "borrower exact Payment plus fee delta",
            int(after_borrower.get("Balance", 0)) - int(before_borrower.get("Balance", 0)),
            -(amount + fee),
        ),
        state_assertion(
            "borrower sequence delta",
            int(after_borrower.get("Sequence", 0)) - int(before_borrower.get("Sequence", 0)),
            1,
        ),
        state_assertion(
            "recipient sequence unchanged",
            after_recipient.get("Sequence"),
            before_recipient.get("Sequence"),
        ),
        state_assertion(
            "borrower SignerList unchanged", after.get("signer_lists"), before.get("signer_lists")
        ),
    ]
