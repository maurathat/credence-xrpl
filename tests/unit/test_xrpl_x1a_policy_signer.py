"""X1a Core payment-authorization binding and the Credence policy signer."""

from __future__ import annotations

import os
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from x1a_world import (
    AMOUNT,
    ATTACKER,
    AUTHORITY,
    AUTHORITY_RETIRED,
    EXPIRES,
    FACILITY,
    NOW,
    OPERATION,
    OTHER_RECEIPT,
    POLICY,
    PROVIDER_A,
    PROVIDER_B,
    RECEIPT,
    RESEARCHBOT,
    RULES,
    SIGNER_CONFIG,
    UNREGISTERED_AUTHORITY,
    FakeSettlementSource,
    account_snapshot,
    authority_keys,
    authorization,
    expected_payment,
    ledger_context,
    policy_signer,
    researchbot_signature,
    settlement_fact,
    signed_authorization,
    signer_list,
    signer_snapshot,
    unsigned_payment,
)

from credence.core.primitives import Ed25519Signature, Timestamp
from credence.core.signatures import key_id_for
from credence.signers.payment_authorization import (
    AUTHORIZATION_DOMAIN,
    PaymentAuthorizationRefused,
    SignedPaymentAuthorization,
    sign_payment_authorization,
)
from credence.signers.policy_signer import PolicySigner, PolicySignerRefused, PolicySigningLog
from credence.xrpl_adapter.multisign import (
    SignerEntry,
    assemble_multisigned,
    unsigned_transaction_digest,
    verify_multisigned_payment,
)
from credence.xrpl_adapter.types import Asset


def refused(code: str, signer: PolicySigner, signed: Any, transaction: Any, **kwargs: Any) -> None:
    with pytest.raises(PolicySignerRefused) as raised:
        signer.sign_payment(
            signed,
            transaction,
            kwargs.pop("ledger", ledger_context()),
            now=kwargs.pop("now", NOW),
        )
    assert raised.value.code == code


# --- the statement ---------------------------------------------------------------------------


def test_statement_is_domain_separated_and_binds_every_field() -> None:
    transaction = unsigned_payment()
    statement = authorization(transaction)
    data = statement.statement_bytes()
    assert data.startswith(len(AUTHORIZATION_DOMAIN).to_bytes(4, "big") + b"credence/xrpl/")
    body = statement.statement()
    assert body["protocol"] == "credence.xrpl.payment_authorization"
    assert body["unsigned_transaction_digest"] == unsigned_transaction_digest(transaction)
    changed = {
        "operation_id": "operation-x1a-2",
        "receipt_digest": OTHER_RECEIPT,
        "receipt_chain_head": OTHER_RECEIPT,
        "verifier_decision_digest": OTHER_RECEIPT,
        "amount_micro_trlusd": AMOUNT + 1,
        "asset_issuer": ATTACKER.account,
        "destination": PROVIDER_B.account,
        "network_id": 1,
        "expires_at": Timestamp("2026-10-24T18:03:00Z"),
    }
    identities = {
        authorization(transaction, **{k: v}).authorization_id() for k, v in changed.items()
    }
    assert statement.authorization_id() not in identities
    assert len(identities) == len(changed)


def test_statement_refuses_malformed_members() -> None:
    transaction = unsigned_payment()
    for changes in (
        {"receipt_digest": "sha256:XYZ"},
        {"unsigned_transaction_digest": "ab" * 32},
        {"amount_micro_trlusd": 0},
        {"amount_micro_trlusd": True},
        {"operation_id": " "},
    ):
        with pytest.raises(PaymentAuthorizationRefused) as raised:
            authorization(transaction, **changes)
        assert raised.value.code == "MALFORMED_AUTHORIZATION"


def test_authority_cannot_sign_a_statement_naming_another_key() -> None:
    transaction = unsigned_payment()
    with pytest.raises(PaymentAuthorizationRefused) as raised:
        sign_payment_authorization(authorization(transaction), UNREGISTERED_AUTHORITY)
    assert raised.value.code == "WRONG_AUTHORITY_KEY"


# --- positive path ---------------------------------------------------------------------------


