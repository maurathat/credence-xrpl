"""X1q/X1s: read-only compatibility probe for a configured QuickNode XRPL Testnet endpoint.

Run only by an operator, with the endpoint in the environment::

    CREDENCE_QUICKNODE_XRPL_URL=... CREDENCE_QUICKNODE_XRPL_NETWORK=testnet \\
        uv run python -m credence.xrpl_adapter.quicknode_probe \\
        --account <public Testnet account> --tx <existing public Payment hash> --out report.json

It calls ``server_info``, ``ledger``, ``server_state`` (sampled repeatedly to see whether
``pubkey_node`` is stable), ``account_info``, ``account_objects``, ``account_lines`` and ``tx``
(binary and JSON). It writes no ledger state and submits nothing.

The report is sanitized: response *shapes* (key names and value types, never values), plus public
ledger facts the checks need. The endpoint URL and its token never appear; only the endpoint
fingerprint does. A transaction hash is never invented: without ``--tx`` the transaction check is
NOT_RUN.

Every check is contained. A malformed or hostile response becomes a ``FAIL`` with a typed code,
the checks that passed are kept, and the command never ends in a traceback.

Exit codes:

- ``0``: every check ``PASS``;
- ``1``: at least one check ``FAIL``, the transport latched a contradiction, or the probe itself
  failed (``PROBE_INTERNAL_ERROR``); the report is still written;
- ``2``: not configured (real-endpoint verification PENDING) or configuration refused; no report;
- ``3``: incomplete: nothing failed, but at least one check is ``NOT_RUN``.

Owner: XRPL Integration. X1q/X1s delegated to Da Nang (Claude Code) by Maura.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from credence.xrpl_adapter.quicknode import (
    HostedLedgerIdentity,
    QuickNodeConfig,
    QuickNodeError,
    QuickNodeJsonRpcClient,
    QuickNodeXRPLTransport,
    RawRead,
    parse_json_bounded,
)

PROBE_SCHEMA = "credence.xrpl.quicknode-probe.v1"
EXIT_PASS = 0
EXIT_FAILED = 1
EXIT_NOT_CONFIGURED = 2
EXIT_INCOMPLETE = 3
_EXIT_CODES = {"PASS": EXIT_PASS, "FAIL": EXIT_FAILED, "INCOMPLETE": EXIT_INCOMPLETE}


class _CheckFailed(Exception):
    def __init__(self, code: str, facts: dict[str, Any]) -> None:
        super().__init__(code)
        self.code = code
        self.facts = facts


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
    if read.method == "tx":
        return "tx:binary" if '"binary": true' in read.params else "tx:json"
    return read.method


def _shapes(reads: Sequence[RawRead]) -> dict[str, object]:
    shapes: dict[str, object] = {}
    for read in reads:
        try:
            shape = response_shape(parse_json_bounded(read.response, read.method))
        except QuickNodeError as exc:
            shape = f"refused: {exc.code}"
        shapes.setdefault(_shape_key(read), shape)
    return shapes


def _check(run: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Run one check. Any failure, typed or not, is a FAIL with a code and no traceback."""
    code = "PROBE_CHECK_FAILED"
    facts: dict[str, Any] = {}
    try:
        return {"status": "PASS", **run()}
    except _CheckFailed as exc:
        code, facts = exc.code, exc.facts
    except QuickNodeError as exc:
        code = exc.code
    except Exception:  # a probe must report, not crash
        pass
    return {"status": "FAIL", "code": code, **facts}


def probe_outcome(report: Mapping[str, Any]) -> str:
    statuses = [check.get("status") for check in report.get("checks", {}).values()]
    if report.get("contradiction") or "FAIL" in statuses or not statuses:
        return "FAIL"
    return "INCOMPLETE" if "NOT_RUN" in statuses else "PASS"


