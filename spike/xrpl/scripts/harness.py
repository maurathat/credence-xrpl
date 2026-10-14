"""Fail-closed result handling and public-evidence helpers for XRPL spikes."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

VALID_EXPERIMENT_STATUSES = {
    "PASS",
    "BLOCKED",
    "NOT_ATTEMPTED",
    "NOT_APPLICABLE",
}


def experiment_status_summary(statuses: list[str]) -> dict[str, int]:
    """Count exact status values without normalizing unrun experiments."""

    invalid = [status for status in statuses if status not in VALID_EXPERIMENT_STATUSES]
    if invalid:
        raise ValueError(f"invalid experiment statuses: {invalid}")
    return {status: statuses.count(status) for status in sorted(VALID_EXPERIMENT_STATUSES)}


@dataclass(frozen=True)
class StateAssertion:
    """One explicit comparison between expected and observed ledger state."""

    name: str
    passed: bool
    expected: Any
    actual: Any


def state_assertion(name: str, actual: Any, expected: Any) -> StateAssertion:
    return StateAssertion(name=name, passed=actual == expected, expected=expected, actual=actual)


def predicate_assertion(name: str, passed: bool, expected: Any, actual: Any) -> StateAssertion:
    return StateAssertion(name=name, passed=passed, expected=expected, actual=actual)


def semantic_objects(
    objects: list[dict[str, Any]],
    *,
    ledger_entry_type: str,
    index: str | None = None,
    identity: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Find ledger objects by stable semantics, independent of list order."""

    identity = identity or {}
    return [
        item
        for item in objects
        if item.get("LedgerEntryType") == ledger_entry_type
        and (index is None or item.get("index") == index)
        and all(item.get(field_name) == expected for field_name, expected in identity.items())
    ]


def _object_assertions(
    *,
    label: str,
    node: dict[str, Any] | None,
    ledger_entry_type: str,
    index: str | None,
    expected_fields: dict[str, Any],
) -> list[StateAssertion]:
    checks = [
        state_assertion(
            f"{label} LedgerEntryType",
            None if node is None else node.get("LedgerEntryType"),
            ledger_entry_type,
        )
    ]
    if index is not None:
        checks.append(
            state_assertion(
                f"{label} index",
                None if node is None else node.get("index"),
                index,
            )
        )
    checks.extend(
        state_assertion(
            f"{label} {field_name}",
            None if node is None else node.get(field_name),
            expected,
        )
        for field_name, expected in expected_fields.items()
    )
    return checks


def vault_assertions(
    node: dict[str, Any] | None,
    *,
    index: str | None,
    owner: str,
    asset: dict[str, str],
    vault_kind: int,
    subscription_date: int,
    redemption_date: int,
    withdrawal_policy: int,
    flags: int | None = None,
) -> list[StateAssertion]:
    """Assert the stable identity and closed-ended configuration of a Vault."""

    fields: dict[str, Any] = {
        "Owner": owner,
        "Asset": asset,
        "VaultKind": vault_kind,
        "SubscriptionDate": subscription_date,
        "RedemptionDate": redemption_date,
        "WithdrawalPolicy": withdrawal_policy,
    }
    if flags is not None:
        fields["Flags"] = flags
    checks = _object_assertions(
        label="Vault",
        node=node,
        ledger_entry_type="Vault",
        index=index,
        expected_fields=fields,
    )
    checks.extend(
        [
            predicate_assertion(
                "Vault pseudo Account present",
                isinstance((node or {}).get("Account"), str) and bool((node or {}).get("Account")),
                True,
                (node or {}).get("Account"),
            ),
            predicate_assertion(
                "Vault ShareMPTID present",
                isinstance((node or {}).get("ShareMPTID"), str)
                and bool((node or {}).get("ShareMPTID")),
                True,
                (node or {}).get("ShareMPTID"),
            ),
        ]
    )
    return checks


def mptoken_assertions(
    node: dict[str, Any] | None,
    *,
    index: str | None,
    account: str,
    issuance_id: str,
    amount: str | None = None,
    flags: int | None = None,
) -> list[StateAssertion]:
    """Assert an MPToken by holder and issuance relationship."""

    fields: dict[str, Any] = {"Account": account, "MPTokenIssuanceID": issuance_id}
    if amount is not None:
        fields["MPTAmount"] = amount
    if flags is not None:
        fields["Flags"] = flags
    return _object_assertions(
        label="MPToken",
        node=node,
        ledger_entry_type="MPToken",
        index=index,
        expected_fields=fields,
    )


