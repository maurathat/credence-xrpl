"""Offline tests for the isolated T1 public XRPL Testnet proof."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import t1_public_testnet.workflow as workflow  # noqa: E402
from t1_public_testnet.workflow import (  # noqa: E402
    AMOUNT_DROPS,
    MEMO_TYPE,
    REQUIRED_LABEL,
    AuthorizationError,
    CredentialError,
    ExecutionAuthorization,
    NetworkRefused,
    OperationJournal,
    ReplayRefused,
    ValidationError,
    assert_expected_network_id,
    construct_unsigned,
    decode_memo,
    load_authorization,
    memo_object,
    require_explicit_execute,
    sign_unsigned,
    validate_rpc_endpoint,
    validate_transaction_structure,
)

SOURCE = "r9cZA1mLK5R5Am25ArfXFmqgNwjZgnfk59"
DESTINATION = "rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh"


def authorization() -> ExecutionAuthorization:
    return ExecutionAuthorization(
        authorization_id="offline-test-authorization",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        operations=frozenset({"construct", "sign"}),
        source=SOURCE,
        destination=DESTINATION,
    )


def transaction() -> dict[str, object]:
    return {
        "Account": SOURCE,
        "Amount": AMOUNT_DROPS,
        "Destination": DESTINATION,
        "Fee": "10",
        "Flags": 0x80000000,
        "LastLedgerSequence": 1004,
        "Memos": [memo_object()],
        "Sequence": 1,
        "SigningPubKey": "",
        "TransactionType": "Payment",
    }


@pytest.mark.parametrize(
    ("network_id", "message"),
    [
        (0, "Mainnet"),
        (2, "Devnet"),
        (21_337, "21337"),
        (1_440_000, "EVM"),
        (1_449_000, "EVM"),
        (1_449_900, "EVM"),
        (999, "not public native XRPL Testnet"),
    ],
)
def test_network_ids_are_explicitly_refused(network_id: int, message: str) -> None:
    with pytest.raises(NetworkRefused, match=message):
        assert_expected_network_id(network_id)


def test_network_id_one_is_required_as_plain_integer() -> None:
    assert assert_expected_network_id(1) == 1
    with pytest.raises(NetworkRefused):
        assert_expected_network_id(True)
    with pytest.raises(NetworkRefused):
        assert_expected_network_id("1")


@pytest.mark.parametrize(
    ("endpoint", "message"),
    [
        ("https://s1.ripple.com:51234/", "Mainnet"),
        ("https://s.devnet.rippletest.net:51234/", "Devnet"),
        ("https://rpc.testnet.xrplevm.org/", "EVM"),
        ("http://127.0.0.1:5005/", "local"),
        ("https://unapproved.example/", "allowlisted"),
    ],
)
def test_endpoint_classes_are_explicitly_refused(endpoint: str, message: str) -> None:
    with pytest.raises(NetworkRefused, match=message):
        validate_rpc_endpoint(endpoint)


def test_unsigned_transaction_has_exact_minimal_structure() -> None:
    value = transaction()
    validate_transaction_structure(value, source=SOURCE, destination=DESTINATION, signed=False)
    assert value["Amount"] == "1"
    assert value["Flags"] == 0x80000000
    assert "NetworkID" not in value
    assert "DestinationTag" not in value
    assert "Paths" not in value
    assert "SendMax" not in value


def test_construction_freezes_fee_sequence_and_plus_four_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        workflow,
        "verify_network",
        lambda _endpoint: {
            "endpoint": "test",
            "network_id": 1,
            "validated_ledger_index": 1000,
            "validated_ledger_hash": "A" * 64,
            "server_state": "full",
            "reserve_base_xrp": "1",
        },
    )

    def account(_endpoint: str, address: str, ledger_index: int) -> dict[str, object]:
        assert ledger_index == 1000
        return {
            "Account": address,
            "Balance": "100000000",
            "Flags": 0,
            "OwnerCount": 0,
            "Sequence": 7,
        }

    monkeypatch.setattr(workflow, "_validated_account", account)
    monkeypatch.setattr(workflow, "_fee_drops", lambda _endpoint: 12)
    artifact = construct_unsigned(authorization())
    value = artifact["transaction"]
    assert value["Fee"] == "12"
    assert value["Sequence"] == 7
    assert value["LastLedgerSequence"] == 1004
    assert value["Amount"] == "1"
    assert "NetworkID" not in value


def test_construction_refuses_non_dedicated_accounts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        workflow,
        "verify_network",
        lambda _endpoint: {
            "endpoint": "test",
            "network_id": 1,
            "validated_ledger_index": 1000,
            "validated_ledger_hash": "A" * 64,
            "server_state": "full",
            "reserve_base_xrp": "1",
        },
    )
    monkeypatch.setattr(
        workflow,
        "_validated_account",
        lambda _endpoint, address, _ledger: {
            "Account": address,
            "Balance": "100000000",
            "Flags": 0,
            "OwnerCount": 1,
            "Sequence": 7,
        },
    )
    with pytest.raises(ValidationError, match="OwnerCount 0"):
        construct_unsigned(authorization())


def test_construction_refuses_destination_deposit_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        workflow,
        "verify_network",
        lambda _endpoint: {
            "endpoint": "test",
            "network_id": 1,
            "validated_ledger_index": 1000,
            "validated_ledger_hash": "A" * 64,
            "server_state": "full",
            "reserve_base_xrp": "1",
        },
    )

    def account(_endpoint: str, address: str, _ledger: int) -> dict[str, object]:
        return {
            "Account": address,
            "Balance": "100000000",
            "Flags": 0 if address == SOURCE else 0x01000000,
            "OwnerCount": 0,
            "Sequence": 7,
        }

    monkeypatch.setattr(workflow, "_validated_account", account)
    with pytest.raises(ValidationError, match="deposit authorization"):
        construct_unsigned(authorization())


def test_construction_refuses_fee_above_frozen_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        workflow,
        "verify_network",
        lambda _endpoint: {
            "endpoint": "test",
            "network_id": 1,
            "validated_ledger_index": 1000,
            "validated_ledger_hash": "A" * 64,
            "server_state": "full",
            "reserve_base_xrp": "1",
        },
    )
    monkeypatch.setattr(
        workflow,
        "_validated_account",
        lambda _endpoint, address, _ledger: {
            "Account": address,
            "Balance": "100000000",
            "Flags": 0,
            "OwnerCount": 0,
            "Sequence": 7,
        },
    )
    monkeypatch.setattr(workflow, "_fee_drops", lambda _endpoint: 1001)
    with pytest.raises(ValidationError, match="Fee"):
        construct_unsigned(authorization())


def test_network_id_field_is_refused_even_when_one() -> None:
    value = transaction()
    value["NetworkID"] = 1
    with pytest.raises(NetworkRefused, match="must be omitted"):
        validate_transaction_structure(value, source=SOURCE, destination=DESTINATION, signed=False)


def test_memo_encoding_round_trips_exact_label_and_reference() -> None:
    value = transaction()
    memo_data, memo_type = decode_memo(value)
    assert memo_data == REQUIRED_LABEL
    assert memo_type == MEMO_TYPE
    encoded = value["Memos"][0]["Memo"]  # type: ignore[index]
    assert encoded["MemoData"] == REQUIRED_LABEL.encode().hex().upper()
    assert encoded["MemoType"] == MEMO_TYPE.encode().hex().upper()


def test_altered_memo_is_refused() -> None:
    value = transaction()
    value["Memos"] = [{"Memo": {"MemoData": "00", "MemoType": MEMO_TYPE.encode().hex().upper()}}]
    with pytest.raises(ValidationError, match="Memo"):
        validate_transaction_structure(value, source=SOURCE, destination=DESTINATION, signed=False)


def test_replay_claim_is_durable_and_refused(tmp_path: Path) -> None:
    path = tmp_path / "operations.sqlite3"
    first = OperationJournal(path)
    first.claim("auth-1", "submit", "A" * 64)
    first.close()
    second = OperationJournal(path)
    try:
        with pytest.raises(ReplayRefused, match="already claimed"):
            second.claim("auth-1", "submit", "A" * 64)
        with pytest.raises(ReplayRefused, match="already claimed"):
            second.claim("auth-1", "submit", "B" * 64)
    finally:
        second.close()


def test_missing_signing_credential_is_refused_before_signing() -> None:
    unsigned = {
        "schema": "credence.t1.unsigned-payment.v1",
        "authorization_id": authorization().authorization_id,
        "transaction": transaction(),
    }
    with pytest.raises(CredentialError, match="required"):
        sign_unsigned(authorization(), unsigned, credential=None)


def test_sensitive_operation_requires_explicit_execute_switch() -> None:
    with pytest.raises(AuthorizationError, match="--execute"):
        require_explicit_execute(False)
    require_explicit_execute(True)


def test_missing_execution_authorization_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(AuthorizationError, match="regular non-symlink file"):
        load_authorization(tmp_path / "not-created.json", operation="sign")