def run_probe(
    transport: QuickNodeXRPLTransport,
    *,
    account: str | None,
    transaction_hash: str | None,
    node_samples: int = 5,
) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    pinned: list[HostedLedgerIdentity] = []

    def identity() -> dict[str, Any]:
        found = transport.validated_ledger_identity()
        pinned.append(found)
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

    account_checks = ("account_info", "account_objects", "account_lines")
    if account is None or not pinned:
        code = "NO_ACCOUNT_SUPPLIED" if account is None else "NO_VALIDATED_LEDGER_IDENTITY"
        for name in account_checks:
            checks[name] = {"status": "NOT_RUN", "code": code}
    else:
        ledger = pinned[0]

        def account_info() -> dict[str, Any]:
            snapshot = transport.account_state(account, ledger)
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
            }

        checks["account_info"] = _check(account_info)
        checks["account_objects"] = _check(
            lambda: {"count": len(transport.account_objects(account, ledger))}
        )
        checks["account_lines"] = _check(
            lambda: {"count": len(transport.account_lines(account, ledger))}
        )

    if transaction_hash is None:
        checks["transaction_lookup"] = {"status": "NOT_RUN", "code": "NO_PUBLIC_HASH_SUPPLIED"}
    else:
        checks["transaction_lookup"] = _check(lambda: _transaction(transport, transaction_hash))

    report: dict[str, Any] = {
        "schema": PROBE_SCHEMA,
        "source_id": transport.source_id,
        "endpoint_fingerprint": transport.endpoint_fingerprint,
        "expected_network_id": transport.network_id,
        "contradiction": transport.contradiction,
        "checks": checks,
        "response_shapes": _shapes(transport.recent_reads()),
    }
    report["outcome"] = probe_outcome(report)
    return report


def _transaction(transport: QuickNodeXRPLTransport, transaction_hash: str) -> dict[str, Any]:
    observation = transport.observe_payment(transaction_hash)
    facts: dict[str, Any] = {
        "observation_status": observation.status.value,
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
    if observation.status.value != "VALIDATED":
        raise _CheckFailed(observation.code, facts)
    return {"code": observation.code, **facts}


def _failure_report(transport: QuickNodeXRPLTransport | None, code: str) -> dict[str, Any]:
    return {
        "schema": PROBE_SCHEMA,
        "source_id": None if transport is None else transport.source_id,
        "endpoint_fingerprint": None if transport is None else transport.endpoint_fingerprint,
        "checks": {},
        "outcome": "FAIL",
        "code": code,
    }


def main(
    argv: Sequence[str] | None = None,
    *,
    transport_factory: Callable[[QuickNodeConfig], QuickNodeXRPLTransport] | None = None,
) -> int:
    """Run the probe. ``transport_factory`` is a test seam; the default uses the real client."""
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
        return EXIT_NOT_CONFIGURED
    if config is None:
        print("QuickNode is not configured: real-endpoint verification PENDING", file=sys.stderr)
        return EXIT_NOT_CONFIGURED
    transport: QuickNodeXRPLTransport | None = None
    try:
        factory = transport_factory or (
            lambda chosen: QuickNodeXRPLTransport(QuickNodeJsonRpcClient(chosen), chosen)
        )
        transport = factory(config)
        report = run_probe(
            transport,
            account=args.account,
            transaction_hash=args.tx,
            node_samples=max(1, min(args.node_samples, 20)),
        )
    except Exception:  # the probe reports its own failure instead of a traceback
        report = _failure_report(transport, "PROBE_INTERNAL_ERROR")
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.out:
        try:
            with open(args.out, "w", encoding="utf-8") as handle:
                handle.write(text)
        except OSError:
            print("could not write the probe report", file=sys.stderr)
            return EXIT_FAILED
    else:
        sys.stdout.write(text)
    print(f"probe outcome: {report['outcome']}", file=sys.stderr)
    return _EXIT_CODES[report["outcome"]]


if __name__ == "__main__":
    sys.exit(main())
