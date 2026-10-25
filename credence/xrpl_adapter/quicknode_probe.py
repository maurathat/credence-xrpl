"""X1q: read-only compatibility probe for a configured QuickNode XRPL Testnet endpoint.

Run only by an operator, with the endpoint in the environment::

    CREDENCE_QUICKNODE_XRPL_URL=... CREDENCE_QUICKNODE_XRPL_NETWORK=testnet \\
        uv run python -m credence.xrpl_adapter.quicknode_probe \\
        --account <public Testnet account> --tx <existing public Payment hash> --out report.json

It calls ``server_info``, ``ledger``, ``server_state`` (sampled repeatedly to see whether
``pubkey_node`` is stable), ``account_info``, ``account_objects``, ``account_lines`` and ``tx``
(binary and JSON). It writes no ledger state and submits nothing.

The report is sanitized: response *shapes* (key names and value types, never values), plus public
ledger facts the checks need. The endpoint URL and its token never appear; only the endpoint
fingerprint does. Without a configured endpoint it exits 2 and reports real-endpoint verification
as PENDING. A transaction hash is never invented: without ``--tx`` the transaction check is
NOT_RUN.

Owner: XRPL Integration. X1q delegated to Da Nang (Claude Code) by Maura for this checkpoint.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from typing import Any

from credence.xrpl_adapter.quicknode import (
    QuickNodeConfig,
    QuickNodeError,
    QuickNodeJsonRpcClient,
    QuickNodeXRPLTransport,
    RawRead,
)

PROBE_SCHEMA = "credence.xrpl.quicknode-probe.v1"


def response_shape(value: object) -> object:
    """Key names and value types only. Lists keep the shape of their first element."""
    if isinstance(value, dict):
        return {str(key): response_shape(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return [response_shape(value[0])] if value else []
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    return "null"


def _shape_key(read: RawRead) -> str:
    params = json.loads(read.params)
    if read.method == "tx":
        return f"tx:{'binary' if params.get('binary') else 'json'}"
    return read.method


def _shapes(reads: Sequence[RawRead]) -> dict[str, object]:
    shapes: dict[str, object] = {}
    for read in reads:
        try:
            shapes.setdefault(_shape_key(read), response_shape(json.loads(read.response)))
        except (UnicodeDecodeError, json.JSONDecodeError):
            shapes.setdefault(_shape_key(read), "not JSON")
    return shapes


def _check(run: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return {"status": "PASS", **run()}
    except QuickNodeError as exc:
        return {"status": "FAIL", "code": exc.code}


def run_probe(
    transport: QuickNodeXRPLTransport,
    *,
    account: str | None,
    transaction_hash: str | None,
    node_samples: int = 5,
) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    identity_holder: list[Any] = []

    def identity() -> dict[str, Any]:
        found = transport.validated_ledger_identity()
        identity_holder.append(found)
        return {
            "network_id": found.network_id,
            "source_kind": found.source_kind,
            "server_version": found.server_version,
            "rippled_version": found.rippled_version,
            "validated_ledger_index": found.ledger_index,
            "validated_ledger_hash": found.ledger_hash,
            "complete_ledgers": found.complete_ledgers,
        }

    checks["validated_ledger_identity"] = _check(identity)

    def nodes() -> dict[str, Any]:
        samples = [transport.sample_node_identity() for _ in range(node_samples)]
        return {
            "samples": len(samples),
            "forwarded": sorted({sample.forwarded for sample in samples}),
            "distinct_node_public_keys": list(transport.observed_node_public_keys),
            "build_versions": sorted({sample.build_version for sample in samples}),
        }

    checks["node_identity_stability"] = _check(nodes)

    if account is None or not identity_holder:
        checks["account_reads"] = {"status": "NOT_RUN"}
    else:
        pinned = identity_holder[0]

        def account_reads() -> dict[str, Any]:
            snapshot = transport.account_state(account, pinned)
            signer_list = snapshot.signer_list
            return {
                "exists": snapshot.exists,
                "sequence": snapshot.sequence,
                "flags": snapshot.flags,
                "owner_count": snapshot.owner_count,
                "regular_key_present": snapshot.regular_key is not None,
                "signer_list": None
                if signer_list is None
                else {"quorum": signer_list.quorum, "entries": len(signer_list.entries)},
                "account_objects": len(transport.account_objects(account, pinned)),
                "account_lines": len(transport.account_lines(account, pinned)),
            }

        checks["account_reads"] = _check(account_reads)

    if transaction_hash is None:
        checks["transaction_lookup"] = {"status": "NOT_RUN", "code": "NO_PUBLIC_HASH_SUPPLIED"}
    else:
        observation = transport.observe_payment(transaction_hash)
        checks["transaction_lookup"] = {
            "status": "PASS" if observation.status.value == "VALIDATED" else "FAIL",
            "observation_status": observation.status.value,
            "code": observation.code,
            "ledger_index": observation.ledger_index,
            "ledger_hash": observation.ledger_hash,
            "result": observation.result,
            "delivered": None
            if observation.delivered is None
            else {
                "currency": observation.delivered.currency,
                "issuer": observation.delivered.issuer,
                "value": observation.delivered.value,
            },
        }

    return {
        "schema": PROBE_SCHEMA,
        "source_id": transport.source_id,
        "endpoint_fingerprint": transport.endpoint_fingerprint,
        "expected_network_id": transport.network_id,
        "contradiction": transport.contradiction,
        "checks": checks,
        "response_shapes": _shapes(transport.recent_reads()),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only QuickNode XRPL Testnet probe.")
    parser.add_argument("--account", help="a public Testnet account to read")
    parser.add_argument("--tx", help="an existing public Testnet Payment hash")
    parser.add_argument("--node-samples", type=int, default=5)
    parser.add_argument("--out", help="write the sanitized report here instead of stdout")
    args = parser.parse_args(argv)
    try:
        config = QuickNodeConfig.from_environment()
    except QuickNodeError as exc:
        print(f"QuickNode configuration refused: {exc.code}", file=sys.stderr)
        return 2
    if config is None:
        print("QuickNode is not configured: real-endpoint verification PENDING", file=sys.stderr)
        return 2
    transport = QuickNodeXRPLTransport(QuickNodeJsonRpcClient(config), config)
    report = run_probe(
        transport,
        account=args.account,
        transaction_hash=args.tx,
        node_samples=max(1, min(args.node_samples, 20)),
    )
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
