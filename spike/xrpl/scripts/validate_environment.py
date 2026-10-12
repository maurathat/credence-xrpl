#!/usr/bin/env python3
"""Run Checkpoint 1B-R's E1-only standalone environment validation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import httpx
from amendment_validation import (
    amendments_ledger_entry_params,
    cross_check_amendment_representations,
    validate_amendments_ledger_entry,
    validate_feature_response,
)

RPC_URL = os.environ.get("XRPL_RPC_URL", "http://127.0.0.1:5005")
CONTAINER = os.environ.get("XRPL_CONTAINER_NAME", "credence-xrpl-spike-ledger")
EXPECTED_VERSION = "3.4.0"
EXPECTED_COMMIT = "4a4fded2eba11427c48ce3f24d9c1aea5e7a9d17"
EXPECTED_NETWORK_ID = 21_337
SPIKE_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_DIR = SPIKE_ROOT / "evidence" / "checkpoint-1b-r"
OUTPUT_PATH = EVIDENCE_DIR / "e1-validation.json"
MANIFEST_PATH = EVIDENCE_DIR / "MANIFEST.json"
EXPECTED_DATA_PATH = "/var/lib/xrpld"


class EnvironmentValidationError(RuntimeError):
    """E1 observed an environment state that does not match the pin."""


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def rpc_raw(method: str, **params: Any) -> tuple[dict[str, Any], bytes]:
    response = httpx.post(
        RPC_URL,
        json={"method": method, "params": [params]},
        timeout=20,
    )
    response.raise_for_status()
    value = response.json()
    if not isinstance(value, dict):
        raise EnvironmentValidationError(f"{method} did not return an object")
    result = value.get("result")
    if not isinstance(result, dict) or result.get("status") == "error":
        raise EnvironmentValidationError(f"{method} failed: {result}")
    return value, response.content


def run(command: list[str]) -> str:
    completed = subprocess.run(command, check=True, capture_output=True, text=True)
    return completed.stdout.strip()


def server_summary(response: dict[str, Any]) -> dict[str, Any]:
    info = response["result"]["info"]
    return {
        "build_version": info["build_version"],
        "complete_ledgers": info["complete_ledgers"],
        "network_id": info["network_id"],
        "server_state": info["server_state"],
        "validated_ledger": info["validated_ledger"],
    }


def validated_ledger() -> dict[str, Any]:
    response, _ = rpc_raw("ledger", ledger_index="validated", transactions=False, expand=False)
    ledger = response["result"]["ledger"]
    return {
        "close_time": int(ledger["close_time"]),
        "closed": bool(ledger["closed"]),
        "ledger_hash": ledger["ledger_hash"],
        "ledger_index": int(ledger["ledger_index"]),
    }


def capture_rpc(name: str, method: str, **params: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    response, raw = rpc_raw(method, **params)
    path = EVIDENCE_DIR / name
    path.write_bytes(raw)
    return response, {
        "path": f"evidence/checkpoint-1b-r/{name}",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
    }


def capture_server_info(name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Capture relevant raw server state without committing key-shaped values."""
    response, _ = rpc_raw("server_info")
    captured = json.loads(json.dumps(response))
    info = captured["result"]["info"]
    omitted = [field for field in ("pubkey_node", "pubkey_validator") if field in info]
    for field in omitted:
        del info[field]
    captured["evidence_redaction"] = {
        "omitted_fields": omitted,
        "reason": "Repository policy prohibits key-shaped strings; these fields are not required for E1.",
    }
    raw = (json.dumps(captured, sort_keys=True, separators=(",", ":")) + "\n").encode()
    path = EVIDENCE_DIR / name
    path.write_bytes(raw)
    return response, {
        "path": f"evidence/checkpoint-1b-r/{name}",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "omitted_fields": omitted,
    }


def build_provenance() -> dict[str, Any]:
    version_output = run(["docker", "exec", CONTAINER, "/usr/bin/xrpld", "--version"])
    version_match = re.search(r"^xrpld version (\S+)$", version_output, re.MULTILINE)
    commit_match = re.search(r"^Git commit hash: ([0-9a-f]{40})$", version_output, re.MULTILINE)
    if version_match is None or commit_match is None:
        raise EnvironmentValidationError("could not parse xrpld --version")

    package = run(
        [
            "docker",
            "exec",
            CONTAINER,
            "dpkg-query",
            "-W",
            "-f=${Package} ${Version} ${Architecture}",
            "xrpld",
        ]
    )
    binary_digest = run(["docker", "exec", CONTAINER, "sha256sum", "/usr/bin/xrpld"]).split()[0]
    inspect = json.loads(run(["docker", "inspect", CONTAINER]))[0]
    command = inspect["Config"]["Cmd"]
    entrypoint = inspect["Config"]["Entrypoint"]
    mounts = [
        {
            "type": item["Type"],
            "name": item.get("Name"),
            "destination": item["Destination"],
        }
        for item in inspect["Mounts"]
    ]
    data_mount_matches_config = any(
        item["type"] == "volume" and item["destination"] == EXPECTED_DATA_PATH for item in mounts
    )

    observed = {
        "xrpld_version": version_match.group(1),
        "source_commit": commit_match.group(1),
        "package": package,
        "binary_sha256": binary_digest,
        "container_image_id": inspect["Image"],
        "entrypoint": entrypoint,
        "command": command,
        "network_mode": "standalone" if "--standalone" in command else "other",
        "fresh_start_flag": "--start" in command,
        "started_at": inspect["State"]["StartedAt"],
        "mounts": mounts,
        "configured_data_path": EXPECTED_DATA_PATH,
        "data_mount_matches_config": data_mount_matches_config,
    }
    if observed["xrpld_version"] != EXPECTED_VERSION:
        raise EnvironmentValidationError("xrpld version differs from the pin")
    if observed["source_commit"] != EXPECTED_COMMIT:
        raise EnvironmentValidationError("xrpld source commit differs from the pin")
    if observed["network_mode"] != "standalone":
        raise EnvironmentValidationError("xrpld is not running in standalone mode")
    if not observed["fresh_start_flag"]:
        raise EnvironmentValidationError("xrpld did not start with --start")
    if not data_mount_matches_config:
        raise EnvironmentValidationError("data volume is not mounted at the configured path")
    return observed


