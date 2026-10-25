"""Fail-closed T1 public native XRPL Testnet workflow.

This module deliberately does not expose a generic endpoint, generic transaction,
automatic retry, or wallet-generation path. Network-mutating and signing operations
require a separately supplied execution-authorization document.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final, TypeGuard
from urllib.parse import urlparse

from xrpl.core.addresscodec import is_valid_classic_address
from xrpl.core.binarycodec import decode
from xrpl.models.transactions import Memo, Payment, Transaction
from xrpl.transaction import sign
from xrpl.wallet import Wallet

EXPECTED_NETWORK_ID: Final = 1
PRIMARY_RPC_ENDPOINT: Final = "https://s.altnet.rippletest.net:51234/"
SECONDARY_RPC_ENDPOINT: Final = "https://testnet.xrpl-labs.com/"
OFFICIAL_FAUCET_ENDPOINT: Final = "https://faucet.altnet.rippletest.net/accounts"

REQUIRED_LABEL: Final = (
    "XRPL connectivity proof transaction. Not governed by Credence procurement authorization."
)
MEMO_TYPE: Final = "claw-street:t1-connectivity-proof"
AMOUNT_DROPS: Final = "1"
TF_FULLY_CANONICAL_SIG: Final = 0x80000000
LAST_LEDGER_OFFSET: Final = 4
MAX_FEE_DROPS: Final = 1_000
MAX_AUTHORIZATION_LIFETIME: Final = timedelta(hours=24)
AUTH_SCHEMA: Final = "credence.t1.execution-authorization.v2"
UNSIGNED_SCHEMA: Final = "credence.t1.unsigned-payment.v1"
SIGNED_SCHEMA: Final = "credence.t1.signed-payment.v1"
EVIDENCE_SCHEMA: Final = "credence.t1.validated-evidence.v1"
ALLOWED_OPERATIONS: Final = frozenset({"faucet", "construct", "sign", "submit", "poll", "capture"})

_LSF_REQUIRE_DEST_TAG: Final = 0x00020000
_LSF_DISABLE_MASTER: Final = 0x00100000
_LSF_DEPOSIT_AUTH: Final = 0x01000000
_EVM_CHAIN_IDS: Final = frozenset({1_440_000, 1_449_000, 1_449_900})


class T1Error(RuntimeError):
    """Base class for a fail-closed T1 refusal."""


class AuthorizationError(T1Error):
    """The separate T1 execution authorization is absent or invalid."""


class NetworkRefused(T1Error):
    """The endpoint or observed network is not public native XRPL Testnet."""


class ValidationError(T1Error):
    """Input or ledger state does not satisfy the frozen T1 rules."""


class CredentialError(T1Error):
    """Signing credentials are absent, invalid, or belong to another account."""


class ReplayRefused(T1Error):
    """A faucet or submission operation would be repeated."""


class RpcError(T1Error):
    """A public endpoint returned an unavailable or unexpected result."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class ExecutionAuthorization:
    authorization_id: str
    issued_at: datetime
    expires_at: datetime
    operations: frozenset[str]
    source: str
    destination: str
    network_id: int
    label: str
    amount_drops: str
    transaction_identity: str | None
    file_path: Path
    file_sha256: str

    def require(self, operation: str, *, now: datetime | None = None) -> None:
        if operation not in self.operations:
            raise AuthorizationError(f"execution authorization does not allow {operation!r}")
        checked_at = _normalized_now(now)
        if checked_at < self.issued_at:
            raise AuthorizationError("execution authorization is future-dated")
        if checked_at >= self.expires_at:
            raise AuthorizationError("execution authorization has expired")


_CAPABILITY_SEAL: Final = object()


@dataclass(frozen=True)
class ExecutionCapability:
    """Opaque, operation-specific authority for a protected library action."""

    authorization_id: str
    authorization_sha256: str
    operation: str
    transaction_identity: str | None
    _seal: object = field(repr=False, compare=False)


def _is_plain_int(value: object) -> TypeGuard[int]:
    return type(value) is int


