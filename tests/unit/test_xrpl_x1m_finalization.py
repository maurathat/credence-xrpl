"""X1m: terminal ledger evidence atomically closes interrupted submission attempts."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from test_xrpl_x1a_flow import build_flow, delivered
from x1a_world import OPERATION, OWNER, VALIDATED_LEDGER

from credence.xrpl_adapter.journal import PaymentJournal
from credence.xrpl_adapter.service import PaymentService
from credence.xrpl_adapter.types import (
    JournalConflict,
    SubmissionDisposition,
    TransactionState,
)

FINALIZED_ATTEMPT_STATUS = "ABANDONED_FINAL_OUTCOME_RECOVERED_FROM_VALIDATED_LEDGER_EVIDENCE"


def crashed_flow(tmp_path: Path) -> Any:
    flow = build_flow(tmp_path)
    flow.offline.queue_submission(RuntimeError("simulated process termination after submission"))
    with pytest.raises(RuntimeError, match="process termination"):
        flow.service.submit(OPERATION)
    (attempt,) = flow.journal.attempts(OPERATION)
    assert attempt.completed_at is None
    return flow


def restarted_service(flow: Any, database: Path) -> PaymentService:
    return PaymentService(
        journal=PaymentJournal(database),
        preparer=flow.service._preparer,
        inspector=flow.service._inspector,
        transport=flow.offline,
        lookup=flow.trusted,
        ledger_observer=flow.trusted,
    )


def assert_attempt_closed(journal: PaymentJournal) -> None:
    (attempt,) = journal.attempts(OPERATION)
    assert attempt.completed_at is not None
    assert attempt.disposition is None
    assert attempt.provisional_status == FINALIZED_ATTEMPT_STATUS


def test_recovered_validated_success_closes_attempt_and_reopens_idempotently(
    tmp_path: Path,
) -> None:
    database = tmp_path / "journal.sqlite3"
    flow = crashed_flow(tmp_path)
    original = flow.journal.get(OPERATION)
    flow.rippled.include(
        flow.signed,
        ledger_index=VALIDATED_LEDGER + 3,
        delivered=delivered(),
    )
    flow.rippled.advance(VALIDATED_LEDGER + 3)

    recovered = restarted_service(flow, database).reconcile(OPERATION)

    assert recovered.state is TransactionState.VALIDATED_SUCCESS
    assert (recovered.signed_transaction, recovered.transaction_hash) == (
        original.signed_transaction,
        original.transaction_hash,
    )
    reopened = PaymentJournal(database)
    assert_attempt_closed(reopened)
    (event,) = reopened.pending_settlement_events()
    assert event.transaction_hash == original.transaction_hash

    repeated = reopened.finalize(
        OPERATION,
        state=recovered.state,
        result=recovered.final_result or "",
        detail=recovered.final_detail or "",
        evidence=recovered.final_evidence or b"",
        validated_ledger_index=recovered.validated_ledger_index,
        validated_ledger_hash=recovered.validated_ledger_hash,
        verified_delivered_amount_micro_units=recovered.verified_delivered_amount_micro_units,
        evidence_verification_version=recovered.evidence_verification_version,
    )
    assert repeated == recovered
    assert_attempt_closed(reopened)
    assert len(reopened.pending_settlement_events()) == 1


def test_recovered_validated_failure_closes_attempt_without_outbox(tmp_path: Path) -> None:
    database = tmp_path / "journal.sqlite3"
    flow = crashed_flow(tmp_path)
    flow.rippled.include(
        flow.signed,
        ledger_index=VALIDATED_LEDGER + 2,
        result="tecUNFUNDED_PAYMENT",
    )
    flow.rippled.advance(VALIDATED_LEDGER + 2)

    recovered = restarted_service(flow, database).reconcile(OPERATION)

    assert recovered.state is TransactionState.VALIDATED_FAILURE
    journal = PaymentJournal(database)
    assert_attempt_closed(journal)
    assert journal.pending_settlement_events() == ()


def test_concurrent_recovery_finalization_closes_once_and_emits_one_event(
    tmp_path: Path,
) -> None:
    database = tmp_path / "journal.sqlite3"
    flow = crashed_flow(tmp_path)
    flow.rippled.include(
        flow.signed,
        ledger_index=VALIDATED_LEDGER + 3,
        delivered=delivered(),
    )
    flow.rippled.advance(VALIDATED_LEDGER + 3)
    workers = (restarted_service(flow, database), restarted_service(flow, database))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda worker: worker.reconcile(OPERATION), workers))

    assert {result.state for result in results} == {TransactionState.VALIDATED_SUCCESS}
    journal = PaymentJournal(database)
    assert_attempt_closed(journal)
    assert len(journal.pending_settlement_events()) == 1


def test_stale_lease_owner_cannot_complete_after_finalization(tmp_path: Path) -> None:
    flow = build_flow(tmp_path)
    original = flow.journal.get(OPERATION)
    flow.journal.record_submission_anchor(OPERATION, flow.trusted.validated_ledger(OWNER.account))
    lease = flow.journal.begin_submission(OPERATION, resubmission=False)
    assert lease is not None
    flow.rippled.include(
        flow.signed,
        ledger_index=VALIDATED_LEDGER + 3,
        delivered=delivered(),
    )
    flow.rippled.advance(VALIDATED_LEDGER + 3)
    assert flow.service.reconcile(OPERATION).state is TransactionState.VALIDATED_SUCCESS

    with pytest.raises(JournalConflict, match="not owned"):
        flow.journal.finish_submission(
            OPERATION,
            lease,
            disposition=SubmissionDisposition.ACKNOWLEDGED,
            next_state=TransactionState.PENDING_CONFIRMATION,
            provisional_status="stale transport response",
        )

    finalized = flow.journal.get(OPERATION)
    assert finalized.state is TransactionState.VALIDATED_SUCCESS
    assert (finalized.signed_transaction, finalized.transaction_hash) == (
        original.signed_transaction,
        original.transaction_hash,
    )
    assert_attempt_closed(flow.journal)
    assert len(flow.journal.pending_settlement_events()) == 1


def test_failed_success_finalization_rolls_back_attempt_closure_and_state(tmp_path: Path) -> None:
    flow = build_flow(tmp_path)
    flow.journal.record_submission_anchor(OPERATION, flow.trusted.validated_ledger(OWNER.account))
    assert flow.journal.begin_submission(OPERATION, resubmission=False) is not None

    with pytest.raises(JournalConflict, match="settlement requires X0s-verified evidence"):
        flow.journal.finalize(
            OPERATION,
            state=TransactionState.VALIDATED_SUCCESS,
            result="tesSUCCESS",
            detail="invalid unverified finalization",
            evidence=b"unverified",
            validated_ledger_index=VALIDATED_LEDGER + 1,
            validated_ledger_hash=f"{VALIDATED_LEDGER + 1:064X}",
            verified_delivered_amount_micro_units=1_500_000,
            evidence_verification_version=0,
        )

    assert flow.journal.get(OPERATION).state is TransactionState.SUBMITTED
    (attempt,) = flow.journal.attempts(OPERATION)
    assert attempt.completed_at is None
    assert flow.journal.pending_settlement_events() == ()


def test_repeated_finalization_repairs_preexisting_terminal_record_with_open_attempt(
    tmp_path: Path,
) -> None:
    flow = build_flow(tmp_path)
    flow.journal.record_submission_anchor(OPERATION, flow.trusted.validated_ledger(OWNER.account))
    assert flow.journal.begin_submission(OPERATION, resubmission=False) is not None
    database = tmp_path / "journal.sqlite3"
    ledger_index = VALIDATED_LEDGER + 1
    ledger_hash = f"{ledger_index:064X}"
    evidence = b"preserved validated failure evidence"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """UPDATE xrpl_payment_transactions
               SET state = 'VALIDATED_FAILURE', final_result = 'tecUNFUNDED_PAYMENT',
                   final_detail = 'validated terminal failure', final_evidence = ?,
                   validated_ledger_index = ?, validated_ledger_hash = ?,
                   evidence_verification_version = 1
               WHERE operation_id = ?""",
            (evidence, ledger_index, ledger_hash, OPERATION),
        )

    reopened = PaymentJournal(database)
    result = reopened.finalize(
        OPERATION,
        state=TransactionState.VALIDATED_FAILURE,
        result="tecUNFUNDED_PAYMENT",
        detail="validated terminal failure",
        evidence=evidence,
        validated_ledger_index=ledger_index,
        validated_ledger_hash=ledger_hash,
        verified_delivered_amount_micro_units=None,
        evidence_verification_version=1,
    )

    assert result.state is TransactionState.VALIDATED_FAILURE
    assert_attempt_closed(reopened)
    assert reopened.pending_settlement_events() == ()


def test_attempt_closure_and_terminal_state_share_one_database_transaction(tmp_path: Path) -> None:
    flow = build_flow(tmp_path)
    flow.journal.record_submission_anchor(OPERATION, flow.trusted.validated_ledger(OWNER.account))
    assert flow.journal.begin_submission(OPERATION, resubmission=False) is not None
    database = tmp_path / "journal.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """CREATE TRIGGER refuse_terminal_update
               BEFORE UPDATE OF state ON xrpl_payment_transactions
               WHEN NEW.state = 'VALIDATED_FAILURE'
               BEGIN SELECT RAISE(ABORT, 'injected terminal write failure'); END"""
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected terminal write failure"):
        flow.journal.finalize(
            OPERATION,
            state=TransactionState.VALIDATED_FAILURE,
            result="tecUNFUNDED_PAYMENT",
            detail="validated terminal failure",
            evidence=b"validated failure evidence",
            validated_ledger_index=VALIDATED_LEDGER + 1,
            validated_ledger_hash=f"{VALIDATED_LEDGER + 1:064X}",
            verified_delivered_amount_micro_units=None,
            evidence_verification_version=1,
        )

    assert flow.journal.get(OPERATION).state is TransactionState.SUBMITTED
    (attempt,) = flow.journal.attempts(OPERATION)
    assert attempt.completed_at is None
    assert flow.journal.pending_settlement_events() == ()