def ripple_state_assertions(
    node: dict[str, Any] | None,
    *,
    index: str | None,
    low_limit: dict[str, Any],
    high_limit: dict[str, Any],
    balance: dict[str, Any],
) -> list[StateAssertion]:
    """Assert a RippleState by both endpoints, asset, and balance."""

    return _object_assertions(
        label="RippleState",
        node=node,
        ledger_entry_type="RippleState",
        index=index,
        expected_fields={"LowLimit": low_limit, "HighLimit": high_limit, "Balance": balance},
    )


def account_root_assertions(
    node: dict[str, Any] | None,
    *,
    index: str | None,
    account: str,
    required_flags: int | None = None,
) -> list[StateAssertion]:
    """Assert AccountRoot identity and, when required, exact flags."""

    fields: dict[str, Any] = {"Account": account}
    if required_flags is not None:
        fields["Flags"] = required_flags
    return _object_assertions(
        label="AccountRoot",
        node=node,
        ledger_entry_type="AccountRoot",
        index=index,
        expected_fields=fields,
    )


def loan_broker_assertions(
    node: dict[str, Any] | None,
    *,
    index: str | None,
    owner: str,
    vault_id: str,
    debt_maximum: str,
    cover_rate_minimum: int,
    cover_rate_liquidation: int,
    cover_available: str = "0",
    debt_total: str = "0",
    management_fee_rate: int = 0,
) -> list[StateAssertion]:
    """Assert LoanBroker identity, Vault relationship, limits, rates, and cover."""

    checks = _object_assertions(
        label="LoanBroker",
        node=node,
        ledger_entry_type="LoanBroker",
        index=index,
        expected_fields={
            "Owner": owner,
            "VaultID": vault_id,
            "DebtMaximum": debt_maximum,
            "CoverRateMinimum": cover_rate_minimum,
            "CoverRateLiquidation": cover_rate_liquidation,
        },
    )
    checks.extend(
        [
            state_assertion(
                "LoanBroker DebtTotal",
                None if node is None else node.get("DebtTotal", "0"),
                debt_total,
            ),
            state_assertion(
                "LoanBroker CoverAvailable",
                None if node is None else node.get("CoverAvailable", "0"),
                cover_available,
            ),
            state_assertion(
                "LoanBroker ManagementFeeRate",
                None if node is None else node.get("ManagementFeeRate", 0),
                management_fee_rate,
            ),
            predicate_assertion(
                "LoanBroker pseudo Account present",
                isinstance((node or {}).get("Account"), str) and bool((node or {}).get("Account")),
                True,
                (node or {}).get("Account"),
            ),
        ]
    )
    return checks


def vault_create_transition_assertions(
    *,
    before_objects: list[dict[str, Any]],
    after_objects: list[dict[str, Any]],
    vault_index: str | None,
    owner: str,
    asset: dict[str, str],
    vault_kind: int,
    subscription_date: int,
    redemption_date: int,
    withdrawal_policy: int,
) -> list[StateAssertion]:
    """Assert VaultCreate semantics without treating total object count as an oracle."""

    vaults = semantic_objects(
        after_objects,
        ledger_entry_type="Vault",
        index=vault_index,
        identity={"Owner": owner, "Asset": asset},
    )
    checks = [
        state_assertion("expected Vault match count", len(vaults), 1),
    ]
    vault = vaults[0] if len(vaults) == 1 else None
    checks.extend(
        vault_assertions(
            vault,
            index=vault_index,
            owner=owner,
            asset=asset,
            vault_kind=vault_kind,
            subscription_date=subscription_date,
            redemption_date=redemption_date,
            withdrawal_policy=withdrawal_policy,
            flags=0,
        )
    )

    share_id = None if vault is None else vault.get("ShareMPTID")
    owner_tokens = (
        semantic_objects(
            after_objects,
            ledger_entry_type="MPToken",
            identity={"Account": owner, "MPTokenIssuanceID": share_id},
        )
        if share_id is not None
        else []
    )
    checks.append(state_assertion("expected owner MPToken match count", len(owner_tokens), 1))
    token = owner_tokens[0] if len(owner_tokens) == 1 else None
    checks.extend(
        mptoken_assertions(
            token,
            index=None,
            account=owner,
            issuance_id="" if share_id is None else str(share_id),
            flags=0,
        )
    )

    if vault_index is not None:
        old_vaults = semantic_objects(
            before_objects,
            ledger_entry_type="Vault",
            index=vault_index,
        )
        checks.append(state_assertion("expected Vault absent from pre-state", len(old_vaults), 0))
    if share_id is not None:
        old_tokens = semantic_objects(
            before_objects,
            ledger_entry_type="MPToken",
            identity={"Account": owner, "MPTokenIssuanceID": share_id},
        )
        checks.append(
            state_assertion("expected owner MPToken absent from pre-state", len(old_tokens), 0)
        )
    return checks