def _is_under(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _read_authorization_file(path: Path) -> tuple[Path, bytes, dict[str, Any]]:
    candidate = path.expanduser()
    if candidate.is_symlink():
        raise AuthorizationError("execution authorization must not be a symlink")
    resolved = candidate.resolve()
    repository = Path(__file__).resolve().parents[4]
    if _is_under(resolved, repository):
        raise AuthorizationError("execution authorization must be stored outside the repository")
    try:
        if not resolved.is_file():
            raise AuthorizationError("execution authorization must be a regular non-symlink file")
        if resolved.stat().st_mode & 0o777 != 0o600:
            raise AuthorizationError("execution authorization permissions must be mode 0600")
        raw = resolved.read_bytes()
        data = json.loads(raw.decode("utf-8"))
    except AuthorizationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AuthorizationError("execution authorization is unavailable or invalid") from exc
    if not isinstance(data, dict):
        raise AuthorizationError("execution authorization must be a JSON object")
    return resolved, raw, data


def _normalized_now(now: datetime | None = None) -> datetime:
    value = _utc_now() if now is None else now
    if value.tzinfo is None or value.utcoffset() is None:
        raise AuthorizationError("authorization validation time must be timezone-aware")
    return value.astimezone(UTC)


def _authorization_timestamp(value: object, field_name: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise AuthorizationError(f"execution {field_name} must be an ISO 8601 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AuthorizationError(
            f"execution {field_name} must be an ISO 8601 UTC timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AuthorizationError(f"execution {field_name} must be timezone-aware")
    if parsed.utcoffset() != timedelta(0):
        raise AuthorizationError(f"execution {field_name} must use UTC")
    return parsed.astimezone(UTC)


def _parse_authorization(
    path: Path,
    raw: bytes,
    data: dict[str, Any],
    *,
    operation: str,
    now: datetime | None = None,
) -> ExecutionAuthorization:
    required = {
        "schema",
        "authorization_id",
        "issued_at",
        "expires_at",
        "network_id",
        "label",
        "amount_drops",
        "source",
        "destination",
        "allowed_operations",
        "transaction_identity",
    }
    if set(data) != required:
        raise AuthorizationError("execution authorization has unexpected or missing fields")
    if data["schema"] != AUTH_SCHEMA:
        raise AuthorizationError("execution authorization schema is not T1 v2")
    assert_expected_network_id(data["network_id"])
    if data["label"] != REQUIRED_LABEL or data["amount_drops"] != AMOUNT_DROPS:
        raise AuthorizationError(
            "execution authorization does not bind the frozen label and amount"
        )
    authorization_id = data["authorization_id"]
    if not isinstance(authorization_id, str) or not authorization_id.strip():
        raise AuthorizationError("execution authorization_id must be a nonempty string")
    if len(authorization_id) > 128:
        raise AuthorizationError("execution authorization_id is too long")
    issued_at = _authorization_timestamp(data["issued_at"], "issued_at")
    expires_at = _authorization_timestamp(data["expires_at"], "expires_at")
    if expires_at <= issued_at:
        raise AuthorizationError("execution timestamps are contradictory")
    if expires_at - issued_at > MAX_AUTHORIZATION_LIFETIME:
        raise AuthorizationError("execution authorization lifetime exceeds 24 hours")
    checked_at = _normalized_now(now)
    if issued_at > checked_at:
        raise AuthorizationError("execution authorization is future-dated")
    if expires_at <= checked_at:
        raise AuthorizationError("execution authorization has expired")
    operations = data["allowed_operations"]
    if not isinstance(operations, list) or not operations:
        raise AuthorizationError("allowed_operations must be a nonempty list")
    if any(not isinstance(item, str) for item in operations):
        raise AuthorizationError("allowed_operations entries must be strings")
    operation_set = frozenset(operations)
    if len(operation_set) != len(operations) or not operation_set <= ALLOWED_OPERATIONS:
        raise AuthorizationError("allowed_operations contains duplicates or unknown operations")
    source = require_classic_address(data["source"], "source")
    destination = require_classic_address(data["destination"], "destination")
    if source == destination:
        raise AuthorizationError("source and destination must be distinct")
    transaction_identity = data["transaction_identity"]
    if transaction_identity is not None and (
        not isinstance(transaction_identity, str)
        or len(transaction_identity) != 64
        or any(character not in "0123456789ABCDEF" for character in transaction_identity)
    ):
        raise AuthorizationError("transaction_identity must be null or 64 uppercase hex characters")
    if operation in {"sign", "submit", "poll", "capture"} and transaction_identity is None:
        raise AuthorizationError(f"{operation} authorization requires a transaction_identity")
    authorization = ExecutionAuthorization(
        authorization_id=authorization_id,
        issued_at=issued_at,
        expires_at=expires_at,
        operations=operation_set,
        source=source,
        destination=destination,
        network_id=EXPECTED_NETWORK_ID,
        label=REQUIRED_LABEL,
        amount_drops=AMOUNT_DROPS,
        transaction_identity=transaction_identity,
        file_path=path,
        file_sha256=hashlib.sha256(raw).hexdigest(),
    )
    authorization.require(operation, now=checked_at)
    return authorization


def load_authorization(
    path: Path, *, operation: str, now: datetime | None = None
) -> ExecutionAuthorization:
    """Load a separately created authorization file, never a repository artifact."""

    resolved, raw, data = _read_authorization_file(path)
    return _parse_authorization(resolved, raw, data, operation=operation, now=now)


def revalidate_authorization(
    authorization: ExecutionAuthorization,
    *,
    operation: str,
    now: datetime | None = None,
) -> None:
    """Re-read the private file and confirm its authority immediately before action."""

    resolved, raw, data = _read_authorization_file(authorization.file_path)
    current = _parse_authorization(resolved, raw, data, operation=operation, now=now)
    if current != authorization:
        raise AuthorizationError("execution authorization changed after it was loaded")


def issue_execution_capability(
    authorization: ExecutionAuthorization,
    *,
    operation: str,
    execution_intent: bool = False,
) -> ExecutionCapability:
    """Issue an opaque operation capability only for explicit execution intent."""

    if execution_intent is not True:
        raise AuthorizationError("explicit library execution intent is required")
    revalidate_authorization(authorization, operation=operation)
    return ExecutionCapability(
        authorization_id=authorization.authorization_id,
        authorization_sha256=authorization.file_sha256,
        operation=operation,
        transaction_identity=authorization.transaction_identity,
        _seal=_CAPABILITY_SEAL,
    )


def _require_execution_capability(
    authorization: ExecutionAuthorization,
    capability: ExecutionCapability | None,
    *,
    operation: str,
    execution_intent: bool,
    transaction_identity: str | None = None,
) -> None:
    if execution_intent is not True:
        raise AuthorizationError("explicit library execution intent is required")
    if not isinstance(capability, ExecutionCapability) or capability._seal is not _CAPABILITY_SEAL:
        raise AuthorizationError("valid library execution capability is required")
    if (
        capability.authorization_id != authorization.authorization_id
        or capability.authorization_sha256 != authorization.file_sha256
        or capability.operation != operation
    ):
        raise AuthorizationError("execution capability does not match authorization and operation")
    if authorization.network_id != EXPECTED_NETWORK_ID:
        raise NetworkRefused("execution authorization is not bound to native XRPL Testnet ID 1")
    if authorization.label != REQUIRED_LABEL or authorization.amount_drops != AMOUNT_DROPS:
        raise AuthorizationError("execution authorization lost its frozen payment binding")
    if transaction_identity is not None and (
        authorization.transaction_identity != transaction_identity
        or capability.transaction_identity != transaction_identity
    ):
        raise AuthorizationError("execution capability does not match the authorized transaction")
    revalidate_authorization(authorization, operation=operation)


def require_explicit_execute(execute: bool) -> None:
    if not execute:
        raise AuthorizationError("refusing without the explicit --execute switch")


def require_runtime_private_path(path: Path) -> Path:
    """Keep signed/runtime state outside Git or in the ignored raw-evidence tree."""

    resolved = path.expanduser().absolute()
    repository = Path(__file__).resolve().parents[4]
    raw_evidence = repository / "spike" / "xrpl" / "evidence" / "raw"
    if _is_under(resolved, repository) and not _is_under(resolved, raw_evidence):
        raise ValidationError(
            "sensitive runtime artifacts inside the repository must be under evidence/raw"
        )
    return resolved


def default_journal_path() -> Path:
    repository = Path(__file__).resolve().parents[4]
    return repository / "spike" / "xrpl" / "evidence" / "raw" / "t1" / "operations.sqlite3"


def require_classic_address(value: object, field: str) -> str:
    if not isinstance(value, str) or not is_valid_classic_address(value):
        raise ValidationError(f"{field} must be a valid classic XRPL address")
    return value


def validate_rpc_endpoint(endpoint: str) -> str:
    """Allow only the two frozen public native Testnet HTTPS endpoints."""

    parsed = urlparse(endpoint)
    host = (parsed.hostname or "").lower()
    lowered = endpoint.lower()
    if "xrplevm" in lowered or "evm" in host:
        raise NetworkRefused("XRPL EVM endpoints are explicitly refused")
    if host in {"127.0.0.1", "localhost", "::1"} or host.endswith(".local"):
        raise NetworkRefused("local and standalone XRPL endpoints are explicitly refused")
    if "devnet" in host:
        raise NetworkRefused("XRPL Devnet endpoints are explicitly refused")
    if host in {
        "s1.ripple.com",
        "s2.ripple.com",
        "xrplcluster.com",
        "xrpl.ws",
        "honeycluster.io",
    }:
        raise NetworkRefused("XRPL Mainnet endpoints are explicitly refused")
    if endpoint not in {PRIMARY_RPC_ENDPOINT, SECONDARY_RPC_ENDPOINT}:
        raise NetworkRefused("endpoint is not an allowlisted public native XRPL Testnet endpoint")
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise NetworkRefused("endpoint must use credential-free HTTPS without query or fragment")
    return endpoint


def assert_expected_network_id(network_id: object) -> int:
    if not _is_plain_int(network_id):
        raise NetworkRefused("server did not return an integer native XRPL network ID")
    if network_id == 0:
        raise NetworkRefused("XRPL Mainnet network ID 0 is explicitly refused")
    if network_id == 2:
        raise NetworkRefused("XRPL Devnet network ID 2 is explicitly refused")
    if network_id == 21_337:
        raise NetworkRefused("network ID 21337, including the local standalone network, is refused")
    if network_id in _EVM_CHAIN_IDS:
        raise NetworkRefused("XRPL EVM chain IDs are explicitly refused")
    if network_id != EXPECTED_NETWORK_ID:
        raise NetworkRefused(f"network ID {network_id} is not public native XRPL Testnet ID 1")
    return network_id


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse all redirects instead of letting urllib change the destination."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        del req, fp, msg, headers, newurl
        raise RpcError(f"HTTP redirect {code} is refused")


def _post_json(url: str, body: dict[str, Any], *, timeout: float = 15.0) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "credence-t1-proof/1"},
        method="POST",
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirectHandler(),
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            if response.geturl() != url:
                raise RpcError("HTTP response destination changed unexpectedly")
            payload = response.read()
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RpcError("network request failed; response details were not logged") from exc
    try:
        decoded = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RpcError("endpoint returned non-JSON data") from exc
    if not isinstance(decoded, dict):
        raise RpcError("endpoint returned a non-object JSON response")
    return decoded


def rpc_raw(endpoint: str, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    validate_rpc_endpoint(endpoint)
    request_params = dict(params or {})
    request_params["api_version"] = 1
    return _post_json(endpoint, {"method": method, "params": [request_params]})


def rpc_result(endpoint: str, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    response = rpc_raw(endpoint, method, params)
    result = response.get("result")
    if not isinstance(result, dict) or result.get("status") != "success":
        raise RpcError(f"{method} returned an unexpected result")
    return result


def verify_network(endpoint: str = PRIMARY_RPC_ENDPOINT) -> dict[str, Any]:
    result = rpc_result(endpoint, "server_info")
    info = result.get("info")
    if not isinstance(info, dict):
        raise RpcError("server_info omitted result.info")
    network_id = assert_expected_network_id(info.get("network_id"))
    ledger = info.get("validated_ledger")
    if not isinstance(ledger, dict):
        raise RpcError("server_info has no validated ledger")
    ledger_index = ledger.get("seq")
    ledger_hash = ledger.get("hash")
    if not _is_plain_int(ledger_index) or ledger_index <= 0:
        raise RpcError("server_info returned an invalid validated ledger index")
    if not isinstance(ledger_hash, str) or len(ledger_hash) != 64:
        raise RpcError("server_info returned an invalid validated ledger hash")
    server_state = info.get("server_state")
    if server_state not in {"full", "proposing", "validating"}:
        raise RpcError("server is not in a state suitable for validated-ledger operations")
    complete_ledgers = info.get("complete_ledgers")
    if not isinstance(complete_ledgers, str):
        raise RpcError("server_info omitted complete_ledgers")
    return {
        "endpoint": endpoint,
        "network_id": network_id,
        "validated_ledger_index": ledger_index,
        "validated_ledger_hash": ledger_hash.upper(),
        "server_state": server_state,
        "complete_ledgers": complete_ledgers,
        "reserve_base_xrp": ledger.get("reserve_base_xrp"),
    }


def parse_complete_ledgers(value: str) -> tuple[tuple[int, int], ...]:
    """Parse the server's complete-ledger range expression without assumptions."""

    if value == "empty":
        return ()
    ranges: list[tuple[int, int]] = []
    for part in value.split(","):
        pieces = part.split("-")
        if len(pieces) == 1 and pieces[0].isdecimal():
            start = end = int(pieces[0])
        elif len(pieces) == 2 and all(piece.isdecimal() for piece in pieces):
            start, end = (int(piece) for piece in pieces)
        else:
            raise RpcError("complete_ledgers has an invalid range expression")
        if start <= 0 or end < start:
            raise RpcError("complete_ledgers has an invalid range")
        ranges.append((start, end))
    previous_end = 0
    for start, end in ranges:
        if start <= previous_end:
            raise RpcError("complete_ledgers ranges overlap or are unordered")
        previous_end = end
    return tuple(ranges)


def history_covers(value: str, first_ledger: int, last_ledger: int) -> bool:
    if first_ledger <= 0 or last_ledger < first_ledger:
        raise ValidationError("bounded transaction history range is invalid")
    next_required = first_ledger
    for start, end in parse_complete_ledgers(value):
        if end < next_required:
            continue
        if start > next_required:
            return False
        next_required = end + 1
        if next_required > last_ledger:
            return True
    return False


def _validated_account(endpoint: str, account: str, ledger_index: int) -> dict[str, Any]:
    result = rpc_result(
        endpoint,
        "account_info",
        {"account": account, "ledger_index": ledger_index, "strict": True},
    )
    if result.get("validated") is not True or result.get("ledger_index") != ledger_index:
        raise ValidationError("account_info was not from the pinned validated ledger")
    data = result.get("account_data")
    if not isinstance(data, dict) or data.get("Account") != account:
        raise ValidationError("account_info returned the wrong account")
    return data


def _account_absent(endpoint: str, account: str) -> bool:
    response = rpc_raw(
        endpoint,
        "account_info",
        {"account": account, "ledger_index": "validated", "strict": True},
    )
    result = response.get("result")
    if not isinstance(result, dict):
        raise RpcError("account_info returned no result")
    if result.get("status") == "success":
        data = result.get("account_data")
        if not isinstance(data, dict) or data.get("Account") != account:
            raise RpcError("account_info returned the wrong account")
        return False
    if result.get("error") == "actNotFound":
        return True
    raise RpcError("account_info returned an unexpected account-setup result")


def _drops_from_xrp(value: object, field: str) -> int:
    try:
        drops = Decimal(str(value)) * Decimal(1_000_000)
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError(f"{field} is not a valid XRP amount") from exc
    if drops != drops.to_integral_value() or drops < 0:
        raise ValidationError(f"{field} is not an exact nonnegative drop amount")
    return int(drops)


def _fee_drops(endpoint: str) -> int:
    result = rpc_result(endpoint, "fee")
    drops = result.get("drops")
    if not isinstance(drops, dict):
        raise ValidationError("fee response omitted drops")
    value = drops.get("open_ledger_fee")
    if not isinstance(value, str) or not value.isdecimal():
        raise ValidationError("open_ledger_fee must be an integer drop string")
    fee = int(value)
    if fee <= 0 or fee > MAX_FEE_DROPS:
        raise ValidationError(f"open-ledger fee {fee} is outside the allowed 1..{MAX_FEE_DROPS}")
    return fee


def memo_object() -> dict[str, Any]:
    return {
        "Memo": {
            "MemoData": REQUIRED_LABEL.encode("utf-8").hex().upper(),
            "MemoType": MEMO_TYPE.encode("utf-8").hex().upper(),
        }
    }


def decode_memo(transaction: dict[str, Any]) -> tuple[str, str]:
    memos = transaction.get("Memos")
    if not isinstance(memos, list) or len(memos) != 1 or not isinstance(memos[0], dict):
        raise ValidationError("transaction must contain exactly one Memo")
    memo = memos[0].get("Memo")
    if not isinstance(memo, dict) or set(memo) != {"MemoData", "MemoType"}:
        raise ValidationError("Memo must contain exactly MemoData and MemoType")
    try:
        memo_data = bytes.fromhex(memo["MemoData"]).decode("utf-8")
        memo_type = bytes.fromhex(memo["MemoType"]).decode("utf-8")
    except (KeyError, TypeError, ValueError, UnicodeDecodeError) as exc:
        raise ValidationError("Memo fields must be UTF-8 encoded uppercase hex") from exc
    if memo["MemoData"] != memo["MemoData"].upper() or memo["MemoType"] != memo["MemoType"].upper():
        raise ValidationError("Memo hex must be uppercase")
    if memo_data != REQUIRED_LABEL or memo_type != MEMO_TYPE:
        raise ValidationError("Memo does not contain the frozen label and Claw Street reference")
    return memo_data, memo_type


def validate_transaction_structure(
    transaction: dict[str, Any],
    *,
    source: str,
    destination: str,
    signed: bool,
) -> None:
    if "NetworkID" in transaction:
        raise NetworkRefused("NetworkID must be omitted for native Testnet network ID 1")
    required = {
        "Account",
        "Amount",
        "Destination",
        "Fee",
        "Flags",
        "LastLedgerSequence",
        "Memos",
        "Sequence",
        "SigningPubKey",
        "TransactionType",
    }
    if signed:
        required.add("TxnSignature")
    if set(transaction) != required:
        raise ValidationError("transaction contains unexpected or missing fields")
    if transaction["TransactionType"] != "Payment":
        raise ValidationError("only a Payment is allowed")
    if transaction["Account"] != source or transaction["Destination"] != destination:
        raise ValidationError("transaction accounts do not match execution authorization")
    if source == destination:
        raise ValidationError("source and destination must be distinct")
    if transaction["Amount"] != AMOUNT_DROPS:
        raise ValidationError("payment amount must be exactly 1 drop")
    if transaction["Flags"] != TF_FULLY_CANONICAL_SIG:
        raise ValidationError("only tfFullyCanonicalSig is allowed")
    fee = transaction["Fee"]
    if not isinstance(fee, str) or not fee.isdecimal() or not 0 < int(fee) <= MAX_FEE_DROPS:
        raise ValidationError("Fee must be an integer drop string within the frozen cap")
    if not _is_plain_int(transaction["Sequence"]) or transaction["Sequence"] <= 0:
        raise ValidationError("Sequence must be a positive integer")
    if (
        not _is_plain_int(transaction["LastLedgerSequence"])
        or transaction["LastLedgerSequence"] <= 0
    ):
        # Construction separately binds this value to the validated ledger plus four.
        raise ValidationError("LastLedgerSequence must be a positive integer")
    decode_memo(transaction)
    signing_key = transaction["SigningPubKey"]
    if signed:
        signature = transaction.get("TxnSignature")
        if not isinstance(signing_key, str) or not signing_key:
            raise ValidationError("signed transaction omitted SigningPubKey")
        if not isinstance(signature, str) or not signature:
            raise ValidationError("signed transaction omitted TxnSignature")
    elif signing_key != "":
        raise ValidationError("unsigned transaction must have an empty SigningPubKey")


def transaction_identity(transaction: dict[str, Any]) -> str:
    """Identify the complete unsigned transaction, including fee, sequence and expiry."""

    unsigned = dict(transaction)
    unsigned.pop("TxnSignature", None)
    unsigned["SigningPubKey"] = ""
    encoded = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest().upper()


def _validate_construction_binding(
    artifact: dict[str, Any], transaction: dict[str, Any]
) -> tuple[int, str]:
    network = artifact.get("network")
    construction = artifact.get("construction")
    identity = artifact.get("transaction_identity")
    if not isinstance(network, dict) or not isinstance(construction, dict):
        raise ValidationError("artifact omitted its recorded construction context")
    if network.get("network_id") != EXPECTED_NETWORK_ID:
        raise NetworkRefused("artifact is not bound to native XRPL Testnet ID 1")
    ledger_index = construction.get("validated_ledger_index")
    ledger_hash = construction.get("validated_ledger_hash")
    if not _is_plain_int(ledger_index) or ledger_index <= 0:
        raise ValidationError("construction context has an invalid validated-ledger index")
    if not isinstance(ledger_hash, str) or len(ledger_hash) != 64:
        raise ValidationError("construction context has an invalid validated-ledger hash")
    if (
        network.get("validated_ledger_index") != ledger_index
        or network.get("validated_ledger_hash") != ledger_hash
    ):
        raise ValidationError("network and construction ledger identities disagree")
    if transaction.get("LastLedgerSequence") != ledger_index + LAST_LEDGER_OFFSET:
        raise ValidationError(
            "LastLedgerSequence must equal the recorded validated-ledger index plus exactly 4"
        )
    computed_identity = transaction_identity(transaction)
    if identity != computed_identity:
        raise ValidationError("transaction does not match its recorded transaction identity")
    return ledger_index, computed_identity


def construct_unsigned(
    authorization: ExecutionAuthorization,
    *,
    endpoint: str = PRIMARY_RPC_ENDPOINT,
) -> dict[str, Any]:
    authorization.require("construct")
    revalidate_authorization(authorization, operation="construct")
    network = verify_network(endpoint)
    ledger_index = network["validated_ledger_index"]
    source = _validated_account(endpoint, authorization.source, ledger_index)
    destination = _validated_account(endpoint, authorization.destination, ledger_index)
    for label, account in (("source", source), ("destination", destination)):
        flags = account.get("Flags")
        if not _is_plain_int(flags):
            raise ValidationError(f"{label} Flags must be an integer")
        if account.get("OwnerCount") != 0:
            raise ValidationError(f"{label} must be a dedicated faucet account with OwnerCount 0")
    if source["Flags"] & _LSF_DISABLE_MASTER:
        raise ValidationError("source master key is disabled")
    if destination["Flags"] & (_LSF_REQUIRE_DEST_TAG | _LSF_DEPOSIT_AUTH):
        raise ValidationError("destination requires unsupported tag or deposit authorization")
    sequence = source.get("Sequence")
    balance = source.get("Balance")
    if not _is_plain_int(sequence) or sequence <= 0:
        raise ValidationError("source Sequence is invalid")
    if not isinstance(balance, str) or not balance.isdecimal():
        raise ValidationError("source Balance is invalid")
    fee = _fee_drops(endpoint)
    reserve = _drops_from_xrp(network["reserve_base_xrp"], "reserve_base_xrp")
    if int(balance) - int(AMOUNT_DROPS) - fee < reserve:
        raise ValidationError(
            "source balance cannot preserve the base reserve after payment and fee"
        )
    payment = Payment(
        account=authorization.source,
        amount=AMOUNT_DROPS,
        destination=authorization.destination,
        fee=str(fee),
        flags=TF_FULLY_CANONICAL_SIG,
        last_ledger_sequence=ledger_index + LAST_LEDGER_OFFSET,
        memos=[
            Memo(
                memo_data=REQUIRED_LABEL.encode("utf-8").hex().upper(),
                memo_type=MEMO_TYPE.encode("utf-8").hex().upper(),
            )
        ],
        sequence=sequence,
    )
    transaction = payment.to_xrpl()
    validate_transaction_structure(
        transaction,
        source=authorization.source,
        destination=authorization.destination,
        signed=False,
    )
    identity = transaction_identity(transaction)
    if (
        authorization.transaction_identity is not None
        and authorization.transaction_identity != identity
    ):
        raise AuthorizationError("construction does not match the authorized transaction identity")
    return {
        "schema": UNSIGNED_SCHEMA,
        "authorization_id": authorization.authorization_id,
        "network": network,
        "construction": {
            "validated_ledger_index": ledger_index,
            "validated_ledger_hash": network["validated_ledger_hash"],
            "fee_rule": "exact open_ledger_fee at construction; no bump; cap 1000 drops",
            "sequence_rule": "exact source Sequence from the pinned validated ledger",
            "last_ledger_rule": "pinned validated ledger index plus 4",
        },
        "transaction_identity": identity,
        "transaction": transaction,
    }


def _read_artifact(path: Path, schema: str) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError("artifact is unavailable or invalid JSON") from exc
    if not isinstance(data, dict) or data.get("schema") != schema:
        raise ValidationError("artifact has the wrong schema")
    return data


def write_new_json(path: Path, data: dict[str, Any]) -> None:
    """Create a mode-0600 artifact without overwriting or following a final symlink."""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ValidationError(f"refusing to overwrite artifact: {path}") from exc
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def sign_unsigned(
    authorization: ExecutionAuthorization,
    unsigned_artifact: dict[str, Any],
    *,
    credential: str | None,
    capability: ExecutionCapability | None = None,
    execution_intent: bool = False,
) -> dict[str, Any]:
    artifact_identity = unsigned_artifact.get("transaction_identity")
    _require_execution_capability(
        authorization,
        capability,
        operation="sign",
        execution_intent=execution_intent,
        transaction_identity=artifact_identity if isinstance(artifact_identity, str) else None,
    )
    if unsigned_artifact.get("schema") != UNSIGNED_SCHEMA:
        raise ValidationError("unsigned artifact has the wrong schema")
    if unsigned_artifact.get("authorization_id") != authorization.authorization_id:
        raise AuthorizationError("unsigned artifact belongs to another execution authorization")
    transaction = unsigned_artifact.get("transaction")
    if not isinstance(transaction, dict):
        raise ValidationError("unsigned artifact omitted transaction")
    validate_transaction_structure(
        transaction,
        source=authorization.source,
        destination=authorization.destination,
        signed=False,
    )
    _construction_ledger, computed_identity = _validate_construction_binding(
        unsigned_artifact, transaction
    )
    if authorization.transaction_identity != computed_identity:
        raise AuthorizationError("authorization does not identify the unsigned transaction")
    if credential is None or not credential.strip():
        raise CredentialError("signing credential is required through the private prompt")
    try:
        wallet = Wallet.from_seed(credential.strip())
    except Exception as exc:
        raise CredentialError("signing credential is invalid") from exc
    if wallet.address != authorization.source:
        raise CredentialError("signing credential does not control the authorized source")
    _require_execution_capability(
        authorization,
        capability,
        operation="sign",
        execution_intent=execution_intent,
        transaction_identity=computed_identity,
    )
    try:
        signed = sign(Transaction.from_xrpl(transaction), wallet)
    except Exception as exc:
        raise CredentialError("local signing failed") from exc
    signed_transaction = signed.to_xrpl()
    validate_transaction_structure(
        signed_transaction,
        source=authorization.source,
        destination=authorization.destination,
        signed=True,
    )
    for field_name, value in transaction.items():
        if field_name != "SigningPubKey" and signed_transaction.get(field_name) != value:
            raise ValidationError(f"signing unexpectedly changed {field_name}")
    return {
        "schema": SIGNED_SCHEMA,
        "authorization_id": authorization.authorization_id,
        "network": unsigned_artifact["network"],
        "construction": unsigned_artifact["construction"],
        "transaction_identity": computed_identity,
        "transaction_hash": signed.get_hash(),
        "transaction_blob": signed.blob(),
        "transaction": signed_transaction,
    }


class OperationJournal:
    """Durable one-shot claims for faucet calls and transaction submission."""

    def __init__(self, path: Path):
        self.path = require_runtime_private_path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.connection = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        os.chmod(self.path, 0o600)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS operation_claims (
                authorization_id TEXT NOT NULL,
                operation TEXT NOT NULL,
                target TEXT NOT NULL,
                status TEXT NOT NULL,
                detail TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (authorization_id, operation, target)
            )
            """
        )
        self.connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS one_submission_per_authorization "
            "ON operation_claims(authorization_id) WHERE operation = 'submit'"
        )
        self.connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS one_submission_per_transaction_hash "
            "ON operation_claims(target) WHERE operation = 'submit'"
        )

    def close(self) -> None:
        self.connection.close()

    def claim(self, authorization_id: str, operation: str, target: str) -> None:
        now = datetime.now(UTC).isoformat()
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute(
                "INSERT INTO operation_claims VALUES (?, ?, ?, ?, ?, ?, ?)",
                (authorization_id, operation, target, "claimed", None, now, now),
            )
            self.connection.execute("COMMIT")
        except sqlite3.IntegrityError as exc:
            self.connection.execute("ROLLBACK")
            raise ReplayRefused(
                f"{operation} target or authorization is already claimed durably"
            ) from exc
        except Exception:
            self.connection.execute("ROLLBACK")
            raise

    def update(
        self,
        authorization_id: str,
        operation: str,
        target: str,
        status: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        encoded = None if detail is None else json.dumps(detail, sort_keys=True)
        cursor = self.connection.execute(
            """
            UPDATE operation_claims SET status = ?, detail = ?, updated_at = ?
            WHERE authorization_id = ? AND operation = ? AND target = ?
            """,
            (status, encoded, now, authorization_id, operation, target),
        )
        if cursor.rowcount != 1:
            raise ReplayRefused("operation was not durably claimed")


def _faucet_request(address: str) -> None:
    _post_json(
        OFFICIAL_FAUCET_ENDPOINT,
        {
            "destination": address,
            "usageContext": "Credence T1 public XRPL Testnet connectivity proof",
            "userAgent": "credence-t1-proof/1",
        },
        timeout=40.0,
    )


def setup_accounts(
    authorization: ExecutionAuthorization,
    journal: OperationJournal,
    *,
    capability: ExecutionCapability | None = None,
    execution_intent: bool = False,
    endpoint: str = PRIMARY_RPC_ENDPOINT,
    poll_seconds: float = 2.0,
    timeout_seconds: float = 60.0,
) -> dict[str, str]:
    _require_execution_capability(
        authorization,
        capability,
        operation="faucet",
        execution_intent=execution_intent,
    )
    verify_network(endpoint)
    statuses: dict[str, str] = {}
    for role, address in (
        ("source", authorization.source),
        ("destination", authorization.destination),
    ):
        if not _account_absent(endpoint, address):
            statuses[role] = "already_funded"
            continue
        _require_execution_capability(
            authorization,
            capability,
            operation="faucet",
            execution_intent=execution_intent,
        )
        journal.claim(authorization.authorization_id, "faucet", address)
        _require_execution_capability(
            authorization,
            capability,
            operation="faucet",
            execution_intent=execution_intent,
        )
        try:
            _faucet_request(address)
        except T1Error:
            journal.update(
                authorization.authorization_id,
                "faucet",
                address,
                "unknown_after_request",
            )
            raise
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if not _account_absent(endpoint, address):
                journal.update(
                    authorization.authorization_id,
                    "faucet",
                    address,
                    "validated",
                )
                statuses[role] = "validated"
                break
            time.sleep(poll_seconds)
        else:
            journal.update(
                authorization.authorization_id,
                "faucet",
                address,
                "unknown_after_timeout",
            )
            raise RpcError("faucet result was not validated before timeout; do not retry")
    return statuses


def _signed_artifact_checks(
    authorization: ExecutionAuthorization, artifact: dict[str, Any]
) -> tuple[dict[str, Any], str, str]:
    if artifact.get("schema") != SIGNED_SCHEMA:
        raise ValidationError("signed artifact has the wrong schema")
    if artifact.get("authorization_id") != authorization.authorization_id:
        raise AuthorizationError("signed artifact belongs to another execution authorization")
    transaction = artifact.get("transaction")
    blob = artifact.get("transaction_blob")
    expected_hash = artifact.get("transaction_hash")
    if not isinstance(transaction, dict) or not isinstance(blob, str):
        raise ValidationError("signed artifact is incomplete")
    if not isinstance(expected_hash, str) or len(expected_hash) != 64:
        raise ValidationError("signed artifact hash is invalid")
    validate_transaction_structure(
        transaction,
        source=authorization.source,
        destination=authorization.destination,
        signed=True,
    )
    _construction_ledger, computed_identity = _validate_construction_binding(artifact, transaction)
    if authorization.transaction_identity != computed_identity:
        raise AuthorizationError("authorization does not identify the signed transaction")
    try:
        decoded = decode(blob)
        model = Transaction.from_xrpl(transaction)
    except Exception as exc:
        raise ValidationError("signed transaction cannot be decoded") from exc
    if decoded != transaction or model.blob() != blob or model.get_hash() != expected_hash:
        raise ValidationError("signed blob, decoded transaction and hash do not agree")
    return transaction, blob, expected_hash


def _submission_preflight(
    endpoint: str,
    transaction: dict[str, Any],
    construction_ledger_index: int,
    construction_ledger_hash: str,
) -> None:
    network = verify_network(endpoint)
    current_ledger_index = network["validated_ledger_index"]
    if construction_ledger_index > current_ledger_index:
        raise ValidationError("recorded construction ledger is ahead of validated node state")
    observed_construction_hash = _ledger_hash(endpoint, construction_ledger_index)
    if observed_construction_hash != construction_ledger_hash:
        raise ValidationError("recorded construction ledger hash does not match validated history")
    if (
        construction_ledger_index == current_ledger_index
        and observed_construction_hash != network["validated_ledger_hash"]
    ):
        raise ValidationError("current validated-ledger identity is internally inconsistent")
    if network["validated_ledger_index"] > transaction["LastLedgerSequence"]:
        raise ValidationError("signed transaction has already expired")
    source = _validated_account(endpoint, transaction["Account"], network["validated_ledger_index"])
    if source.get("Sequence") != transaction["Sequence"]:
        raise ValidationError("source Sequence changed after construction; refusing submission")
    if source.get("OwnerCount") != 0 or source.get("Flags", 0) & _LSF_DISABLE_MASTER:
        raise ValidationError("source account settings changed after construction")
    destination = _validated_account(
        endpoint, transaction["Destination"], network["validated_ledger_index"]
    )
    if destination.get("OwnerCount") != 0 or destination.get("Flags", 0) & (
        _LSF_REQUIRE_DEST_TAG | _LSF_DEPOSIT_AUTH
    ):
        raise ValidationError("destination settings changed after construction")
    current_fee = _fee_drops(endpoint)
    if int(transaction["Fee"]) < current_fee:
        raise ValidationError(
            "signed Fee is below the current open-ledger fee; refusing without bump"
        )


def submit_once(
    authorization: ExecutionAuthorization,
    signed_artifact: dict[str, Any],
    journal: OperationJournal,
    *,
    capability: ExecutionCapability | None = None,
    execution_intent: bool = False,
    endpoint: str = PRIMARY_RPC_ENDPOINT,
) -> dict[str, Any]:
    artifact_identity = signed_artifact.get("transaction_identity")
    _require_execution_capability(
        authorization,
        capability,
        operation="submit",
        execution_intent=execution_intent,
        transaction_identity=artifact_identity if isinstance(artifact_identity, str) else None,
    )
    transaction, blob, transaction_hash = _signed_artifact_checks(authorization, signed_artifact)
    construction_ledger, computed_identity = _validate_construction_binding(
        signed_artifact, transaction
    )
    _submission_preflight(
        endpoint,
        transaction,
        construction_ledger,
        signed_artifact["construction"]["validated_ledger_hash"],
    )
    _require_execution_capability(
        authorization,
        capability,
        operation="submit",
        execution_intent=execution_intent,
        transaction_identity=computed_identity,
    )
    journal.claim(authorization.authorization_id, "submit", transaction_hash)
    _require_execution_capability(
        authorization,
        capability,
        operation="submit",
        execution_intent=execution_intent,
        transaction_identity=computed_identity,
    )
    try:
        response = rpc_raw(endpoint, "submit", {"tx_blob": blob, "fail_hard": True})
    except T1Error:
        journal.update(
            authorization.authorization_id,
            "submit",
            transaction_hash,
            "unknown_after_request",
        )
        raise
    result = response.get("result")
    if not isinstance(result, dict):
        journal.update(
            authorization.authorization_id,
            "submit",
            transaction_hash,
            "unexpected_result",
        )
        raise RpcError("submit returned no result; do not retry")
    engine_result = result.get("engine_result")
    sanitized = {
        "accepted": result.get("accepted"),
        "applied": result.get("applied"),
        "broadcast": result.get("broadcast"),
        "engine_result": engine_result,
        "kept": result.get("kept"),
        "queued": result.get("queued"),
        "validated_ledger_index": result.get("validated_ledger_index"),
    }
    if engine_result not in {"tesSUCCESS", "terQUEUED"}:
        journal.update(
            authorization.authorization_id,
            "submit",
            transaction_hash,
            "unexpected_result",
            sanitized,
        )
        raise RpcError(f"unexpected provisional submit result {engine_result!r}; do not retry")
    journal.update(
        authorization.authorization_id,
        "submit",
        transaction_hash,
        "submitted_provisional",
        sanitized,
    )
    return sanitized


def _tx_projection(result: dict[str, Any], expected_transaction: dict[str, Any]) -> dict[str, Any]:
    if result.get("validated") is not True:
        raise ValidationError("transaction result is not validated")
    metadata = result.get("meta")
    if not isinstance(metadata, dict) or metadata.get("TransactionResult") != "tesSUCCESS":
        raise ValidationError("validated transaction result is not tesSUCCESS")
    projection = {
        "transaction_hash": result.get("hash"),
        "ledger_index": result.get("ledger_index", result.get("inLedger")),
        "result": metadata.get("TransactionResult"),
        "source": result.get("Account"),
        "destination": result.get("Destination"),
        "amount_drops": result.get("Amount"),
        "fee_drops": result.get("Fee"),
        "sequence": result.get("Sequence"),
        "last_ledger_sequence": result.get("LastLedgerSequence"),
        "flags": result.get("Flags"),
        "memos": result.get("Memos"),
    }
    for result_field, tx_field in (
        ("source", "Account"),
        ("destination", "Destination"),
        ("amount_drops", "Amount"),
        ("fee_drops", "Fee"),
        ("sequence", "Sequence"),
        ("last_ledger_sequence", "LastLedgerSequence"),
        ("flags", "Flags"),
        ("memos", "Memos"),
    ):
        if projection[result_field] != expected_transaction[tx_field]:
            raise ValidationError(f"validated transaction changed {tx_field}")
    if not _is_plain_int(projection["ledger_index"]) or projection["ledger_index"] <= 0:
        raise ValidationError("validated transaction omitted ledger index")
    decode_memo(result)
    return projection


def poll_validated(
    authorization: ExecutionAuthorization,
    signed_artifact: dict[str, Any],
    journal: OperationJournal,
    *,
    endpoint: str = PRIMARY_RPC_ENDPOINT,
    poll_seconds: float = 4.0,
    timeout_seconds: float = 90.0,
) -> dict[str, Any]:
    authorization.require("poll")
    revalidate_authorization(authorization, operation="poll")
    transaction, _blob, transaction_hash = _signed_artifact_checks(authorization, signed_artifact)
    construction_ledger, _identity = _validate_construction_binding(signed_artifact, transaction)
    construction_hash = signed_artifact["construction"]["validated_ledger_hash"]
    first_possible_ledger = construction_ledger + 1
    last_possible_ledger = transaction["LastLedgerSequence"]
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        revalidate_authorization(authorization, operation="poll")
        before = verify_network(endpoint)
        if before["validated_ledger_index"] < construction_ledger:
            journal.update(
                authorization.authorization_id,
                "submit",
                transaction_hash,
                "UNKNOWN_CONTRADICTORY_NODE",
            )
            raise RpcError("validated ledger predates the recorded construction context")
        try:
            if _ledger_hash(endpoint, construction_ledger) != construction_hash:
                raise ValidationError("construction ledger hash changed")
        except T1Error as exc:
            journal.update(
                authorization.authorization_id,
                "submit",
                transaction_hash,
                "UNKNOWN_CONTRADICTORY_NODE",
            )
            raise RpcError("poll observations are contradictory; retain UNKNOWN") from exc
        response = rpc_raw(
            endpoint,
            "tx",
            {
                "transaction": transaction_hash,
                "binary": False,
                "min_ledger": first_possible_ledger,
                "max_ledger": last_possible_ledger,
            },
        )
        after = verify_network(endpoint)
        try:
            _validate_node_progression(endpoint, before, after)
        except T1Error as exc:
            journal.update(
                authorization.authorization_id,
                "submit",
                transaction_hash,
                "UNKNOWN_CONTRADICTORY_NODE",
            )
            raise RpcError("poll observations are contradictory; retain UNKNOWN") from exc
        result = response.get("result")
        if isinstance(result, dict) and result.get("status") == "success":
            try:
                projection = _tx_projection(result, transaction)
                if projection["transaction_hash"] != transaction_hash:
                    raise ValidationError("tx lookup returned a different transaction hash")
                if not first_possible_ledger <= projection["ledger_index"] <= last_possible_ledger:
                    raise ValidationError(
                        "transaction was returned outside its possible ledger range"
                    )
                included_hash = _ledger_hash(endpoint, projection["ledger_index"])
                if (
                    projection["ledger_index"] == after["validated_ledger_index"]
                    and included_hash != after["validated_ledger_hash"]
                ):
                    raise ValidationError(
                        "validated transaction ledger conflicts with the current ledger identity"
                    )
                if (
                    projection["ledger_index"] == before["validated_ledger_index"]
                    and included_hash != before["validated_ledger_hash"]
                ):
                    raise ValidationError(
                        "validated transaction ledger conflicts with the prior ledger identity"
                    )
            except T1Error:
                journal.update(
                    authorization.authorization_id,
                    "submit",
                    transaction_hash,
                    "unexpected_validated_result",
                )
                raise
            journal.update(
                authorization.authorization_id,
                "submit",
                transaction_hash,
                "validated",
                projection,
            )
            return projection
        if not isinstance(result, dict) or result.get("error") != "txnNotFound":
            journal.update(
                authorization.authorization_id,
                "submit",
                transaction_hash,
                "unexpected_poll_result",
            )
            raise RpcError("tx lookup returned an unexpected result; stop")
        latest = after["validated_ledger_index"]
        if latest > last_possible_ledger:
            if result.get("searched_all") is not True:
                journal.update(
                    authorization.authorization_id,
                    "submit",
                    transaction_hash,
                    "UNKNOWN_INCOMPLETE_SEARCH",
                    {"searched_all": result.get("searched_all")},
                )
                raise RpcError(
                    "bounded transaction lookup did not report literal searched_all true"
                )
            before_history_complete = history_covers(
                before["complete_ledgers"], first_possible_ledger, last_possible_ledger
            )
            after_history_complete = history_covers(
                after["complete_ledgers"], first_possible_ledger, last_possible_ledger
            )
            if not before_history_complete or not after_history_complete:
                journal.update(
                    authorization.authorization_id,
                    "submit",
                    transaction_hash,
                    "UNKNOWN_HISTORY_GAP",
                    {
                        "before_complete_ledgers": before["complete_ledgers"],
                        "after_complete_ledgers": after["complete_ledgers"],
                    },
                )
                raise RpcError("bounded transaction history is incomplete; retain UNKNOWN")
            before_index = before["validated_ledger_index"]
            if before_index <= last_possible_ledger:
                journal.update(
                    authorization.authorization_id,
                    "submit",
                    transaction_hash,
                    "UNKNOWN_PREQUERY_LEDGER_NOT_EXPIRED",
                    {
                        "before_validated_ledger": before_index,
                        "after_validated_ledger": latest,
                        "last_possible_ledger": last_possible_ledger,
                    },
                )
                raise RpcError("pre-query validated ledger had not passed expiry; retain UNKNOWN")
            journal.update(
                authorization.authorization_id,
                "submit",
                transaction_hash,
                "EXPIRED_NOT_INCLUDED",
                {
                    "first_possible_ledger": first_possible_ledger,
                    "last_possible_ledger": last_possible_ledger,
                    "latest_validated_ledger": latest,
                    "searched_all": True,
                },
            )
            raise RpcError("transaction conclusively expired without inclusion; do not resubmit")
        time.sleep(poll_seconds)
    journal.update(
        authorization.authorization_id,
        "submit",
        transaction_hash,
        "poll_timeout_unknown",
    )
    raise RpcError("polling timed out with unknown finality; do not resubmit")


def _ledger_hash(endpoint: str, ledger_index: int) -> str:
    result = rpc_result(endpoint, "ledger", {"ledger_index": ledger_index, "transactions": False})
    if result.get("validated") is not True:
        raise ValidationError("independent ledger lookup is not validated")
    observed_index = result.get("ledger_index")
    if observed_index is not None and observed_index != ledger_index:
        raise ValidationError("ledger lookup returned a contradictory ledger index")
    ledger_hash = result.get("ledger_hash")
    if not isinstance(ledger_hash, str) or len(ledger_hash) != 64:
        raise ValidationError("independent ledger lookup omitted ledger hash")
    return ledger_hash.upper()


def _validate_node_progression(
    endpoint: str,
    before: dict[str, Any],
    after: dict[str, Any],
) -> None:
    if (
        before.get("network_id") != EXPECTED_NETWORK_ID
        or after.get("network_id") != EXPECTED_NETWORK_ID
    ):
        raise NetworkRefused("poll observations are not both native XRPL Testnet ID 1")
    if before.get("endpoint") != endpoint or after.get("endpoint") != endpoint:
        raise NetworkRefused("poll observations came from an unexpected endpoint")
    before_index = before.get("validated_ledger_index")
    after_index = after.get("validated_ledger_index")
    if not _is_plain_int(before_index) or not _is_plain_int(after_index):
        raise ValidationError("poll observations omitted validated-ledger indices")
    if after_index < before_index:
        raise ValidationError("validated-ledger index moved backwards between poll observations")
    if after_index == before_index:
        if after.get("validated_ledger_hash") != before.get("validated_ledger_hash"):
            raise ValidationError("same validated-ledger index returned contradictory hashes")
        return
    if _ledger_hash(endpoint, before_index) != before.get("validated_ledger_hash"):
        raise ValidationError(
            "advanced node no longer agrees with the prior validated-ledger identity"
        )


def _response_digest(response: dict[str, Any]) -> str:
    encoded = json.dumps(response, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def capture_independent_evidence(
    authorization: ExecutionAuthorization,
    signed_artifact: dict[str, Any],
) -> dict[str, Any]:
    authorization.require("capture")
    revalidate_authorization(authorization, operation="capture")
    transaction, _blob, transaction_hash = _signed_artifact_checks(authorization, signed_artifact)
    observations: dict[str, dict[str, Any]] = {}
    for name, endpoint in (
        ("primary", PRIMARY_RPC_ENDPOINT),
        ("secondary", SECONDARY_RPC_ENDPOINT),
    ):
        revalidate_authorization(authorization, operation="capture")
        network = verify_network(endpoint)
        response = rpc_raw(
            endpoint,
            "tx",
            {"transaction": transaction_hash, "binary": False},
        )
        result = response.get("result")
        if not isinstance(result, dict) or result.get("status") != "success":
            raise RpcError(f"{name} independent tx lookup failed")
        projection = _tx_projection(result, transaction)
        if projection["transaction_hash"] != transaction_hash:
            raise ValidationError(f"{name} returned a different transaction hash")
        projection["ledger_hash"] = _ledger_hash(endpoint, projection["ledger_index"])
        observations[name] = {
            "endpoint": endpoint,
            "network_id": network["network_id"],
            "projection": projection,
            "response_sha256": _response_digest(response),
        }
    if observations["primary"]["projection"] != observations["secondary"]["projection"]:
        raise ValidationError("independent endpoints disagree on validated transaction evidence")
    projection = observations["primary"]["projection"]
    memo_data, memo_type = decode_memo(transaction)
    return {
        "schema": EVIDENCE_SCHEMA,
        "captured_at": datetime.now(UTC).isoformat(),
        "claim_boundary": REQUIRED_LABEL,
        "network": {"name": "Public native XRPL Testnet", "network_id": 1},
        "transaction": {
            "hash": transaction_hash,
            "ledger_index": projection["ledger_index"],
            "ledger_hash": projection["ledger_hash"],
            "result": projection["result"],
            "amount_drops": projection["amount_drops"],
            "fee_drops": projection["fee_drops"],
            "source": projection["source"],
            "destination": projection["destination"],
            "sequence": projection["sequence"],
            "last_ledger_sequence": projection["last_ledger_sequence"],
            "memo": {"type": memo_type, "data": memo_data},
        },
        "independent_verification": observations,
    }


def load_unsigned(path: Path) -> dict[str, Any]:
    return _read_artifact(path, UNSIGNED_SCHEMA)


def load_signed(path: Path) -> dict[str, Any]:
    return _read_artifact(path, SIGNED_SCHEMA)
