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