def create_manifest() -> dict[str, Any]:
    files = []
    for path in sorted(EVIDENCE_DIR.iterdir()):
        if path.is_file() and path != MANIFEST_PATH:
            raw = path.read_bytes()
            files.append(
                {
                    "path": path.name,
                    "bytes": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                }
            )
    manifest = {"algorithm": "SHA-256", "file_count": len(files), "files": files}
    write_json(MANIFEST_PATH, manifest)
    return manifest


def main() -> int:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    provenance = build_provenance()
    server_before_raw, server_before_evidence = capture_server_info("server-info-before.json")
    server_before = server_summary(server_before_raw)
    if server_before["network_id"] != EXPECTED_NETWORK_ID:
        raise EnvironmentValidationError("network ID differs from the spike configuration")

    feature_before_raw, feature_before_evidence = capture_rpc("feature-before.json", "feature")
    amendment_before = validate_feature_response(feature_before_raw)
    ledger_amendments_before_raw, ledger_amendments_before_evidence = capture_rpc(
        "amendments-ledger-entry-before.json",
        "ledger_entry",
        **amendments_ledger_entry_params(),
    )
    ledger_amendments_before = validate_amendments_ledger_entry(ledger_amendments_before_raw)
    cross_check_before = cross_check_amendment_representations(
        amendment_before, ledger_amendments_before
    )
    ledger_before = validated_ledger()

    closures: list[dict[str, Any]] = []
    previous_index = ledger_before["ledger_index"]
    for attempt in range(1, 3):
        accept_response, _ = rpc_raw("ledger_accept")
        ledger = validated_ledger()
        if ledger["ledger_index"] != previous_index + 1:
            raise EnvironmentValidationError(
                f"ledger close {attempt} did not advance exactly one index"
            )
        closures.append(
            {
                "attempt": attempt,
                "ledger_accept_status": accept_response["result"]["status"],
                "ledger": ledger,
            }
        )
        previous_index = ledger["ledger_index"]

    feature_after_raw, feature_after_evidence = capture_rpc("feature-after.json", "feature")
    amendment_after = validate_feature_response(feature_after_raw)
    ledger_amendments_after_raw, ledger_amendments_after_evidence = capture_rpc(
        "amendments-ledger-entry-after.json",
        "ledger_entry",
        **amendments_ledger_entry_params(),
    )
    ledger_amendments_after = validate_amendments_ledger_entry(ledger_amendments_after_raw)
    cross_check_after = cross_check_amendment_representations(
        amendment_after, ledger_amendments_after
    )
    server_after_raw, server_after_evidence = capture_server_info("server-info-after.json")
    server_after = server_summary(server_after_raw)
    state_survived = (
        amendment_before == amendment_after
        and ledger_amendments_before == ledger_amendments_after
        and cross_check_before == cross_check_after
        and server_before["build_version"] == server_after["build_version"]
        and server_before["network_id"] == server_after["network_id"]
    )
    if not state_survived:
        raise EnvironmentValidationError("required server state changed across ledger closes")

    output = {
        "experiment": "E1",
        "result": "PASS",
        "build_provenance": provenance,
        "network": {
            "mode": provenance["network_mode"],
            "network_id": server_before["network_id"],
        },
        "server_before": server_before,
        "server_after": server_after,
        "ledger_before": ledger_before,
        "ledger_closures": closures,
        "ledger_close_reproducible": True,
        "state_survived_closures": state_survived,
        "amendment_validation_before": amendment_before,
        "amendment_validation_after": amendment_after,
        "amendments_ledger_validation_before": ledger_amendments_before,
        "amendments_ledger_validation_after": ledger_amendments_after,
        "feature_vs_ledger_cross_check_before": cross_check_before,
        "feature_vs_ledger_cross_check_after": cross_check_after,
        "authoritative_request": {
            "method": "ledger_entry",
            "params": amendments_ledger_entry_params(),
            "source": [
                "rippled 3.4.0 src/xrpld/rpc/handlers/ledger/LedgerEntry.cpp: fixed()/parseFixed()",
                "rippled 3.4.0 src/test/rpc/LedgerEntry_test.cpp: field:true fixed-entry test",
            ],
            "previous_request_invalid_because": "An empty object is not boolean true; parseFixed therefore attempted to parse it as a ledger object index and returned malformedRequest.",
        },
        "raw_feature_evidence": {
            "before": feature_before_evidence,
            "after": feature_after_evidence,
            "validator_agrees_with_both": amendment_before["valid"] and amendment_after["valid"],
        },
        "raw_amendments_ledger_evidence": {
            "before": ledger_amendments_before_evidence,
            "after": ledger_amendments_after_evidence,
            "validator_agrees_with_both": ledger_amendments_before["valid"]
            and ledger_amendments_after["valid"],
        },
        "raw_server_evidence": {
            "before": server_before_evidence,
            "after": server_after_evidence,
        },
        "later_experiments_executed": False,
    }
    write_json(OUTPUT_PATH, output)
    create_manifest()
    print(json.dumps({"E1": "PASS", "evidence": str(OUTPUT_PATH)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