def loan_broker_create_transition_assertions(
    *,
    before_objects: list[dict[str, Any]],
    after_objects: list[dict[str, Any]],
    broker_index: str | None,
    owner: str,
    vault_id: str,
    debt_maximum: str,
    cover_rate_minimum: int,
    cover_rate_liquidation: int,
) -> list[StateAssertion]:
    """Assert creation of the intended LoanBroker while allowing auxiliary objects."""

    brokers = semantic_objects(
        after_objects,
        ledger_entry_type="LoanBroker",
        index=broker_index,
        identity={"Owner": owner, "VaultID": vault_id},
    )
    checks = [state_assertion("expected LoanBroker match count", len(brokers), 1)]
    broker = brokers[0] if len(brokers) == 1 else None
    checks.extend(
        loan_broker_assertions(
            broker,
            index=broker_index,
            owner=owner,
            vault_id=vault_id,
            debt_maximum=debt_maximum,
            cover_rate_minimum=cover_rate_minimum,
            cover_rate_liquidation=cover_rate_liquidation,
        )
    )
    if broker_index is not None:
        old_brokers = semantic_objects(
            before_objects,
            ledger_entry_type="LoanBroker",
            index=broker_index,
        )
        checks.append(
            state_assertion("expected LoanBroker absent from pre-state", len(old_brokers), 0)
        )
    return checks


def cover_transition_assertions(
    *,
    before_broker: dict[str, Any] | None,
    after_broker: dict[str, Any] | None,
    broker_index: str,
    owner: str,
    vault_id: str,
    before_owner_balance: str,
    after_owner_balance: str,
    cover_delta: int,
) -> list[StateAssertion]:
    """Assert cover movement on a known LoanBroker and its owner balance."""

    checks = [
        state_assertion(
            "pre-state LoanBroker identity",
            None if before_broker is None else before_broker.get("index"),
            broker_index,
        ),
        state_assertion(
            "pre-state LoanBroker owner",
            None if before_broker is None else before_broker.get("Owner"),
            owner,
        ),
        state_assertion(
            "pre-state LoanBroker Vault relationship",
            None if before_broker is None else before_broker.get("VaultID"),
            vault_id,
        ),
        state_assertion(
            "LoanBroker identity remains stable",
            None if after_broker is None else after_broker.get("index"),
            broker_index,
        ),
        state_assertion(
            "LoanBroker owner remains stable",
            None if after_broker is None else after_broker.get("Owner"),
            owner,
        ),
        state_assertion(
            "LoanBroker Vault relationship remains stable",
            None if after_broker is None else after_broker.get("VaultID"),
            vault_id,
        ),
        state_assertion(
            "CoverAvailable delta",
            int((after_broker or {}).get("CoverAvailable", "0"))
            - int((before_broker or {}).get("CoverAvailable", "0")),
            cover_delta,
        ),
        state_assertion(
            "cover owner balance delta",
            int(after_owner_balance) - int(before_owner_balance),
            -cover_delta,
        ),
    ]
    return checks


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, StateAssertion):
        return asdict(value)
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(json_safe(value), indent=2, sort_keys=True) + "\n")


def public_transaction(transaction: dict[str, Any]) -> dict[str, Any]:
    """Retain public transaction semantics but remove all signing material."""

    value = dict(transaction)
    signers = value.pop("Signers", [])
    signing_key_present = bool(value.pop("SigningPubKey", None))
    signature_present = bool(value.pop("TxnSignature", None))
    value.pop("CounterpartySignature", None)
    value["authorization"] = {
        "mode": "multisign" if signers else "single_sign",
        "signers": [
            wrapper.get("Signer", {}).get("Account")
            for wrapper in signers
            if wrapper.get("Signer", {}).get("Account")
        ],
        "top_level_signing_public_key_present": signing_key_present,
        "top_level_transaction_signature_present": signature_present,
    }
    return sanitize_response(value)


def sanitize_response(value: Any) -> Any:
    """Recursively remove private/signature/blob material from evidence."""

    forbidden = {
        "SigningPubKey",
        "TxnSignature",
        "CounterpartySignature",
        "tx_blob",
        "public_key",
        "private_key",
        "seed",
    }
    if isinstance(value, dict):
        return {key: sanitize_response(item) for key, item in value.items() if key not in forbidden}
    if isinstance(value, list):
        return [sanitize_response(item) for item in value]
    return value


def capture_exception(call: Callable[[], Any]) -> tuple[Any | None, dict[str, str] | None]:
    try:
        return call(), None
    except Exception as exc:  # noqa: BLE001 - the evidence must retain client failures
        return None, {"type": type(exc).__name__, "message": str(exc)}