def test_policy_signer_signs_and_the_assembled_payment_verifies(tmp_path: Path) -> None:
    transaction = unsigned_payment()
    signer = policy_signer(tmp_path)
    policy_part = signer.sign_payment(
        signed_authorization(transaction), transaction, ledger_context(), now=NOW
    )
    assert policy_part.account == POLICY.account
    signed = assemble_multisigned(transaction, [researchbot_signature(transaction), policy_part])
    verified = verify_multisigned_payment(
        signed, policy=RULES, expected=expected_payment(transaction), ledger=ledger_context()
    )
    assert set(verified.signer_accounts) == {RESEARCHBOT.account, POLICY.account}
    assert "private" not in repr(signer).lower() and POLICY.private_key not in repr(signer)
    assert os.stat(tmp_path / "policy-signer.sqlite3").st_mode & 0o777 == 0o600


def test_resigning_the_identical_transaction_is_idempotent(tmp_path: Path) -> None:
    transaction = unsigned_payment()
    statement = signed_authorization(transaction)
    first = policy_signer(tmp_path).sign_payment(statement, transaction, ledger_context(), now=NOW)
    again = policy_signer(tmp_path).sign_payment(statement, transaction, ledger_context(), now=NOW)
    assert first == again


# --- the authorization statement -------------------------------------------------------------


def test_forged_statement_signature_is_refused(tmp_path: Path) -> None:
    transaction = unsigned_payment()
    good = signed_authorization(transaction)
    forged = SignedPaymentAuthorization(good.authorization, Ed25519Signature("00" * 64))
    refused("BAD_AUTHORIZATION_SIGNATURE", policy_signer(tmp_path), forged, transaction)


def test_statement_altered_after_signing_is_refused(tmp_path: Path) -> None:
    transaction = unsigned_payment()
    good = signed_authorization(transaction)
    altered = SignedPaymentAuthorization(
        replace(good.authorization, destination=PROVIDER_B.account), good.signature
    )
    refused("BAD_AUTHORIZATION_SIGNATURE", policy_signer(tmp_path), altered, transaction)


def test_unknown_and_revoked_authority_keys_are_refused(tmp_path: Path) -> None:
    transaction = unsigned_payment()
    refused(
        "UNKNOWN_AUTHORITY_KEY",
        policy_signer(tmp_path),
        signed_authorization(transaction, key=UNREGISTERED_AUTHORITY),
        transaction,
    )
    refused(
        "AUTHORITY_KEY_REVOKED",
        policy_signer(tmp_path),
        signed_authorization(transaction, key=AUTHORITY_RETIRED),
        transaction,
    )
    keys = authority_keys()
    keys.revoke(key_id_for(AUTHORITY.verify_key))
    refused(
        "AUTHORITY_KEY_REVOKED",
        policy_signer(tmp_path, keys=keys),
        signed_authorization(transaction),
        transaction,
    )


def test_expired_and_overlong_statements_are_refused(tmp_path: Path) -> None:
    transaction = unsigned_payment()
    signer = policy_signer(tmp_path)
    refused(
        "AUTHORIZATION_EXPIRED", signer, signed_authorization(transaction), transaction, now=EXPIRES
    )
    overlong = signed_authorization(transaction, expires_at=Timestamp("2026-10-24T18:05:01Z"))
    refused("AUTHORIZATION_TOO_LONG", signer, overlong, transaction)


def test_statement_for_another_network_is_refused(tmp_path: Path) -> None:
    transaction = unsigned_payment()
    refused(
        "NETWORK_MISMATCH",
        policy_signer(tmp_path),
        signed_authorization(transaction, network_id=1),
        transaction,
    )


# --- independent Core binding ----------------------------------------------------------------


def test_payment_without_core_settlement_is_refused(tmp_path: Path) -> None:
    transaction = unsigned_payment()
    refused(
        "SETTLEMENT_NOT_FOUND",
        policy_signer(tmp_path, source=FakeSettlementSource(facts=())),
        signed_authorization(transaction),
        transaction,
    )


def test_settlement_in_another_state_is_refused(tmp_path: Path) -> None:
    transaction = unsigned_payment()
    source = FakeSettlementSource(facts=(settlement_fact(state="xrpl_settled"),))
    refused(
        "SETTLEMENT_NOT_ACCOUNTING_SETTLED",
        policy_signer(tmp_path, source=source),
        signed_authorization(transaction),
        transaction,
    )


