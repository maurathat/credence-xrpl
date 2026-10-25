"""X1g adversarial tests for bounded, identity-stable transaction non-inclusion proof."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from test_xrpl_x1a_flow import OPERATION, build_flow, delivered, submit_accepted
from x1a_rippled import NODE_KEY
from x1a_world import LAST_LEDGER, VALIDATED_LEDGER

from credence.xrpl_adapter.types import TransactionState


def test_history_gap_then_backfill_cannot_create_false_expiry(tmp_path: Path) -> None:
    """F1: ledger 101 exists, but the transaction index is unavailable during the first lookup."""
    flow = build_flow(tmp_path)
    submit_accepted(flow)
    tx_hash = flow.rippled.include(
        flow.signed, ledger_index=VALIDATED_LEDGER + 1, delivered=delivered()
    )
    gap = f"1-{VALIDATED_LEDGER},{VALIDATED_LEDGER + 2}-{LAST_LEDGER + 1}"
    flow.rippled.advance(LAST_LEDGER + 1, complete=gap)

    def missing_then_backfilled(request: dict[str, Any]) -> dict[str, Any] | None:
        if request["transaction"] != tx_hash:
            return None
        flow.rippled.complete_ledgers = f"1-{LAST_LEDGER + 1}"
        return {"status": "error", "error": "txnNotFound", "searched_all": False}

    flow.rippled.faults["tx"] = missing_then_backfilled
    unresolved = flow.service.reconcile(OPERATION)
    assert (unresolved.state, unresolved.last_provisional_status) == (
        TransactionState.UNKNOWN,
        "HISTORY_CHANGED_DURING_LOOKUP",
    )
    assert unresolved.final_evidence is None
    assert flow.journal.pending_settlement_events() == ()

    del flow.rippled.faults["tx"]
    assert flow.service.reconcile(OPERATION).state is TransactionState.VALIDATED_SUCCESS


def test_stable_history_gap_and_searched_all_false_stay_unknown(tmp_path: Path) -> None:
    flow = build_flow(tmp_path)
    submit_accepted(flow)
    flow.rippled.advance(
        LAST_LEDGER + 1,
        complete=f"1-{VALIDATED_LEDGER},{VALIDATED_LEDGER + 2}-{LAST_LEDGER + 1}",
    )
    record = flow.service.reconcile(OPERATION)
    assert (record.state, record.last_provisional_status) == (
        TransactionState.UNKNOWN,
        "TRANSACTION_RANGE_NOT_FULLY_SEARCHED",
    )


def test_missing_searched_all_is_not_inferred_from_txn_not_found(tmp_path: Path) -> None:
    flow = build_flow(tmp_path)
    submit_accepted(flow)
    flow.rippled.advance(LAST_LEDGER + 1)
    flow.rippled.faults["tx"] = lambda request: {
        "status": "error",
        "error": "txnNotFound",
    }
    record = flow.service.reconcile(OPERATION)
    assert (record.state, record.last_provisional_status) == (
        TransactionState.UNKNOWN,
        "TRANSACTION_RANGE_NOT_FULLY_SEARCHED",
    )


def test_node_change_during_not_found_lookup_is_unavailable(tmp_path: Path) -> None:
    flow = build_flow(tmp_path)
    submit_accepted(flow)
    flow.rippled.advance(LAST_LEDGER + 1)
    real_state = flow.rippled._server_state
    reads = {"count": 0}

    def changed_node(request: dict[str, Any]) -> dict[str, Any]:
        reads["count"] += 1
        result = real_state(request)
        if reads["count"] >= 2:
            result["state"]["pubkey_node"] = "n9ChangedDuringNotFoundLookup"
        return result

    flow.rippled.faults["server_state"] = changed_node
    record = flow.service.reconcile(OPERATION)
    assert (record.state, record.last_provisional_status) == (
        TransactionState.UNKNOWN,
        "NODE_IDENTITY_MISMATCH",
    )


def test_history_truncation_during_not_found_lookup_is_unavailable(tmp_path: Path) -> None:
    flow = build_flow(tmp_path)
    submit_accepted(flow)
    flow.rippled.advance(LAST_LEDGER + 1)

    def truncate_after_search(request: dict[str, Any]) -> dict[str, Any]:
        flow.rippled.complete_ledgers = f"{VALIDATED_LEDGER + 2}-{LAST_LEDGER + 1}"
        return {"status": "error", "error": "txnNotFound", "searched_all": True}

    flow.rippled.faults["tx"] = truncate_after_search
    record = flow.service.reconcile(OPERATION)
    assert (record.state, record.last_provisional_status) == (
        TransactionState.UNKNOWN,
        "HISTORY_CHANGED_DURING_LOOKUP",
    )


def test_complete_bounded_search_proves_non_inclusion(tmp_path: Path) -> None:
    flow = build_flow(tmp_path)
    submit_accepted(flow)
    flow.rippled.advance(LAST_LEDGER + 1)
    flow.rippled.transaction_search_ranges = ((1, LAST_LEDGER),)
    record = flow.service.reconcile(OPERATION)
    assert record.state is TransactionState.EXPIRED_NOT_INCLUDED
    assert record.final_evidence is not None
    evidence = json.loads(record.final_evidence)
    bounded = evidence["bounded_lookup"]
    assert (bounded["min_ledger"], bounded["max_ledger"], bounded["searched_all"]) == (
        VALIDATED_LEDGER + 1,
        LAST_LEDGER,
        True,
    )
    assert bounded["validated_identity"]["node_public_key"] == NODE_KEY
    assert bounded["validated_identity"] == bounded["validated_identity_after"]
    tx_calls = [params for method, params in flow.rippled.calls if method == "tx"]
    assert tx_calls == [
        {
            "transaction": record.transaction_hash,
            "binary": True,
            "api_version": 2,
        },
        {
            "transaction": record.transaction_hash,
            "binary": True,
            "min_ledger": VALIDATED_LEDGER + 1,
            "max_ledger": LAST_LEDGER,
            "api_version": 2,
        },
    ]