def resolve_transaction_result(
    *,
    submission_response: dict[str, Any] | None = None,
    submission_hash_if_any: str | None = None,
    validated_lookup: dict[str, Any] | None = None,
    client_exception: dict[str, str] | None = None,
    malformed_request: dict[str, Any] | None = None,
    transport_or_server_failure: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve a result without allowing a weak lookup to erase submission truth."""

    submission_result = None
    if submission_response is not None:
        raw = submission_response.get("engine_result")
        submission_result = str(raw) if raw not in (None, "") else None
        if submission_hash_if_any is None:
            tx_json = submission_response.get("tx_json", {})
            submission_hash_if_any = tx_json.get("hash")

    validated_lookup_status = "NOT_ATTEMPTED"
    validated_result = None
    if validated_lookup is not None:
        meta = validated_lookup.get("meta")
        if validated_lookup.get("validated") is True and isinstance(meta, dict):
            raw = meta.get("TransactionResult")
            if raw not in (None, ""):
                validated_result = str(raw)
                validated_lookup_status = "VALIDATED"
            else:
                validated_lookup_status = "MALFORMED_VALIDATED_RESPONSE"
        else:
            validated_lookup_status = "NOT_IN_VALIDATED_LEDGER"

    if client_exception is not None:
        category = "CLIENT_SIDE_EXCEPTION"
    elif malformed_request is not None:
        category = "MALFORMED_REQUEST"
    elif transport_or_server_failure is not None:
        category = "TRANSPORT_OR_SERVER_FAILURE"
    elif validated_result is not None:
        category = (
            "ACCEPTED_VALIDATED_SUCCESS"
            if validated_result.startswith("tes")
            else "ACCEPTED_VALIDATED_DETERMINISTIC_FAILURE"
        )
    elif submission_result is not None:
        category = (
            "SUBMITTED_SUCCESS_NOT_VALIDATED"
            if submission_result.startswith("tes")
            else "SUBMISSION_DETERMINISTIC_FAILURE"
        )
    else:
        category = "NO_AUTHORITATIVE_RESULT"

    authoritative_result = validated_result or submission_result
    return {
        "category": category,
        "submission_result": submission_result,
        "submission_hash_if_any": submission_hash_if_any,
        "validated_lookup_status": validated_lookup_status,
        "validated_result_if_any": validated_result,
        "authoritative_result": authoritative_result,
        "client_exception": client_exception,
        "malformed_request": malformed_request,
        "transport_or_server_failure": transport_or_server_failure,
    }


def classify_transaction(
    *,
    expected_result: str,
    resolution: dict[str, Any],
    before_state: dict[str, Any] | None,
    after_state: dict[str, Any] | None,
    assertions: list[StateAssertion],
    state_unchanged: bool | None = None,
) -> dict[str, Any]:
    """Require both the expected result and explicit post-state proof."""

    observed = resolution.get("authoritative_result")
    reasons: list[str] = []
    if observed != expected_result:
        reasons.append(f"expected {expected_result}, observed {observed}")
    if before_state is None or after_state is None:
        reasons.append("before_state and after_state are required")
    if not assertions:
        reasons.append("at least one state assertion is required")
    if any(not item.passed for item in assertions):
        reasons.append("one or more state assertions failed")
    if not expected_result.startswith("tes") and state_unchanged is not True:
        reasons.append("deterministic negative requires unchanged relevant state")
    if (
        expected_result.startswith("tes")
        and resolution.get("validated_result_if_any") != expected_result
    ):
        reasons.append("success requires a matching validated result")
    return {
        "status": "PASS" if not reasons else "FAIL",
        "expected_result": expected_result,
        "observed_result": observed,
        "state_assertion": "PASS"
        if assertions and all(item.passed for item in assertions)
        else "FAIL",
        "state_unchanged": state_unchanged,
        "reasons": reasons,
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_manifest(root: Path, output: Path) -> dict[str, Any]:
    files = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path != output:
            files.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "sha256": sha256_file(path),
                    "bytes": path.stat().st_size,
                }
            )
    manifest = {"algorithm": "SHA-256", "file_count": len(files), "files": files}
    write_json(output, manifest)
    return manifest


def verify_manifest(root: Path, manifest_path: Path) -> list[str]:
    manifest = json.loads(manifest_path.read_text())
    errors = []
    for entry in manifest.get("files", []):
        path = root / entry["path"]
        if not path.is_file():
            errors.append(f"missing: {entry['path']}")
        elif path.stat().st_size != entry["bytes"]:
            errors.append(f"size: {entry['path']}")
        elif sha256_file(path) != entry["sha256"]:
            errors.append(f"sha256: {entry['path']}")
    return errors