@pytest.mark.parametrize(
    ("statement_changes", "detail"),
    [
        ({"facility_id": "facility-other"}, "facility_id"),
        ({"reservation_id": "reservation-other"}, "reservation_id"),
        ({"provider_id": "provider-b", "destination": PROVIDER_B.account}, "provider_id"),
        ({"verifier_decision_digest": OTHER_RECEIPT}, "verifier_decision_digest"),
        ({"accounting_settled_at": Timestamp("2026-10-24T17:59:31Z")}, "settled_at"),
    ],
)
def test_statement_contradicting_core_is_refused(
    tmp_path: Path, statement_changes: dict[str, Any], detail: str
) -> None:
    transaction = unsigned_payment(
        destination=statement_changes.get("destination", PROVIDER_A.account)
    )
    with pytest.raises(PolicySignerRefused) as raised:
        policy_signer(tmp_path).sign_payment(
            signed_authorization(transaction, **statement_changes),
            transaction,
            ledger_context(),
            now=NOW,
        )
    assert (raised.value.code, raised.value.detail) == ("SETTLEMENT_MISMATCH", detail)


def test_amount_above_core_settlement_is_refused(tmp_path: Path) -> None:
    transaction = unsigned_payment(amount_micro_units=AMOUNT + 1)
    with pytest.raises(PolicySignerRefused) as raised:
        policy_signer(tmp_path).sign_payment(
            signed_authorization(transaction, amount_micro_trlusd=AMOUNT + 1),
            transaction,
            ledger_context(),
            now=NOW,
        )
    assert (raised.value.code, raised.value.detail) == ("SETTLEMENT_MISMATCH", "amount")


def test_moved_chain_head_is_refused(tmp_path: Path) -> None:
    transaction = unsigned_payment()
    source = FakeSettlementSource(heads={FACILITY: OTHER_RECEIPT})
    refused(
        "CHAIN_HEAD_MISMATCH",
        policy_signer(tmp_path, source=source),
        signed_authorization(transaction),
        transaction,
    )


# --- parties ---------------------------------------------------------------------------------


def test_destination_must_be_the_providers_payout_account(tmp_path: Path) -> None:
    transaction = unsigned_payment(destination=ATTACKER.account)
    refused(
        "DESTINATION_NOT_PROVIDER_PAYOUT",
        policy_signer(tmp_path),
        signed_authorization(transaction, destination=ATTACKER.account),
        transaction,
    )


def test_wrong_issuer_or_source_account_is_refused(tmp_path: Path) -> None:
    other_asset = Asset("tRLUSD", SIGNER_CONFIG.asset.currency_code, ATTACKER.account)
    transaction = unsigned_payment(asset=other_asset)
    refused(
        "ISSUER_MISMATCH",
        policy_signer(tmp_path),
        signed_authorization(transaction, asset_issuer=ATTACKER.account),
        transaction,
    )
    from_attacker = unsigned_payment(account=ATTACKER.account)
    refused(
        "SOURCE_ACCOUNT_MISMATCH",
        policy_signer(tmp_path),
        signed_authorization(from_attacker, source_account=ATTACKER.account),
        from_attacker,
    )


# --- the transaction -------------------------------------------------------------------------


def test_transaction_other_than_the_authorized_digest_is_refused(tmp_path: Path) -> None:
    authorized = unsigned_payment()
    swapped = unsigned_payment(fee_drops=31)
    refused(
        "TRANSACTION_DIGEST_MISMATCH",
        policy_signer(tmp_path),
        signed_authorization(authorized),
        swapped,
    )


def test_extra_fields_cannot_ride_along_even_if_authorized(tmp_path: Path) -> None:
    padded = dict(unsigned_payment(), Memos=[{"Memo": {"MemoData": "DEADBEEF"}}])
    refused(
        "TRANSACTION_FIELDS_MISMATCH",
        policy_signer(tmp_path),
        signed_authorization(padded),
        padded,
    )


def test_already_signed_transaction_is_refused(tmp_path: Path) -> None:
    transaction = unsigned_payment()
    statement = signed_authorization(transaction)
    signed_form = dict(transaction, Signers=[])
    refused("NOT_UNSIGNED", policy_signer(tmp_path), statement, signed_form)


# --- ledger state and signer authority -------------------------------------------------------


