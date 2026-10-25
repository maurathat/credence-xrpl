"""X1i adversarial coverage for positive lookup liveness, rebroadcast, and pin validation."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_xrpl_x1a_flow import OPERATION, build_flow, delivered
from x1a_rippled import NODE_KEY
from x1a_world import LAST_LEDGER, VALIDATED_LEDGER

from credence.xrpl_adapter.rippled import RippledTransportConfig, RpcTimeout
from credence.xrpl_adapter.service import PaymentService
from credence.xrpl_adapter.types import (
    LookupDisposition,
    SubmissionResponse,
    SubmissionTimeout,
    TransactionState,
)


@pytest.mark.parametrize("window", [999, 1_000, 1_500])
def test_positive_inclusion_is_not_blocked_by_non_inclusion_window(
    tmp_path: Path, window: int
) -> None:
    flow = build_flow(tmp_path)
    tx_hash = flow.rippled.include(
        flow.signed,
        ledger_index=VALIDATED_LEDGER + 1,
        delivered=delivered(),
    )
    flow.rippled.advance(VALIDATED_LEDGER + window)

    result = flow.trusted.lookup(
        tx_hash,
        min_ledger=VALIDATED_LEDGER + 1,
        max_ledger=VALIDATED_LEDGER + window,
    )

    assert result.disposition is LookupDisposition.FOUND
    assert result.payment is not None and result.payment.validated
    assert result.payment.ledger_index == VALIDATED_LEDGER + 1
    tx_calls = [params for method, params in flow.rippled.calls if method == "tx"]
    assert all("min_ledger" not in params and "max_ledger" not in params for params in tx_calls)


def test_long_not_found_window_is_unresolved_without_attempting_unsupported_search(
    tmp_path: Path,
) -> None:
    flow = build_flow(tmp_path)
    result = flow.trusted.lookup(
        "AB" * 32,
        min_ledger=VALIDATED_LEDGER + 1,
        max_ledger=VALIDATED_LEDGER + 1_500,
    )

    assert (result.disposition, result.non_inclusion_proof, result.detail) == (
        LookupDisposition.NOT_FOUND,
        None,
        "BOUNDED_NON_INCLUSION_PROOF_UNAVAILABLE",
    )
    tx_calls = [params for method, params in flow.rippled.calls if method == "tx"]
    assert len(tx_calls) == 1
    assert "min_ledger" not in tx_calls[0] and "max_ledger" not in tx_calls[0]


def test_pinned_rippled_range_boundary_can_prove_non_inclusion(tmp_path: Path) -> None:
    flow = build_flow(tmp_path)
    last = VALIDATED_LEDGER + 1_001
    flow.rippled.advance(last + 1)
    flow.rippled.transaction_search_ranges = ((1, last),)

    result = flow.trusted.lookup(
        "AB" * 32,
        min_ledger=VALIDATED_LEDGER + 1,
        max_ledger=last,
    )

    assert result.disposition is LookupDisposition.NOT_FOUND
    assert result.non_inclusion_proof is not None
    assert result.non_inclusion_proof.searched_all is True


def test_timeout_rebroadcasts_identical_bytes_before_expiry_across_restarts(
    tmp_path: Path,
) -> None:
    flow = build_flow(tmp_path)
    flow.offline.queue_submission(SubmissionTimeout("response lost"))
    first = flow.service.submit(OPERATION)
    assert first.state is TransactionState.UNKNOWN

    for ledger_index in (101, 105, 109):
        flow.rippled.advance(ledger_index)
        flow.offline.queue_submission(SubmissionResponse(True, "tesSUCCESS", "same bytes"))
        restarted = PaymentService(
            journal=flow.journal,
            preparer=flow.service._preparer,
            inspector=flow.service._inspector,
            transport=flow.offline,
            lookup=flow.trusted,
            ledger_observer=flow.trusted,
        )
        record = restarted.reconcile(OPERATION, resubmit_if_pending=True)
        assert record.state is TransactionState.PENDING_CONFIRMATION

    persisted = flow.journal.get(OPERATION)
    assert (
        flow.offline.submitted
        == [
            (persisted.signed_transaction, persisted.transaction_hash),
        ]
        * 4
    )
    assert len(flow.journal.attempts(OPERATION)) == 4


def test_unavailable_lookup_does_not_rebroadcast(tmp_path: Path) -> None:
    flow = build_flow(tmp_path)
    flow.offline.queue_submission(SubmissionTimeout("response lost"))
    flow.service.submit(OPERATION)
    flow.rippled.advance(105)

    def unavailable(_: dict[str, object]) -> dict[str, object]:
        raise RpcTimeout("tx")

    flow.rippled.faults["tx"] = unavailable
    record = flow.service.reconcile(OPERATION, resubmit_if_pending=True)
    assert (record.state, record.last_provisional_status) == (
        TransactionState.UNKNOWN,
        "RPC_UNAVAILABLE:tx",
    )
    assert len(flow.offline.submitted) == 1


def test_expiry_without_transaction_search_proof_never_rebroadcasts_or_finalizes(
    tmp_path: Path,
) -> None:
    flow = build_flow(tmp_path)
    flow.offline.queue_submission(SubmissionTimeout("response lost"))
    flow.service.submit(OPERATION)
    flow.rippled.advance(LAST_LEDGER + 1)
    flow.offline.queue_submission(SubmissionResponse(True, "tesSUCCESS", "must stay unused"))

    record = flow.service.reconcile(OPERATION, resubmit_if_pending=True)

    assert (record.state, record.last_provisional_status) == (
        TransactionState.UNKNOWN,
        "TRANSACTION_RANGE_NOT_FULLY_SEARCHED",
    )
    assert record.final_evidence is None
    assert len(flow.offline.submitted) == 1


def test_complete_ledgers_never_synthesizes_searched_all(tmp_path: Path) -> None:
    flow = build_flow(tmp_path)
    flow.rippled.advance(LAST_LEDGER + 1, complete=f"1-{LAST_LEDGER + 1}")
    flow.rippled.transaction_search_ranges = ()

    result = flow.trusted.lookup(
        "AB" * 32,
        min_ledger=VALIDATED_LEDGER + 1,
        max_ledger=LAST_LEDGER,
    )

    assert (result.disposition, result.non_inclusion_proof, result.detail) == (
        LookupDisposition.NOT_FOUND,
        None,
        "TRANSACTION_RANGE_NOT_FULLY_SEARCHED",
    )


def test_missing_node_pin_argument_is_rejected_at_config_time() -> None:
    with pytest.raises(TypeError):
        RippledTransportConfig(  # type: ignore[call-arg]
            source_id="local-rippled",
            expected_network_id=21_337,
            expected_build_version="3.4.0",
        )


@pytest.mark.parametrize("value", [None, "", "   ", "n9not-a-real-node-key"])
def test_invalid_node_pin_is_rejected_at_config_time(value: object) -> None:
    with pytest.raises(ValueError, match="expected_node_public_key"):
        RippledTransportConfig(
            source_id="local-rippled",
            expected_network_id=21_337,
            expected_build_version="3.4.0",
            expected_node_public_key=value,  # type: ignore[arg-type]
        )


def test_valid_node_pin_is_accepted() -> None:
    config = RippledTransportConfig(
        source_id="local-rippled",
        expected_network_id=21_337,
        expected_build_version="3.4.0",
        expected_node_public_key=NODE_KEY,
    )
    assert config.expected_node_public_key == NODE_KEY
