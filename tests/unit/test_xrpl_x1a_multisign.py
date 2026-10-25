"""X1a offline 2-of-2 multisignature assembly and verification."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from x1a_world import (
    AMOUNT,
    ATTACKER,
    BASE_FEE,
    FEE,
    LAST_LEDGER,
    LEDGER_HASH,
    NETWORK_ID,
    OTHER_ISSUER,
    OWNER,
    POLICY,
    PROVIDER_B,
    RESEARCHBOT,
    RULES,
    SEQUENCE,
    TICKET,
    VALIDATED_LEDGER,
    account_snapshot,
    direct_policy_signature,
    expected_payment,
    ledger_context,
    researchbot_signature,
    signer_list,
    signer_snapshot,
    unsigned_payment,
)
from xrpl.core.addresscodec import decode_classic_address
from xrpl.core.binarycodec import decode, encode

from credence.xrpl_adapter.multisign import (
    LSF_DISABLE_MASTER,
    MasterKeyPolicy,
    MultisignRejected,
    MultisignSignature,
    SignerEntry,
    assemble_multisigned,
    multisign_fee_drops,
    sign_as_signer,
    trlusd_value,
    unsigned_transaction_digest,
    verify_multisigned_payment,
)
from credence.xrpl_adapter.preparation import TRLUSD_WIRE_CODE, transaction_hash
from credence.xrpl_adapter.types import Asset


def both_signed(transaction: dict[str, Any]) -> bytes:
    return assemble_multisigned(
        transaction, [researchbot_signature(transaction), direct_policy_signature(transaction)]
    )


def verify(signed: bytes, transaction: dict[str, Any], **kwargs: Any) -> Any:
    return verify_multisigned_payment(
        signed,
        policy=kwargs.pop("policy", RULES),
        expected=kwargs.pop("expected", expected_payment(transaction)),
        ledger=kwargs.pop("ledger", ledger_context()),
    )


def rejected(code: str, signed: bytes, transaction: dict[str, Any], **kwargs: Any) -> None:
    with pytest.raises(MultisignRejected) as raised:
        verify(signed, transaction, **kwargs)
    assert raised.value.code == code


def encode_with_signers(transaction: dict[str, Any], signatures: list[MultisignSignature]) -> bytes:
    """Encode Signers in exactly the given order (bypassing canonical assembly)."""
    signed = dict(transaction)
    signed["Signers"] = [
        {
            "Signer": {
                "Account": item.account,
                "SigningPubKey": item.signing_public_key,
                "TxnSignature": item.signature,
            }
        }
        for item in signatures
    ]
    return bytes.fromhex(encode(signed))


# --- positive path ---------------------------------------------------------------------------


def test_two_signatures_assemble_and_verify() -> None:
    transaction = unsigned_payment()
    signed = both_signed(transaction)
    verified = verify(signed, transaction)
    assert verified.transaction_hash == transaction_hash(signed)
    assert set(verified.signer_accounts) == {RESEARCHBOT.account, POLICY.account}
    assert verified.payment.amount_micro_units == AMOUNT
    decoded = decode(signed.hex().upper())
    assert decoded["SigningPubKey"] == ""
    assert "TxnSignature" not in decoded
    assert len(decoded["Signers"]) == 2


def test_signer_order_is_canonical_regardless_of_input_order() -> None:
    transaction = unsigned_payment()
    forward = both_signed(transaction)
    backward = assemble_multisigned(
        transaction, [direct_policy_signature(transaction), researchbot_signature(transaction)]
    )
    assert forward == backward


def test_ticketed_payment_verifies() -> None:
    transaction = unsigned_payment(sequence=None, ticket_sequence=TICKET)
    assert transaction["Sequence"] == 0
    verify(both_signed(transaction), transaction)


def test_master_key_disabled_policy_accepts_disabled_account() -> None:
    transaction = unsigned_payment()
    verify(
        both_signed(transaction),
        transaction,
        policy=replace(RULES, master_key=MasterKeyPolicy.DISABLED),
        ledger=ledger_context(account=account_snapshot(flags=LSF_DISABLE_MASTER)),
    )


def test_multisign_fee_is_base_fee_times_one_plus_signers() -> None:
    assert multisign_fee_drops(BASE_FEE, 2) == 30
    assert multisign_fee_drops(12, 2) == 36
    with pytest.raises(ValueError):
        multisign_fee_drops(0, 2)


def test_trlusd_values_are_exact() -> None:
    assert trlusd_value(1) == "0.000001"
    assert trlusd_value(1_500_000) == "1.5"
    assert trlusd_value(2_000_000) == "2"
    with pytest.raises(ValueError):
        trlusd_value(0)


# --- missing and invalid signatures ----------------------------------------------------------


def test_one_missing_signature_is_refused() -> None:
    transaction = unsigned_payment()
    only_agent = assemble_multisigned(transaction, [researchbot_signature(transaction)])
    rejected("SIGNER_MISSING", only_agent, transaction)
    only_policy = assemble_multisigned(transaction, [direct_policy_signature(transaction)])
    rejected("SIGNER_MISSING", only_policy, transaction)


def test_invalid_signature_is_refused() -> None:
    transaction = unsigned_payment()
    good = researchbot_signature(transaction)
    flipped = good.signature[:-2] + ("00" if good.signature[-2:] != "00" else "01")
    forged = replace(good, signature=flipped)
    signed = encode_with_signers(
        transaction,
        sorted(
            [forged, direct_policy_signature(transaction)],
            key=lambda item: decode_classic_address(item.account),
        ),
    )
    rejected("SIGNATURE_INVALID", signed, transaction)


def test_signature_over_another_transaction_is_refused() -> None:
    transaction = unsigned_payment()
    other = unsigned_payment(amount_micro_units=AMOUNT + 1)
    stolen = researchbot_signature(other)
    signed = encode_with_signers(
        transaction,
        sorted(
            [stolen, direct_policy_signature(transaction)],
            key=lambda item: decode_classic_address(item.account),
        ),
    )
    rejected("SIGNATURE_INVALID", signed, transaction)


def test_assembly_refuses_an_invalid_signature() -> None:
    transaction = unsigned_payment()
    stolen = researchbot_signature(unsigned_payment(amount_micro_units=AMOUNT + 1))
    with pytest.raises(MultisignRejected) as raised:
        assemble_multisigned(transaction, [stolen, direct_policy_signature(transaction)])
    assert raised.value.code == "SIGNATURE_INVALID"


def test_signer_key_must_be_the_signer_accounts_master_key() -> None:
    transaction = unsigned_payment()
    with pytest.raises(MultisignRejected) as raised:
        sign_as_signer(
            transaction,
            account=RESEARCHBOT.account,
            public_key=ATTACKER.public_key,
            private_key=ATTACKER.private_key,
        )
    assert raised.value.code == "SIGNER_KEY_NOT_MASTER"
    # An attacker key claiming to be ResearchBot inside the Signers array.
    impostor = MultisignSignature(
        account=RESEARCHBOT.account,
        signing_public_key=ATTACKER.public_key,
        signature=sign_as_signer(
            transaction,
            account=ATTACKER.account,
            public_key=ATTACKER.public_key,
            private_key=ATTACKER.private_key,
        ).signature,
    )
    signed = encode_with_signers(
        transaction,
        sorted(
            [impostor, direct_policy_signature(transaction)],
            key=lambda item: decode_classic_address(item.account),
        ),
    )
    rejected("SIGNER_KEY_NOT_MASTER", signed, transaction)


def test_unauthorized_signer_is_refused() -> None:
    transaction = unsigned_payment()
    attacker = sign_as_signer(
        transaction,
        account=ATTACKER.account,
        public_key=ATTACKER.public_key,
        private_key=ATTACKER.private_key,
    )
    signed = assemble_multisigned(transaction, [researchbot_signature(transaction), attacker])
    rejected("UNEXPECTED_SIGNER", signed, transaction)
    three = assemble_multisigned(
        transaction,
        [researchbot_signature(transaction), direct_policy_signature(transaction), attacker],
    )
    rejected("UNEXPECTED_SIGNER", three, transaction)


def test_duplicate_signer_is_refused() -> None:
    transaction = unsigned_payment()
    agent = researchbot_signature(transaction)
    with pytest.raises(MultisignRejected) as raised:
        assemble_multisigned(transaction, [agent, agent])
    assert raised.value.code == "DUPLICATE_SIGNER"
    rejected("DUPLICATE_SIGNER", encode_with_signers(transaction, [agent, agent]), transaction)


def test_non_canonical_signer_order_is_refused() -> None:
    transaction = unsigned_payment()
    ordered = sorted(
        [researchbot_signature(transaction), direct_policy_signature(transaction)],
        key=lambda item: decode_classic_address(item.account),
    )
    signed = encode_with_signers(transaction, list(reversed(ordered)))
    rejected("SIGNER_ORDER", signed, transaction)


def test_single_signed_payment_is_refused() -> None:
    transaction = unsigned_payment()
    single = dict(transaction, SigningPubKey=OWNER.public_key)
    single["TxnSignature"] = "AA"
    rejected("NOT_MULTISIGNED", bytes.fromhex(encode(single)), transaction)


# --- altered transaction ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("Destination", PROVIDER_B.account),
        ("Fee", "31"),
        ("Sequence", 8),
        ("LastLedgerSequence", LAST_LEDGER + 1),
        ("NetworkID", NETWORK_ID + 1),
        ("Amount", {"value": "1.500001"}),
    ],
)
def test_transaction_altered_after_signing_is_refused(field: str, value: object) -> None:
    transaction = unsigned_payment()
    signed = decode(both_signed(transaction).hex().upper())
    if isinstance(value, dict):
        signed[field] = {**signed[field], **value}
    else:
        signed[field] = value
    rejected("TRANSACTION_DIGEST_MISMATCH", bytes.fromhex(encode(signed)), transaction)


def test_altered_transaction_with_matching_digest_still_fails_signatures() -> None:
    transaction = unsigned_payment()
    altered = unsigned_payment(amount_micro_units=AMOUNT * 10)
    signatures = sorted(
        [researchbot_signature(transaction), direct_policy_signature(transaction)],
        key=lambda item: decode_classic_address(item.account),
    )
    signed = encode_with_signers(altered, signatures)
    # Even when the verifier is told to expect the altered digest, the signatures do not cover it.
    rejected(
        "SIGNATURE_INVALID",
        signed,
        altered,
        expected=expected_payment(altered, amount_micro_units=AMOUNT * 10),
    )


# --- wrong economic fields -------------------------------------------------------------------


def test_wrong_destination_amount_issuer_are_refused() -> None:
    # Each transaction is internally consistent and correctly signed, but is not the payment the
    # expectation (PROVIDER_A, AMOUNT, ISSUER) describes.
    wrong_destination = unsigned_payment(destination=PROVIDER_B.account)
    rejected("DESTINATION_MISMATCH", both_signed(wrong_destination), wrong_destination)
    wrong_amount = unsigned_payment(amount_micro_units=AMOUNT + 1)
    rejected("AMOUNT_MISMATCH", both_signed(wrong_amount), wrong_amount)
    wrong_issuer = unsigned_payment(asset=Asset("tRLUSD", TRLUSD_WIRE_CODE, OTHER_ISSUER.account))
    rejected("ISSUER_MISMATCH", both_signed(wrong_issuer), wrong_issuer)


def test_network_mismatch_is_refused() -> None:
    transaction = unsigned_payment()
    rejected(
        "NETWORK_MISMATCH",
        both_signed(transaction),
        transaction,
        ledger=ledger_context(network_id=1),
    )


# --- signer list and quorum ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("snapshot", "code"),
    [
        (account_snapshot(signer_list=None), "SIGNER_LIST_MISSING"),
        (account_snapshot(signer_list=signer_list(quorum=1)), "QUORUM_MISMATCH"),
        (account_snapshot(signer_list=signer_list(quorum=3)), "QUORUM_MISMATCH"),
        (
            account_snapshot(
                signer_list=signer_list(
                    entries=(SignerEntry(RESEARCHBOT.account, 2), SignerEntry(POLICY.account, 1))
                )
            ),
            "SIGNER_WEIGHT_MISMATCH",
        ),
        (
            account_snapshot(
                signer_list=signer_list(
                    entries=(
                        SignerEntry(RESEARCHBOT.account, 1),
                        SignerEntry(POLICY.account, 1),
                        SignerEntry(ATTACKER.account, 1),
                    )
                )
            ),
            "SIGNER_LIST_MISMATCH",
        ),
        (
            account_snapshot(
                signer_list=signer_list(entries=(SignerEntry(RESEARCHBOT.account, 1),))
            ),
            "SIGNER_LIST_MISMATCH",
        ),
        (
            account_snapshot(signer_list=signer_list(account=ATTACKER.account)),
            "SIGNER_LIST_MISMATCH",
        ),
        (account_snapshot(validated=False), "LEDGER_NOT_VALIDATED"),
        (account_snapshot(exists=False), "ACCOUNT_NOT_FOUND"),
        (account_snapshot(regular_key=ATTACKER.account), "REGULAR_KEY_PRESENT"),
        (account_snapshot(flags=LSF_DISABLE_MASTER), "MASTER_KEY_STATE_MISMATCH"),
    ],
)
def test_signer_list_and_account_keys_are_enforced(snapshot: Any, code: str) -> None:
    transaction = unsigned_payment()
    rejected(code, both_signed(transaction), transaction, ledger=ledger_context(account=snapshot))


def test_master_key_policy_disabled_refuses_enabled_master() -> None:
    transaction = unsigned_payment()
    rejected(
        "MASTER_KEY_STATE_MISMATCH",
        both_signed(transaction),
        transaction,
        policy=replace(RULES, master_key=MasterKeyPolicy.DISABLED),
    )


def test_signer_with_disabled_master_key_is_refused() -> None:
    transaction = unsigned_payment()
    signers = {
        RESEARCHBOT.account: signer_snapshot(
            RESEARCHBOT, exists=True, flags=LSF_DISABLE_MASTER, sequence=1
        ),
        POLICY.account: signer_snapshot(POLICY),
    }
    rejected(
        "SIGNER_MASTER_DISABLED",
        both_signed(transaction),
        transaction,
        ledger=ledger_context(signers=signers),
    )


def test_states_from_different_ledgers_are_refused() -> None:
    transaction = unsigned_payment()
    stale = account_snapshot(ledger_index=VALIDATED_LEDGER - 1)
    rejected(
        "LEDGER_STATE_INCONSISTENT",
        both_signed(transaction),
        transaction,
        ledger=ledger_context(account=stale),
    )
    other_hash = account_snapshot(ledger_hash="B2" * 32)
    rejected(
        "LEDGER_STATE_INCONSISTENT",
        both_signed(transaction),
        transaction,
        ledger=ledger_context(account=other_hash),
    )
    assert LEDGER_HASH != "B2" * 32


def test_missing_signer_state_is_refused() -> None:
    transaction = unsigned_payment()
    rejected(
        "SIGNER_STATE_MISSING",
        both_signed(transaction),
        transaction,
        ledger=ledger_context(signers={RESEARCHBOT.account: signer_snapshot(RESEARCHBOT)}),
    )


# --- fee, sequence and expiry ----------------------------------------------------------------


def test_fee_below_multisign_cost_is_refused() -> None:
    transaction = unsigned_payment(fee_drops=FEE - 1)
    rejected("FEE_TOO_LOW", both_signed(transaction), transaction)


def test_fee_above_ceiling_is_refused() -> None:
    transaction = unsigned_payment(fee_drops=101)
    rejected("FEE_ABOVE_CEILING", both_signed(transaction), transaction)


def test_sequence_and_ticket_must_be_usable() -> None:
    stale = unsigned_payment(sequence=6)
    rejected("SEQUENCE_MISMATCH", both_signed(stale), stale)
    missing_ticket = unsigned_payment(sequence=None, ticket_sequence=TICKET + 1)
    rejected("TICKET_UNAVAILABLE", both_signed(missing_ticket), missing_ticket)


def test_last_ledger_sequence_window() -> None:
    expired = unsigned_payment(last_ledger_sequence=VALIDATED_LEDGER)
    rejected("LAST_LEDGER_SEQUENCE_EXPIRED", both_signed(expired), expired)
    too_far = unsigned_payment(last_ledger_sequence=VALIDATED_LEDGER + 21)
    rejected("LAST_LEDGER_SEQUENCE_TOO_FAR", both_signed(too_far), too_far)
    edge = unsigned_payment(last_ledger_sequence=VALIDATED_LEDGER + 20)
    verify(both_signed(edge), edge)


# --- unsigned digest -------------------------------------------------------------------------


def test_unsigned_digest_refuses_signed_forms() -> None:
    transaction = unsigned_payment()
    digest = unsigned_transaction_digest(transaction)
    assert len(digest) == 64 and digest == digest.upper()
    for bad in (
        dict(transaction, SigningPubKey=OWNER.public_key),
        dict(transaction, TxnSignature="AA"),
        dict(transaction, Signers=[]),
    ):
        with pytest.raises(MultisignRejected) as raised:
            unsigned_transaction_digest(bad)
        assert raised.value.code == "NOT_UNSIGNED"


def test_unsigned_digest_changes_with_every_field() -> None:
    base = unsigned_transaction_digest(unsigned_payment())
    variants = [
        unsigned_payment(destination=PROVIDER_B.account),
        unsigned_payment(amount_micro_units=AMOUNT + 1),
        unsigned_payment(fee_drops=FEE + 1),
        unsigned_payment(sequence=SEQUENCE + 1),
        unsigned_payment(last_ledger_sequence=LAST_LEDGER + 1),
        unsigned_payment(network_id=NETWORK_ID + 1),
        dict(unsigned_payment(), Memos=[{"Memo": {"MemoData": "00"}}]),
    ]
    assert len({unsigned_transaction_digest(item) for item in variants} | {base}) == 8
