"""Offline adversarial tests for the isolated T1 public XRPL Testnet proof."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import t1_public_testnet.workflow as workflow  # noqa: E402
from t1_public_testnet.workflow import (  # noqa: E402
    AMOUNT_DROPS,
    AUTH_SCHEMA,
    MEMO_TYPE,
    PRIMARY_RPC_ENDPOINT,
    REQUIRED_LABEL,
    AuthorizationError,
    CredentialError,
    ExecutionAuthorization,
    ExecutionCapability,
    NetworkRefused,
    OperationJournal,
    ReplayRefused,
    RpcError,
    ValidationError,
    assert_expected_network_id,
    construct_unsigned,
    decode_memo,
    history_covers,
    issue_execution_capability,
    load_authorization,
    memo_object,
    parse_complete_ledgers,
    poll_validated,
    require_explicit_execute,
    setup_accounts,
    sign_unsigned,
    submit_once,
    transaction_identity,
    validate_rpc_endpoint,
    validate_transaction_structure,
)

SOURCE = "r9cZA1mLK5R5Am25ArfXFmqgNwjZgnfk59"
DESTINATION = "rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh"
FROZEN_NOW = datetime(2026, 10, 24, 20, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workflow, "_utc_now", lambda: FROZEN_NOW)


def transaction() -> dict[str, Any]:
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


def authorization_payload(
    *,
    authorization_id: str = "offline-test-authorization",
    issued_at: datetime | str | None = None,
    expires_at: datetime | str | None = None,
    operations: tuple[str, ...] = ("construct",),
    network_id: object = 1,
    identity: str | None = None,
) -> dict[str, Any]:
    issued = FROZEN_NOW - timedelta(minutes=1) if issued_at is None else issued_at
    expires = FROZEN_NOW + timedelta(hours=1) if expires_at is None else expires_at

    def encoded(value: datetime | str) -> str:
        return value.isoformat().replace("+00:00", "Z") if isinstance(value, datetime) else value

    return {
        "schema": AUTH_SCHEMA,
        "authorization_id": authorization_id,
        "issued_at": encoded(issued),
        "expires_at": encoded(expires),
        "network_id": network_id,
        "label": REQUIRED_LABEL,
        "amount_drops": AMOUNT_DROPS,
        "source": SOURCE,
        "destination": DESTINATION,
        "allowed_operations": list(operations),
        "transaction_identity": identity,
    }


def write_authorization(
    tmp_path: Path,
    *,
    name: str = "authorization.json",
    operation: str = "construct",
    **overrides: Any,
) -> ExecutionAuthorization:
    path = tmp_path / name
    path.write_text(json.dumps(authorization_payload(**overrides)), encoding="utf-8")
    path.chmod(0o600)
    return load_authorization(path, operation=operation, now=FROZEN_NOW)


def unsigned_artifact(authorization_id: str, value: dict[str, Any] | None = None) -> dict[str, Any]:
    tx = transaction() if value is None else value
    return {
        "schema": "credence.t1.unsigned-payment.v1",
        "authorization_id": authorization_id,
        "network": {
            "endpoint": PRIMARY_RPC_ENDPOINT,
            "network_id": 1,
            "validated_ledger_index": 1000,
            "validated_ledger_hash": "A" * 64,
            "server_state": "full",
            "complete_ledgers": "1-1000",
            "reserve_base_xrp": "1",
        },
        "construction": {
            "validated_ledger_index": 1000,
            "validated_ledger_hash": "A" * 64,
            "fee_rule": "exact",
            "sequence_rule": "exact",
            "last_ledger_rule": "plus 4",
        },
        "transaction_identity": transaction_identity(tx),
        "transaction": tx,
    }


def signed_artifact(authorization_id: str) -> dict[str, Any]:
    artifact = unsigned_artifact(authorization_id)
    tx = dict(artifact["transaction"])
    tx["SigningPubKey"] = "PUBLIC"
    tx["TxnSignature"] = "SIGNATURE"
    return {
        **artifact,
        "schema": "credence.t1.signed-payment.v1",
        "transaction_hash": "F" * 64,
        "transaction_blob": "BLOB",
        "transaction": tx,
    }


def network_observation(index: int, ledger_hash: str, complete: str = "1-2000") -> dict[str, Any]:
    return {
        "endpoint": PRIMARY_RPC_ENDPOINT,
        "network_id": 1,
        "validated_ledger_index": index,
        "validated_ledger_hash": ledger_hash,
        "server_state": "full",
        "complete_ledgers": complete,
        "reserve_base_xrp": "1",
    }


def journal_with_submission(
    tmp_path: Path, authorization_id: str, tx_hash: str
) -> OperationJournal:
    journal = OperationJournal(tmp_path / "operations.sqlite3")
    journal.claim(authorization_id, "submit", tx_hash)
    return journal


def journal_status(path: Path, authorization_id: str) -> str:
    connection = sqlite3.connect(path)
    try:
        row = connection.execute(
            "SELECT status FROM operation_claims WHERE authorization_id = ? AND operation = 'submit'",
            (authorization_id,),
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    return str(row[0])


def journal_claim_count(path: Path) -> int:
    connection = sqlite3.connect(path)
    try:
        row = connection.execute("SELECT COUNT(*) FROM operation_claims").fetchone()
    finally:
        connection.close()
    assert row is not None
    return int(row[0])


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


def install_construction_responses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        workflow,
        "verify_network",
        lambda _endpoint: network_observation(1000, "A" * 64, "1-1000"),
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


def test_b2_construction_freezes_fee_sequence_and_exact_plus_four_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authorization = write_authorization(tmp_path)
    install_construction_responses(monkeypatch)
    artifact = construct_unsigned(authorization)
    value = artifact["transaction"]
    assert value["Fee"] == "12"
    assert value["Sequence"] == 7
    assert value["LastLedgerSequence"] == 1004
    assert artifact["construction"]["validated_ledger_index"] == 1000
    assert artifact["transaction_identity"] == transaction_identity(value)


def test_construction_refuses_non_dedicated_accounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authorization = write_authorization(tmp_path)
    install_construction_responses(monkeypatch)
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
        construct_unsigned(authorization)


def test_construction_refuses_destination_deposit_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authorization = write_authorization(tmp_path)
    install_construction_responses(monkeypatch)

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
        construct_unsigned(authorization)


def test_construction_refuses_fee_above_frozen_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authorization = write_authorization(tmp_path)
    install_construction_responses(monkeypatch)
    monkeypatch.setattr(workflow, "_fee_drops", lambda _endpoint: 1001)
    with pytest.raises(ValidationError, match="Fee"):
        construct_unsigned(authorization)


def test_b2_signing_refuses_altered_expiry_before_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = transaction()
    identity = transaction_identity(original)
    authorization = write_authorization(
        tmp_path, operation="sign", operations=("sign",), identity=identity
    )
    capability = issue_execution_capability(authorization, operation="sign", execution_intent=True)
    artifact = unsigned_artifact(authorization.authorization_id)
    artifact["transaction"]["LastLedgerSequence"] = 1005
    monkeypatch.setattr(workflow, "sign", lambda *_args, **_kwargs: pytest.fail("signed"))
    with pytest.raises(ValidationError, match="plus exactly 4"):
        sign_unsigned(
            authorization,
            artifact,
            credential=None,
            capability=capability,
            execution_intent=True,
        )


def test_b2_submission_refuses_altered_expiry_before_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = transaction_identity(transaction())
    authorization = write_authorization(
        tmp_path, operation="submit", operations=("submit",), identity=identity
    )
    capability = issue_execution_capability(
        authorization, operation="submit", execution_intent=True
    )
    artifact = signed_artifact(authorization.authorization_id)
    altered = dict(artifact["transaction"])
    altered["LastLedgerSequence"] = 1006
    monkeypatch.setattr(
        workflow,
        "_signed_artifact_checks",
        lambda _authorization, _artifact: (altered, "BLOB", "F" * 64),
    )
    monkeypatch.setattr(workflow, "_submission_preflight", lambda *_args: pytest.fail("network"))
    journal = OperationJournal(tmp_path / "journal.sqlite3")
    try:
        with pytest.raises(ValidationError, match="plus exactly 4"):
            submit_once(
                authorization,
                artifact,
                journal,
                capability=capability,
                execution_intent=True,
            )
    finally:
        journal.close()


def r2_submission_artifact(
    tmp_path: Path,
    *,
    construction_index: int,
    construction_hash: str = "A" * 64,
) -> tuple[ExecutionAuthorization, ExecutionCapability, dict[str, Any]]:
    value = transaction()
    value["LastLedgerSequence"] = construction_index + 4
    identity = transaction_identity(value)
    authorization = write_authorization(
        tmp_path,
        operation="submit",
        operations=("submit",),
        identity=identity,
    )
    capability = issue_execution_capability(
        authorization, operation="submit", execution_intent=True
    )
    artifact = signed_artifact(authorization.authorization_id)
    signed_value = dict(value)
    signed_value["SigningPubKey"] = "PUBLIC"
    signed_value["TxnSignature"] = "SIGNATURE"
    artifact["transaction"] = signed_value
    artifact["transaction_identity"] = identity
    artifact["network"]["validated_ledger_index"] = construction_index
    artifact["network"]["validated_ledger_hash"] = construction_hash
    artifact["construction"]["validated_ledger_index"] = construction_index
    artifact["construction"]["validated_ledger_hash"] = construction_hash
    return authorization, capability, artifact


def install_r2_submission_mocks(
    monkeypatch: pytest.MonkeyPatch,
    artifact: dict[str, Any],
    *,
    current_index: int,
    current_hash: str,
    historical_hash: str | None,
) -> list[str]:
    monkeypatch.setattr(
        workflow,
        "_signed_artifact_checks",
        lambda _authorization, _artifact: (
            artifact["transaction"],
            "BLOB",
            artifact["transaction_hash"],
        ),
    )
    monkeypatch.setattr(
        workflow,
        "verify_network",
        lambda _endpoint: network_observation(current_index, current_hash),
    )

    def ledger_hash(_endpoint: str, _index: int) -> str:
        if historical_hash is None:
            raise RpcError("exact historical ledger is unavailable")
        return historical_hash

    monkeypatch.setattr(workflow, "_ledger_hash", ledger_hash)

    def account(_endpoint: str, address: str, _ledger: int) -> dict[str, object]:
        return {"Account": address, "Flags": 0, "OwnerCount": 0, "Sequence": 1}

    monkeypatch.setattr(workflow, "_validated_account", account)
    monkeypatch.setattr(workflow, "_fee_drops", lambda _endpoint: 10)
    submit_calls: list[str] = []

    def rpc(_endpoint: str, method: str, _params: dict[str, Any]) -> dict[str, Any]:
        if method == "submit":
            submit_calls.append(method)
        return {"result": {"engine_result": "tesSUCCESS"}}

    monkeypatch.setattr(workflow, "rpc_raw", rpc)
    return submit_calls


@pytest.mark.parametrize(
    ("case", "construction_index", "current_index", "current_hash", "historical_hash", "message"),
    [
        ("future", 1996, 1000, "C" * 64, "A" * 64, "ahead"),
        ("false-hash", 1000, 1001, "C" * 64, "B" * 64, "does not match"),
        ("missing-history", 1000, 1001, "C" * 64, None, "unavailable"),
        ("inconsistent-current", 1000, 1000, "C" * 64, "A" * 64, "inconsistent"),
    ],
)
def test_r2_submission_refuses_untrusted_construction_ledger_without_submit_rpc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    construction_index: int,
    current_index: int,
    current_hash: str,
    historical_hash: str | None,
    message: str,
) -> None:
    del case
    authorization, capability, artifact = r2_submission_artifact(
        tmp_path, construction_index=construction_index
    )
    submit_calls = install_r2_submission_mocks(
        monkeypatch,
        artifact,
        current_index=current_index,
        current_hash=current_hash,
        historical_hash=historical_hash,
    )
    journal = OperationJournal(tmp_path / "journal.sqlite3")
    try:
        with pytest.raises((ValidationError, RpcError), match=message):
            submit_once(
                authorization,
                artifact,
                journal,
                capability=capability,
                execution_intent=True,
            )
    finally:
        journal.close()
    assert submit_calls == []


@pytest.mark.parametrize(
    ("current_index", "current_hash"),
    [(1001, "C" * 64), (1000, "A" * 64)],
)
def test_r2_submission_accepts_verified_historical_or_current_construction_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    current_index: int,
    current_hash: str,
) -> None:
    authorization, capability, artifact = r2_submission_artifact(tmp_path, construction_index=1000)
    submit_calls = install_r2_submission_mocks(
        monkeypatch,
        artifact,
        current_index=current_index,
        current_hash=current_hash,
        historical_hash="A" * 64,
    )
    journal = OperationJournal(tmp_path / "journal.sqlite3")
    try:
        result = submit_once(
            authorization,
            artifact,
            journal,
            capability=capability,
            execution_intent=True,
        )
    finally:
        journal.close()
    assert result["engine_result"] == "tesSUCCESS"
    assert submit_calls == ["submit"]


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
    encoded = value["Memos"][0]["Memo"]
    assert encoded["MemoData"] == REQUIRED_LABEL.encode().hex().upper()
    assert encoded["MemoType"] == MEMO_TYPE.encode().hex().upper()


def test_altered_memo_is_refused() -> None:
    value = transaction()
    value["Memos"] = [{"Memo": {"MemoData": "00", "MemoType": MEMO_TYPE.encode().hex().upper()}}]
    with pytest.raises(ValidationError, match="Memo"):
        validate_transaction_structure(value, source=SOURCE, destination=DESTINATION, signed=False)


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_c1_all_http_redirect_classes_are_refused(code: int) -> None:
    handler = workflow._NoRedirectHandler()
    request = workflow.urllib.request.Request(PRIMARY_RPC_ENDPOINT)
    with pytest.raises(RpcError, match=f"redirect {code}"):
        handler.redirect_request(request, None, code, "redirect", {}, "https://hostile.example/")


def test_c1_environment_proxies_are_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://hostile.invalid:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://hostile.invalid:8080")
    observed_handlers: list[object] = []

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def geturl(self) -> str:
            return PRIMARY_RPC_ENDPOINT

        def read(self) -> bytes:
            return b'{"result":{"status":"success"}}'

    class Opener:
        def open(self, _request: object, *, timeout: float) -> Response:
            assert timeout == 15.0
            return Response()

    def build_opener(*handlers: object) -> Opener:
        observed_handlers.extend(handlers)
        return Opener()

    monkeypatch.setattr(workflow.urllib.request, "build_opener", build_opener)
    workflow._post_json(PRIMARY_RPC_ENDPOINT, {"method": "server_info"})
    proxy_handlers = [
        handler
        for handler in observed_handlers
        if isinstance(handler, workflow.urllib.request.ProxyHandler)
    ]
    assert len(proxy_handlers) == 1
    assert proxy_handlers[0].proxies == {}


def test_c1_unexpected_response_destination_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def geturl(self) -> str:
            return "https://hostile.example/"

        def read(self) -> bytes:
            return b"{}"

    class Opener:
        def open(self, _request: object, *, timeout: float) -> Response:
            del timeout
            return Response()

    monkeypatch.setattr(workflow.urllib.request, "build_opener", lambda *_handlers: Opener())
    with pytest.raises(RpcError, match="destination changed"):
        workflow._post_json(PRIMARY_RPC_ENDPOINT, {})


def test_c2_replay_claim_survives_restart_and_changed_authorization_id(tmp_path: Path) -> None:
    path = tmp_path / "operations.sqlite3"
    first = OperationJournal(path)
    first.claim("auth-1", "submit", "A" * 64)
    first.close()
    second = OperationJournal(path)
    try:
        with pytest.raises(ReplayRefused, match="already claimed"):
            second.claim("auth-1", "submit", "B" * 64)
        with pytest.raises(ReplayRefused, match="already claimed"):
            second.claim("auth-2", "submit", "A" * 64)
    finally:
        second.close()


@pytest.mark.parametrize(
    ("issued_at", "expires_at", "message"),
    [
        (FROZEN_NOW - timedelta(hours=2), FROZEN_NOW - timedelta(seconds=1), "expired"),
        (FROZEN_NOW + timedelta(seconds=1), FROZEN_NOW + timedelta(hours=1), "future-dated"),
        (
            FROZEN_NOW - timedelta(minutes=1),
            FROZEN_NOW - timedelta(minutes=1) + timedelta(hours=24, seconds=1),
            "exceeds 24 hours",
        ),
        (FROZEN_NOW, FROZEN_NOW, "contradictory"),
    ],
)
def test_c3_invalid_authorization_times_are_refused(
    tmp_path: Path,
    issued_at: datetime,
    expires_at: datetime,
    message: str,
) -> None:
    path = tmp_path / "bad-time.json"
    path.write_text(
        json.dumps(authorization_payload(issued_at=issued_at, expires_at=expires_at)),
        encoding="utf-8",
    )
    path.chmod(0o600)
    with pytest.raises(AuthorizationError, match=message):
        load_authorization(path, operation="construct", now=FROZEN_NOW)


@pytest.mark.parametrize(
    ("field_name", "value", "message"),
    [
        ("issued_at", "not-a-time", "issued_at"),
        ("expires_at", "not-a-time", "expires_at"),
        ("issued_at", "2026-10-24T19:00:00", "timezone-aware"),
        ("expires_at", "2026-10-25T01:00:00-07:00", "must use UTC"),
    ],
)
def test_c3_malformed_or_inconsistent_timezone_is_refused(
    tmp_path: Path, field_name: str, value: str, message: str
) -> None:
    payload = authorization_payload()
    payload[field_name] = value
    path = tmp_path / f"bad-{field_name}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(AuthorizationError, match=message):
        load_authorization(path, operation="construct", now=FROZEN_NOW)


def test_c3_missing_issuance_timestamp_is_refused(tmp_path: Path) -> None:
    payload = authorization_payload()
    del payload["issued_at"]
    path = tmp_path / "missing-issued-at.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(AuthorizationError, match="missing"):
        load_authorization(path, operation="construct", now=FROZEN_NOW)


def test_c3_missing_expiration_timestamp_is_refused(tmp_path: Path) -> None:
    payload = authorization_payload()
    del payload["expires_at"]
    path = tmp_path / "missing-expires-at.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(AuthorizationError, match="missing"):
        load_authorization(path, operation="construct", now=FROZEN_NOW)


def test_c3_expiration_at_current_instant_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "expires-now.json"
    path.write_text(
        json.dumps(
            authorization_payload(
                issued_at=FROZEN_NOW - timedelta(hours=1),
                expires_at=FROZEN_NOW,
            )
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    with pytest.raises(AuthorizationError, match="expired"):
        load_authorization(path, operation="construct", now=FROZEN_NOW)


def test_c3_exact_24_hour_lifetime_is_allowed(tmp_path: Path) -> None:
    issued_at = FROZEN_NOW - timedelta(minutes=1)
    authorization = write_authorization(
        tmp_path,
        issued_at=issued_at,
        expires_at=issued_at + timedelta(hours=24),
    )
    assert authorization.expires_at - authorization.issued_at == timedelta(hours=24)


@pytest.mark.parametrize("mode", [0o400, 0o640, 0o700])
def test_c3_private_mode_0600_is_preserved(tmp_path: Path, mode: int) -> None:
    path = tmp_path / "permissive.json"
    path.write_text(json.dumps(authorization_payload()), encoding="utf-8")
    path.chmod(mode)
    with pytest.raises(AuthorizationError, match="mode 0600"):
        load_authorization(path, operation="construct", now=FROZEN_NOW)


def test_c3_expiration_between_capability_and_signing_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = transaction_identity(transaction())
    authorization = write_authorization(
        tmp_path,
        operation="sign",
        operations=("sign",),
        identity=identity,
        expires_at=FROZEN_NOW + timedelta(seconds=1),
    )
    capability = issue_execution_capability(authorization, operation="sign", execution_intent=True)
    clock = {"now": FROZEN_NOW}

    class FakeWallet:
        address = SOURCE

        @staticmethod
        def from_seed(_credential: str) -> FakeWallet:
            clock["now"] = FROZEN_NOW + timedelta(seconds=2)
            return FakeWallet()

    monkeypatch.setattr(workflow, "_utc_now", lambda: clock["now"])
    monkeypatch.setattr(workflow, "Wallet", FakeWallet)
    monkeypatch.setattr(workflow, "sign", lambda *_args, **_kwargs: pytest.fail("signed"))
    with pytest.raises(AuthorizationError, match="expired"):
        sign_unsigned(
            authorization,
            unsigned_artifact(authorization.authorization_id),
            credential="deliberately-not-a-key",
            capability=capability,
            execution_intent=True,
        )


def test_c3_expiration_between_preflight_and_submission_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = transaction_identity(transaction())
    authorization = write_authorization(
        tmp_path,
        operation="submit",
        operations=("submit",),
        identity=identity,
        expires_at=FROZEN_NOW + timedelta(seconds=1),
    )
    capability = issue_execution_capability(
        authorization, operation="submit", execution_intent=True
    )
    artifact = signed_artifact(authorization.authorization_id)
    monkeypatch.setattr(
        workflow,
        "_signed_artifact_checks",
        lambda _authorization, _artifact: (artifact["transaction"], "BLOB", "F" * 64),
    )
    clock = {"now": FROZEN_NOW}

    def expire_during_preflight(*_args: object) -> None:
        clock["now"] = FROZEN_NOW + timedelta(seconds=2)

    monkeypatch.setattr(workflow, "_submission_preflight", expire_during_preflight)
    monkeypatch.setattr(workflow, "_utc_now", lambda: clock["now"])
    monkeypatch.setattr(workflow, "rpc_raw", lambda *_args, **_kwargs: pytest.fail("submitted"))
    journal = OperationJournal(tmp_path / "journal.sqlite3")
    try:
        with pytest.raises(AuthorizationError, match="expired"):
            submit_once(
                authorization,
                artifact,
                journal,
                capability=capability,
                execution_intent=True,
            )
    finally:
        journal.close()


def no_network(*_args: object, **_kwargs: object) -> None:
    pytest.fail("network was contacted")


def test_c4_setup_accounts_direct_call_defaults_to_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authorization = write_authorization(tmp_path, operation="faucet", operations=("faucet",))
    monkeypatch.setattr(workflow, "verify_network", no_network)
    journal = OperationJournal(tmp_path / "journal.sqlite3")
    try:
        with pytest.raises(AuthorizationError, match="intent"):
            setup_accounts(authorization, journal)
    finally:
        journal.close()


def test_c4_sign_direct_call_defaults_to_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = transaction_identity(transaction())
    authorization = write_authorization(
        tmp_path, operation="sign", operations=("sign",), identity=identity
    )
    monkeypatch.setattr(workflow, "sign", lambda *_args, **_kwargs: pytest.fail("signed"))
    with pytest.raises(AuthorizationError, match="intent"):
        sign_unsigned(
            authorization,
            unsigned_artifact(authorization.authorization_id),
            credential=None,
        )


def test_c4_submit_direct_call_defaults_to_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = transaction_identity(transaction())
    authorization = write_authorization(
        tmp_path, operation="submit", operations=("submit",), identity=identity
    )
    monkeypatch.setattr(workflow, "rpc_raw", no_network)
    journal = OperationJournal(tmp_path / "journal.sqlite3")
    try:
        with pytest.raises(AuthorizationError, match="intent"):
            submit_once(authorization, signed_artifact(authorization.authorization_id), journal)
    finally:
        journal.close()


@pytest.mark.parametrize("protected", ["setup", "sign", "submit"])
def test_c4_true_intent_without_capability_is_refused(
    protected: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = None if protected == "setup" else transaction_identity(transaction())
    operation = "faucet" if protected == "setup" else protected
    authorization = write_authorization(
        tmp_path, operation=operation, operations=(operation,), identity=identity
    )
    monkeypatch.setattr(workflow, "verify_network", no_network)
    monkeypatch.setattr(workflow, "rpc_raw", no_network)
    monkeypatch.setattr(workflow, "sign", lambda *_args, **_kwargs: pytest.fail("signed"))
    journal = OperationJournal(tmp_path / "journal.sqlite3")
    try:
        with pytest.raises(AuthorizationError, match="capability"):
            if protected == "setup":
                setup_accounts(authorization, journal, execution_intent=True)
            elif protected == "sign":
                sign_unsigned(
                    authorization,
                    unsigned_artifact(authorization.authorization_id),
                    credential=None,
                    execution_intent=True,
                )
            else:
                submit_once(
                    authorization,
                    signed_artifact(authorization.authorization_id),
                    journal,
                    execution_intent=True,
                )
    finally:
        journal.close()


@pytest.mark.parametrize("protected", ["setup", "sign", "submit"])
def test_c4_false_intent_with_valid_capability_is_refused(
    protected: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = None if protected == "setup" else transaction_identity(transaction())
    operation = "faucet" if protected == "setup" else protected
    authorization = write_authorization(
        tmp_path, operation=operation, operations=(operation,), identity=identity
    )
    capability = issue_execution_capability(
        authorization, operation=operation, execution_intent=True
    )
    monkeypatch.setattr(workflow, "verify_network", no_network)
    monkeypatch.setattr(workflow, "rpc_raw", no_network)
    monkeypatch.setattr(workflow, "sign", lambda *_args, **_kwargs: pytest.fail("signed"))
    journal = OperationJournal(tmp_path / "journal.sqlite3")
    try:
        with pytest.raises(AuthorizationError, match="intent"):
            if protected == "setup":
                setup_accounts(
                    authorization,
                    journal,
                    capability=capability,
                    execution_intent=False,
                )
            elif protected == "sign":
                sign_unsigned(
                    authorization,
                    unsigned_artifact(authorization.authorization_id),
                    credential=None,
                    capability=capability,
                    execution_intent=False,
                )
            else:
                submit_once(
                    authorization,
                    signed_artifact(authorization.authorization_id),
                    journal,
                    capability=capability,
                    execution_intent=False,
                )
    finally:
        journal.close()


@pytest.mark.parametrize("protected", ["setup", "sign", "submit"])
def test_c4_malformed_capability_is_refused(
    protected: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = None if protected == "setup" else transaction_identity(transaction())
    operation = "faucet" if protected == "setup" else protected
    authorization = write_authorization(
        tmp_path, operation=operation, operations=(operation,), identity=identity
    )
    monkeypatch.setattr(workflow, "verify_network", no_network)
    monkeypatch.setattr(workflow, "rpc_raw", no_network)
    monkeypatch.setattr(workflow, "sign", lambda *_args, **_kwargs: pytest.fail("signed"))
    journal = OperationJournal(tmp_path / "journal.sqlite3")
    try:
        with pytest.raises(AuthorizationError, match="capability"):
            if protected == "setup":
                setup_accounts(
                    authorization,
                    journal,
                    capability=object(),  # type: ignore[arg-type]
                    execution_intent=True,
                )
            elif protected == "sign":
                sign_unsigned(
                    authorization,
                    unsigned_artifact(authorization.authorization_id),
                    credential=None,
                    capability=object(),  # type: ignore[arg-type]
                    execution_intent=True,
                )
            else:
                submit_once(
                    authorization,
                    signed_artifact(authorization.authorization_id),
                    journal,
                    capability=object(),  # type: ignore[arg-type]
                    execution_intent=True,
                )
    finally:
        journal.close()


def test_c4_capability_cannot_cross_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = transaction_identity(transaction())
    authorization = write_authorization(
        tmp_path,
        operation="sign",
        operations=("faucet", "sign"),
        identity=identity,
    )
    capability = issue_execution_capability(authorization, operation="sign", execution_intent=True)
    monkeypatch.setattr(workflow, "verify_network", no_network)
    journal = OperationJournal(tmp_path / "journal.sqlite3")
    try:
        with pytest.raises(AuthorizationError, match="operation"):
            setup_accounts(
                authorization,
                journal,
                capability=capability,
                execution_intent=True,
            )
    finally:
        journal.close()


def test_c4_capability_cannot_cross_transaction(tmp_path: Path) -> None:
    identity = transaction_identity(transaction())
    authorization = write_authorization(
        tmp_path, operation="sign", operations=("sign",), identity=identity
    )
    capability = issue_execution_capability(authorization, operation="sign", execution_intent=True)
    artifact = unsigned_artifact(authorization.authorization_id)
    artifact["transaction_identity"] = "B" * 64
    with pytest.raises(AuthorizationError, match="authorized transaction"):
        sign_unsigned(
            authorization,
            artifact,
            credential=None,
            capability=capability,
            execution_intent=True,
        )


def test_c4_valid_capability_still_refuses_missing_credentials(tmp_path: Path) -> None:
    identity = transaction_identity(transaction())
    authorization = write_authorization(
        tmp_path, operation="sign", operations=("sign",), identity=identity
    )
    capability = issue_execution_capability(authorization, operation="sign", execution_intent=True)
    with pytest.raises(CredentialError, match="required"):
        sign_unsigned(
            authorization,
            unsigned_artifact(authorization.authorization_id),
            credential=None,
            capability=capability,
            execution_intent=True,
        )


def test_c4_network_mismatch_is_refused_before_library_action(tmp_path: Path) -> None:
    path = tmp_path / "wrong-network.json"
    path.write_text(json.dumps(authorization_payload(network_id=0)), encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(NetworkRefused, match="Mainnet"):
        load_authorization(path, operation="construct", now=FROZEN_NOW)


@pytest.mark.parametrize("protected", ["setup", "sign", "submit"])
def test_c4_direct_invocation_refuses_mutated_network_binding(
    protected: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = None if protected == "setup" else transaction_identity(transaction())
    operation = "faucet" if protected == "setup" else protected
    authorization = write_authorization(
        tmp_path, operation=operation, operations=(operation,), identity=identity
    )
    capability = issue_execution_capability(
        authorization, operation=operation, execution_intent=True
    )
    mismatched = replace(authorization, network_id=0)
    monkeypatch.setattr(workflow, "verify_network", no_network)
    monkeypatch.setattr(workflow, "rpc_raw", no_network)
    monkeypatch.setattr(workflow, "sign", lambda *_args, **_kwargs: pytest.fail("signed"))
    journal = OperationJournal(tmp_path / "journal.sqlite3")
    try:
        with pytest.raises(NetworkRefused, match="Testnet ID 1"):
            if protected == "setup":
                setup_accounts(
                    mismatched,
                    journal,
                    capability=capability,
                    execution_intent=True,
                )
            elif protected == "sign":
                sign_unsigned(
                    mismatched,
                    unsigned_artifact(authorization.authorization_id),
                    credential=None,
                    capability=capability,
                    execution_intent=True,
                )
            else:
                submit_once(
                    mismatched,
                    signed_artifact(authorization.authorization_id),
                    journal,
                    capability=capability,
                    execution_intent=True,
                )
    finally:
        journal.close()


def test_b1_complete_ledger_ranges_are_strict_and_gap_aware() -> None:
    assert parse_complete_ledgers("1-5,6-10") == ((1, 5), (6, 10))
    assert history_covers("1-5,6-10", 3, 9)
    assert not history_covers("1-5,7-10", 3, 9)
    with pytest.raises(RpcError, match="invalid"):
        parse_complete_ledgers("1-five")
    with pytest.raises(RpcError, match="unordered"):
        parse_complete_ledgers("6-10,1-5")


def install_poll_artifact_checks(monkeypatch: pytest.MonkeyPatch, artifact: dict[str, Any]) -> None:
    monkeypatch.setattr(
        workflow,
        "_signed_artifact_checks",
        lambda _authorization, _artifact: (
            artifact["transaction"],
            "BLOB",
            artifact["transaction_hash"],
        ),
    )


def test_b1_included_in_last_valid_ledger_wins_after_ledger_advance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = transaction_identity(transaction())
    authorization = write_authorization(
        tmp_path, operation="poll", operations=("poll",), identity=identity
    )
    artifact = signed_artifact(authorization.authorization_id)
    install_poll_artifact_checks(monkeypatch, artifact)
    observations = iter([network_observation(1004, "D" * 64), network_observation(1005, "E" * 64)])
    calls: list[str] = []

    def verify(_endpoint: str) -> dict[str, Any]:
        calls.append("validated-ledger")
        return next(observations)

    def lookup(_endpoint: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
        calls.append(method)
        assert params["min_ledger"] == 1001
        assert params["max_ledger"] == 1004
        tx = artifact["transaction"]
        return {
            "result": {
                **tx,
                "status": "success",
                "validated": True,
                "hash": artifact["transaction_hash"],
                "ledger_index": 1004,
                "meta": {"TransactionResult": "tesSUCCESS"},
            }
        }

    monkeypatch.setattr(workflow, "verify_network", verify)
    monkeypatch.setattr(workflow, "rpc_raw", lookup)
    monkeypatch.setattr(
        workflow,
        "_ledger_hash",
        lambda _endpoint, index: {1000: "A" * 64, 1004: "D" * 64}[index],
    )
    journal = journal_with_submission(
        tmp_path, authorization.authorization_id, artifact["transaction_hash"]
    )
    try:
        result = poll_validated(authorization, artifact, journal, timeout_seconds=1)
    finally:
        journal.close()
    assert result["ledger_index"] == 1004
    assert calls[0] == "validated-ledger"
    assert calls.index("tx") > calls.index("validated-ledger")


def run_not_found_poll(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    searched_all: object,
    complete_ledgers: str,
    before_complete_ledgers: str | None = None,
    after_complete_ledgers: str | None = None,
    before_index: int = 1005,
    after_index: int = 1005,
    before_hash: str = "E" * 64,
    after_hash: str = "E" * 64,
) -> tuple[ExecutionAuthorization, dict[str, Any], Path]:
    identity = transaction_identity(transaction())
    authorization = write_authorization(
        tmp_path, operation="poll", operations=("poll",), identity=identity
    )
    artifact = signed_artifact(authorization.authorization_id)
    install_poll_artifact_checks(monkeypatch, artifact)
    before_history = (
        complete_ledgers if before_complete_ledgers is None else before_complete_ledgers
    )
    after_history = complete_ledgers if after_complete_ledgers is None else after_complete_ledgers
    observations = iter(
        [
            network_observation(before_index, before_hash, before_history),
            network_observation(after_index, after_hash, after_history),
        ]
    )
    calls: list[str] = []

    def verify(_endpoint: str) -> dict[str, Any]:
        calls.append("validated-ledger")
        return next(observations)

    def lookup(_endpoint: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
        calls.append(method)
        assert method == "tx"
        assert params["min_ledger"] == 1001
        assert params["max_ledger"] == 1004
        return {
            "result": {
                "status": "error",
                "error": "txnNotFound",
                "searched_all": searched_all,
            }
        }

    monkeypatch.setattr(workflow, "verify_network", verify)
    monkeypatch.setattr(workflow, "rpc_raw", lookup)
    ledger_hashes = {1000: "A" * 64, before_index: before_hash}
    monkeypatch.setattr(workflow, "_ledger_hash", lambda _endpoint, index: ledger_hashes[index])
    journal_path = tmp_path / "operations.sqlite3"
    journal = journal_with_submission(
        tmp_path, authorization.authorization_id, artifact["transaction_hash"]
    )
    try:
        with pytest.raises(RpcError):
            poll_validated(authorization, artifact, journal, timeout_seconds=1)
    finally:
        journal.close()
    assert calls[0] == "validated-ledger"
    assert calls.index("tx") > calls.index("validated-ledger")
    return authorization, artifact, journal_path


def test_b1_conclusive_bounded_non_inclusion_requires_all_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authorization, _artifact, path = run_not_found_poll(
        tmp_path,
        monkeypatch,
        searched_all=True,
        complete_ledgers="1-2000",
    )
    assert journal_status(path, authorization.authorization_id) == "EXPIRED_NOT_INCLUDED"


def test_t1c_prequery_history_gap_remains_unknown_then_original_hash_validates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        workflow,
        "construct_unsigned",
        lambda *_args, **_kwargs: pytest.fail("replacement Payment was constructed"),
    )
    monkeypatch.setattr(
        workflow,
        "submit_once",
        lambda *_args, **_kwargs: pytest.fail("submit was attempted"),
    )
    authorization, artifact, path = run_not_found_poll(
        tmp_path,
        monkeypatch,
        searched_all=True,
        complete_ledgers="1-1006",
        before_complete_ledgers="1-1001,1003-1006",
        after_complete_ledgers="1-1006",
        before_index=1006,
        after_index=1006,
        before_hash="E" * 64,
        after_hash="E" * 64,
    )
    assert journal_status(path, authorization.authorization_id) == "UNKNOWN_HISTORY_GAP"
    assert journal_claim_count(path) == 1

    observations = iter([network_observation(1006, "E" * 64), network_observation(1007, "F" * 64)])
    monkeypatch.setattr(workflow, "verify_network", lambda _endpoint: next(observations))

    def validated_lookup(_endpoint: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
        assert method == "tx"
        assert params["transaction"] == artifact["transaction_hash"]
        return {
            "result": {
                **artifact["transaction"],
                "status": "success",
                "validated": True,
                "hash": artifact["transaction_hash"],
                "ledger_index": 1002,
                "meta": {"TransactionResult": "tesSUCCESS"},
            }
        }

    monkeypatch.setattr(workflow, "rpc_raw", validated_lookup)
    ledger_hashes = {1000: "A" * 64, 1002: "B" * 64, 1006: "E" * 64}
    monkeypatch.setattr(workflow, "_ledger_hash", lambda _endpoint, index: ledger_hashes[index])
    journal = OperationJournal(path)
    try:
        result = poll_validated(authorization, artifact, journal, timeout_seconds=1)
    finally:
        journal.close()
    assert result["transaction_hash"] == artifact["transaction_hash"]
    assert result["ledger_index"] == 1002
    assert journal_status(path, authorization.authorization_id) == "validated"
    assert journal_claim_count(path) == 1


def test_t1c_postquery_history_gap_remains_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authorization, _artifact, path = run_not_found_poll(
        tmp_path,
        monkeypatch,
        searched_all=True,
        complete_ledgers="1-1006",
        before_complete_ledgers="1-1006",
        after_complete_ledgers="1-1001,1003-1006",
        before_index=1006,
        after_index=1006,
        before_hash="E" * 64,
        after_hash="E" * 64,
    )
    assert journal_status(path, authorization.authorization_id) == "UNKNOWN_HISTORY_GAP"
    assert journal_claim_count(path) == 1


def test_r1_prequery_v_plus_three_cannot_become_terminal_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authorization, _artifact, path = run_not_found_poll(
        tmp_path,
        monkeypatch,
        searched_all=True,
        complete_ledgers="1-2000",
        before_index=1003,
        after_index=1005,
        before_hash="D" * 64,
        after_hash="E" * 64,
    )
    assert (
        journal_status(path, authorization.authorization_id)
        == "UNKNOWN_PREQUERY_LEDGER_NOT_EXPIRED"
    )


@pytest.mark.parametrize(
    ("before_index", "expected_status"),
    [
        (1004, "UNKNOWN_PREQUERY_LEDGER_NOT_EXPIRED"),
        (1005, "EXPIRED_NOT_INCLUDED"),
    ],
)
def test_r1_prequery_expiry_boundary_is_strict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    before_index: int,
    expected_status: str,
) -> None:
    authorization, _artifact, path = run_not_found_poll(
        tmp_path,
        monkeypatch,
        searched_all=True,
        complete_ledgers="1-2000",
        before_index=before_index,
        after_index=1005,
    )
    assert journal_status(path, authorization.authorization_id) == expected_status


@pytest.mark.parametrize("searched_all", [None, False, 1, "true"])
def test_b1_missing_or_nonliteral_searched_all_retains_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, searched_all: object
) -> None:
    authorization, _artifact, path = run_not_found_poll(
        tmp_path,
        monkeypatch,
        searched_all=searched_all,
        complete_ledgers="1-2000",
    )
    assert journal_status(path, authorization.authorization_id) == "UNKNOWN_INCOMPLETE_SEARCH"


@pytest.mark.parametrize("complete_ledgers", ["empty", "1-1002,1004-2000"])
def test_b1_incomplete_or_gapped_history_retains_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    complete_ledgers: str,
) -> None:
    authorization, _artifact, path = run_not_found_poll(
        tmp_path,
        monkeypatch,
        searched_all=True,
        complete_ledgers=complete_ledgers,
    )
    assert journal_status(path, authorization.authorization_id) == "UNKNOWN_HISTORY_GAP"


def test_b1_contradictory_same_index_hashes_retain_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authorization, _artifact, path = run_not_found_poll(
        tmp_path,
        monkeypatch,
        searched_all=True,
        complete_ledgers="1-2000",
        before_index=1004,
        after_index=1004,
        before_hash="D" * 64,
        after_hash="E" * 64,
    )
    assert journal_status(path, authorization.authorization_id) == "UNKNOWN_CONTRADICTORY_NODE"


def test_r1_original_hash_can_validate_after_prequery_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authorization, artifact, path = run_not_found_poll(
        tmp_path,
        monkeypatch,
        searched_all=True,
        complete_ledgers="1-2000",
        before_index=1003,
        after_index=1005,
        before_hash="D" * 64,
        after_hash="E" * 64,
    )
    assert (
        journal_status(path, authorization.authorization_id)
        == "UNKNOWN_PREQUERY_LEDGER_NOT_EXPIRED"
    )

    observations = iter([network_observation(1005, "F" * 64), network_observation(1006, "G" * 64)])
    monkeypatch.setattr(workflow, "verify_network", lambda _endpoint: next(observations))

    def validated_lookup(_endpoint: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
        assert method == "tx"
        assert params["transaction"] == artifact["transaction_hash"]
        return {
            "result": {
                **artifact["transaction"],
                "status": "success",
                "validated": True,
                "hash": artifact["transaction_hash"],
                "ledger_index": 1004,
                "meta": {"TransactionResult": "tesSUCCESS"},
            }
        }

    monkeypatch.setattr(workflow, "rpc_raw", validated_lookup)
    ledger_hashes = {1000: "A" * 64, 1004: "D" * 64, 1005: "F" * 64}
    monkeypatch.setattr(workflow, "_ledger_hash", lambda _endpoint, index: ledger_hashes[index])
    journal = OperationJournal(path)
    try:
        result = poll_validated(authorization, artifact, journal, timeout_seconds=1)
    finally:
        journal.close()
    assert result["transaction_hash"] == artifact["transaction_hash"]
    assert result["ledger_index"] == 1004
    assert journal_status(path, authorization.authorization_id) == "validated"


def test_sensitive_operation_requires_explicit_execute_switch() -> None:
    with pytest.raises(AuthorizationError, match="--execute"):
        require_explicit_execute(False)
    require_explicit_execute(True)


def test_missing_execution_authorization_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(AuthorizationError, match="regular non-symlink file"):
        load_authorization(tmp_path / "not-created.json", operation="sign", now=FROZEN_NOW)


def test_authorization_reuse_after_file_change_is_refused(tmp_path: Path) -> None:
    authorization = write_authorization(tmp_path, operation="faucet", operations=("faucet",))
    capability = issue_execution_capability(
        authorization, operation="faucet", execution_intent=True
    )
    payload = authorization_payload(authorization_id="changed", operations=("faucet",))
    authorization.file_path.write_text(json.dumps(payload), encoding="utf-8")
    authorization.file_path.chmod(0o600)
    journal = OperationJournal(tmp_path / "journal.sqlite3")
    try:
        with pytest.raises(AuthorizationError, match="changed"):
            setup_accounts(
                authorization,
                journal,
                capability=capability,
                execution_intent=True,
            )
    finally:
        journal.close()


def test_authorization_file_remains_owner_only(tmp_path: Path) -> None:
    authorization = write_authorization(tmp_path)
    assert os.stat(authorization.file_path).st_mode & 0o077 == 0