def test_revoked_policy_signer_does_not_sign(tmp_path: Path) -> None:
    transaction = unsigned_payment()
    revoked = account_snapshot(
        signer_list=signer_list(
            entries=(SignerEntry(RESEARCHBOT.account, 1), SignerEntry(ATTACKER.account, 1))
        )
    )
    refused(
        "POLICY_SIGNER_NOT_AUTHORIZED",
        policy_signer(tmp_path),
        signed_authorization(transaction),
        transaction,
        ledger=ledger_context(account=revoked),
    )
    no_list = account_snapshot(signer_list=None)
    refused(
        "POLICY_SIGNER_NOT_AUTHORIZED",
        policy_signer(tmp_path),
        signed_authorization(transaction),
        transaction,
        ledger=ledger_context(account=no_list),
    )


def test_policy_signer_refuses_a_key_that_is_not_its_account(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        PolicySigner(
            config=SIGNER_CONFIG,
            authority_keys=authority_keys(),
            settlement_source=FakeSettlementSource(),
            signing_log=PolicySigningLog(tmp_path / "log.sqlite3"),
            public_key=ATTACKER.public_key,
            private_key=ATTACKER.private_key,
        )


def ledger_at(index: int) -> Any:
    """A consistent validated ledger at another index (the payment's LastLedgerSequence is 110)."""
    return ledger_context(
        validated_ledger_index=index,
        account=account_snapshot(ledger_index=index),
        signers={
            RESEARCHBOT.account: signer_snapshot(RESEARCHBOT, ledger_index=index),
            POLICY.account: signer_snapshot(POLICY, ledger_index=index),
        },
    )


@pytest.mark.parametrize(
    ("ledger", "code"),
    [
        (
            ledger_context(account=account_snapshot(regular_key=ATTACKER.account)),
            "REGULAR_KEY_PRESENT",
        ),
        (
            ledger_context(account=account_snapshot(signer_list=signer_list(quorum=1))),
            "QUORUM_MISMATCH",
        ),
        (ledger_context(account=account_snapshot(sequence=8)), "SEQUENCE_MISMATCH"),
        (ledger_at(110), "LAST_LEDGER_SEQUENCE_EXPIRED"),
        (ledger_context(base_fee_drops=11), "FEE_TOO_LOW"),
        (ledger_context(network_id=1), "NETWORK_MISMATCH"),
    ],
)
def test_ledger_state_is_checked_before_signing(tmp_path: Path, ledger: Any, code: str) -> None:
    transaction = unsigned_payment()
    refused(
        code, policy_signer(tmp_path), signed_authorization(transaction), transaction, ledger=ledger
    )


# --- one transaction per receipt -------------------------------------------------------------


def test_second_transaction_for_the_same_receipt_is_refused_across_restart(tmp_path: Path) -> None:
    first = unsigned_payment()
    policy_signer(tmp_path).sign_payment(
        signed_authorization(first), first, ledger_context(), now=NOW
    )
    second = unsigned_payment(last_ledger_sequence=111)
    restarted = policy_signer(tmp_path)
    refused("ALREADY_AUTHORIZED", restarted, signed_authorization(second), second)
    # A new operation ID does not let the same receipt be paid twice.
    refused(
        "ALREADY_AUTHORIZED",
        restarted,
        signed_authorization(second, operation_id="operation-x1a-retry"),
        second,
    )


def test_concurrent_signers_authorize_only_one_transaction(tmp_path: Path) -> None:
    candidates = [unsigned_payment(last_ledger_sequence=101 + index) for index in range(8)]
    results: list[str] = []
    barrier = threading.Barrier(len(candidates))

    def attempt(transaction: dict[str, Any]) -> None:
        signer = policy_signer(tmp_path)
        statement = signed_authorization(transaction)
        barrier.wait()
        try:
            signer.sign_payment(statement, transaction, ledger_context(), now=NOW)
            results.append("signed")
        except PolicySignerRefused as exc:
            results.append(exc.code)

    threads = [threading.Thread(target=attempt, args=(item,)) for item in candidates]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count("signed") == 1
    assert results.count("ALREADY_AUTHORIZED") == len(candidates) - 1


def test_signing_log_holds_no_key_material(tmp_path: Path) -> None:
    transaction = unsigned_payment()
    policy_signer(tmp_path).sign_payment(
        signed_authorization(transaction), transaction, ledger_context(), now=NOW
    )
    content = (tmp_path / "policy-signer.sqlite3").read_bytes()
    assert POLICY.private_key.encode() not in content
    assert POLICY.private_key[2:].lower().encode() not in content
    assert RECEIPT.encode() in content and OPERATION.encode() in content
