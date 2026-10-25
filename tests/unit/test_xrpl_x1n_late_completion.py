"""X1n R1: a live lease owner finishing after trusted finalization sees a benign terminal result.

A live worker A holds the 30-second submission lease and is still in transport. Worker F
finalizes the payment from validated ledger evidence, which atomically closes A's attempt as
``ABANDONED_FINAL_OUTCOME_RECOVERED_FROM_VALIDATED_LEDGER_EVIDENCE``. When A then completes with
the persisted transaction identity it broadcast, its lease identity and a self-consistent
validated terminal outcome, the completion is a benign no-op: nothing is written, no event,
lease or resubmission is created, and the terminal state is unchanged. Every other mismatch
remains ``JournalConflict``, and an attempt closed only after its lease expired stays fenced.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from multiprocessing import get_context
from pathlib import Path
from typing import Any

import pytest
from test_xrpl_payment_readiness import payment_request, signed_blob
from test_xrpl_x1a_flow import build_flow, delivered
from test_xrpl_x1k_concurrency import BlockingTransport
from x1a_world import OPERATION, OWNER, VALIDATED_LEDGER

import credence.xrpl_adapter.journal as journal_module
from credence.xrpl_adapter.journal import PaymentJournal
from credence.xrpl_adapter.service import PaymentService
from credence.xrpl_adapter.types import (
    JournalConflict,
    PreparedPayment,
    SubmissionDisposition,
    SubmissionLease,
    TransactionState,
)

FINALIZED = "ABANDONED_FINAL_OUTCOME_RECOVERED_FROM_VALIDATED_LEDGER_EVIDENCE"
LEASE_SECONDS = journal_module.SUBMISSION_LEASE_SECONDS


class Clock:
    """Controls ``datetime.now`` inside the journal module only."""

    def __init__(self) -> None:
        self.base = datetime.now(UTC).replace(microsecond=0)
        self.offset = 0.0

    def at(self, seconds: float) -> None:
        self.offset = seconds

    def now(self) -> datetime:
        return self.base + timedelta(seconds=self.offset)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Iterator[Clock]:
    controlled = Clock()

    class JournalDatetime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
            return controlled.now()

    monkeypatch.setattr(journal_module, "datetime", JournalDatetime)
    yield controlled


def snapshot(database: Path) -> tuple[str, ...]:
    with sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True) as connection:
        return tuple(connection.iterdump())


def worker(flow: Any, *, transport: Any | None = None) -> PaymentService:
    """An independent process-like worker: its own journal handle on the shared database."""
    return PaymentService(
        journal=PaymentJournal(flow.database),
        preparer=flow.service._preparer,
        inspector=flow.service._inspector,
        transport=transport or flow.offline,
        lookup=flow.trusted,
        ledger_observer=flow.trusted,
    )


def leased_flow(tmp_path: Path) -> tuple[Any, SubmissionLease]:
    """Worker A has broadcast-ready bytes and holds a live lease (clock at 0 s)."""
    flow = build_flow(tmp_path)
    flow.database = tmp_path / "journal.sqlite3"
    flow.journal.record_submission_anchor(OPERATION, flow.trusted.validated_ledger(OWNER.account))
    lease = flow.journal.begin_submission(OPERATION, resubmission=False)
    assert lease is not None
    return flow, lease


def finalize_from_ledger(flow: Any) -> None:
    """Worker F observes A's transaction validated and finalizes it."""
    flow.rippled.include(flow.signed, ledger_index=VALIDATED_LEDGER + 3, delivered=delivered())
    flow.rippled.advance(VALIDATED_LEDGER + 3)
    assert worker(flow).reconcile(OPERATION).state is TransactionState.VALIDATED_SUCCESS


def late_completion(
    flow: Any,
    lease: SubmissionLease,
    *,
    operation_id: str = OPERATION,
    transaction_hash: str | None = "persisted",
) -> None:
    if transaction_hash == "persisted":
        transaction_hash = flow.journal.get(OPERATION).transaction_hash
    flow.journal.finish_submission(
        operation_id,
        lease,
        disposition=SubmissionDisposition.ACKNOWLEDGED,
        next_state=TransactionState.PENDING_CONFIRMATION,
        provisional_status="tesSUCCESS",
        transaction_hash=transaction_hash,
    )


def assert_one_terminal_effect(flow: Any) -> None:
    journal = PaymentJournal(flow.database)
    record = journal.get(OPERATION)
    assert record.state is TransactionState.VALIDATED_SUCCESS
    assert record.settlement_status == "XRPL_SETTLED"
    (attempt,) = journal.attempts(OPERATION)
    assert attempt.completed_at is not None
    assert attempt.disposition is None
    assert attempt.provisional_status == FINALIZED
    (event,) = journal.pending_settlement_events()
    assert event.transaction_hash == record.transaction_hash


# --- the benign race -------------------------------------------------------------------------


def test_r1_live_owner_finishing_28_seconds_in_sees_terminal_success(
    tmp_path: Path, clock: Clock
) -> None:
    flow, lease = leased_flow(tmp_path)
    clock.at(LEASE_SECONDS - 2)  # 28 s into a 30 s lease: A is still the live owner
    finalize_from_ledger(flow)
    before = snapshot(flow.database)

    late_completion(flow, lease)  # no exception

    assert snapshot(flow.database) == before
    assert_one_terminal_effect(flow)
    assert flow.offline.submitted == []


def test_r1_service_level_race_one_send_one_event(tmp_path: Path, clock: Clock) -> None:
    flow = build_flow(tmp_path)
    flow.database = tmp_path / "journal.sqlite3"
    transport = BlockingTransport()
    worker_a = worker(flow, transport=transport)
    results: list[TransactionState] = []
    errors: list[BaseException] = []

    def run_a() -> None:
        try:
            results.append(worker_a.submit(OPERATION).state)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    thread = threading.Thread(target=run_a)
    thread.start()
    assert transport.entered.wait(5)
    clock.at(LEASE_SECONDS - 2)
    finalize_from_ledger(flow)
    before = snapshot(flow.database)
    transport.release.set()
    thread.join(5)

    assert errors == []
    assert results == [TransactionState.VALIDATED_SUCCESS]
    assert snapshot(flow.database) == before
    assert len(transport.submitted) == 1
    assert_one_terminal_effect(flow)


def test_r1_completion_after_lease_expiry_is_benign_when_closed_while_live(
    tmp_path: Path, clock: Clock
) -> None:
    """Decision: liveness is judged when finalization closed the attempt, not at the late call.
    The no-op writes nothing, so the late call time cannot weaken fencing."""
    flow, lease = leased_flow(tmp_path)
    clock.at(LEASE_SECONDS - 2)
    finalize_from_ledger(flow)
    clock.at(LEASE_SECONDS + 15)
    before = snapshot(flow.database)

    late_completion(flow, lease)

    assert snapshot(flow.database) == before
    assert_one_terminal_effect(flow)


def test_r1_restart_preserves_the_benign_outcome(tmp_path: Path, clock: Clock) -> None:
    flow, lease = leased_flow(tmp_path)
    clock.at(LEASE_SECONDS - 2)
    finalize_from_ledger(flow)
    late_completion(flow, lease)
    reopened = PaymentJournal(flow.database)
    reopened.finish_submission(
        OPERATION,
        lease,
        disposition=SubmissionDisposition.TIMEOUT,
        next_state=TransactionState.UNKNOWN,
        provisional_status="late timeout after restart",
        transaction_hash=reopened.get(OPERATION).transaction_hash,
    )
    assert_one_terminal_effect(flow)
    with pytest.raises(JournalConflict, match="cannot submit from VALIDATED_SUCCESS"):
        reopened.begin_submission(OPERATION, resubmission=True)


# --- still fenced ------------------------------------------------------------------------------


def test_r1_finalization_after_lease_expiry_keeps_owner_fenced(
    tmp_path: Path, clock: Clock
) -> None:
    flow, lease = leased_flow(tmp_path)
    clock.at(LEASE_SECONDS + 1)  # A lost ownership before F finalized
    finalize_from_ledger(flow)
    before = snapshot(flow.database)

    with pytest.raises(JournalConflict, match="not owned"):
        late_completion(flow, lease)

    assert snapshot(flow.database) == before
    assert_one_terminal_effect(flow)


def test_r1_expired_lease_abandoned_before_finalization_keeps_owner_fenced(
    tmp_path: Path, clock: Clock
) -> None:
    flow, lease = leased_flow(tmp_path)
    clock.at(LEASE_SECONDS + 1)
    # A reconciliation before inclusion durably abandons the expired attempt.
    assert worker(flow).reconcile(OPERATION).state is not TransactionState.SUBMITTED
    (abandoned,) = flow.journal.attempts(OPERATION)
    assert abandoned.provisional_status == "SUBMISSION_LEASE_EXPIRED_OUTCOME_UNKNOWN"
    finalize_from_ledger(flow)
    before = snapshot(flow.database)

    with pytest.raises(JournalConflict, match="not owned"):
        late_completion(flow, lease)

    assert snapshot(flow.database) == before


def test_r1_unrelated_stale_owner_is_fenced(tmp_path: Path, clock: Clock) -> None:
    flow, lease = leased_flow(tmp_path)
    clock.at(LEASE_SECONDS - 2)
    finalize_from_ledger(flow)
    before = snapshot(flow.database)
    for other in (
        SubmissionLease(lease.attempt_number, "unrelated-owner", lease.expires_at),
        SubmissionLease(lease.attempt_number + 1, lease.owner_token, lease.expires_at),
        SubmissionLease(lease.attempt_number, lease.owner_token, "2099-01-01T00:00:00Z"),
    ):
        with pytest.raises(JournalConflict, match="not owned"):
            late_completion(flow, other)
    assert snapshot(flow.database) == before


@pytest.mark.parametrize(
    "transaction_hash",
    [None, "", "AB" * 32],
    ids=["no-identity", "empty-identity", "different-signed-hash"],
)
def test_r1_inconsistent_signed_identity_is_fenced(
    tmp_path: Path, clock: Clock, transaction_hash: str | None
) -> None:
    flow, lease = leased_flow(tmp_path)
    clock.at(LEASE_SECONDS - 2)
    finalize_from_ledger(flow)
    before = snapshot(flow.database)
    with pytest.raises(JournalConflict, match="not owned"):
        late_completion(flow, lease, transaction_hash=transaction_hash)
    assert snapshot(flow.database) == before


def test_r1_other_payment_identity_is_fenced(tmp_path: Path, clock: Clock) -> None:
    flow, lease = leased_flow(tmp_path)
    request = payment_request("operation-other", sequence=500)
    other = flow.journal.persist_signed(PreparedPayment(request), signed_blob(request))
    clock.at(LEASE_SECONDS - 2)
    finalize_from_ledger(flow)
    before = snapshot(flow.database)
    for operation_id, transaction_hash in (
        ("operation-other", flow.journal.get(OPERATION).transaction_hash),
        ("operation-other", other.transaction_hash),
        (OPERATION, other.transaction_hash),
    ):
        with pytest.raises(JournalConflict, match="not owned"):
            late_completion(
                flow, lease, operation_id=operation_id, transaction_hash=transaction_hash
            )
    assert snapshot(flow.database) == before


TAMPERING = {
    "outbox-event-missing": "DELETE FROM xrpl_settlement_outbox",
    "settlement-status-cleared": "UPDATE xrpl_payment_transactions SET settlement_status = NULL",
    "terminal-state-is-expiry": (
        "UPDATE xrpl_payment_transactions SET state = 'EXPIRED_NOT_INCLUDED', "
        "settlement_status = NULL"
    ),
    "payment-nonterminal": (
        "UPDATE xrpl_payment_transactions SET state = 'PENDING_CONFIRMATION', "
        "settlement_status = NULL"
    ),
    "evidence-unverified": (
        "UPDATE xrpl_payment_transactions SET evidence_verification_version = 0"
    ),
    "evidence-missing": "UPDATE xrpl_payment_transactions SET final_evidence = NULL",
    "terminal-write-time-differs": (
        "UPDATE xrpl_payment_transactions SET updated_at = '2001-01-01T00:00:00Z'"
    ),
    "attempt-closed-otherwise": (
        "UPDATE xrpl_submission_attempts SET provisional_status = "
        "'SUBMISSION_LEASE_EXPIRED_OUTCOME_UNKNOWN'"
    ),
    "attempt-has-disposition": ("UPDATE xrpl_submission_attempts SET disposition = 'ACKNOWLEDGED'"),
    "signed-bytes-altered": ("UPDATE xrpl_payment_transactions SET signed_transaction = X'1200'"),
}


@pytest.mark.parametrize("statement", TAMPERING.values(), ids=TAMPERING.keys())
def test_r1_inconsistent_terminal_history_is_fenced(
    tmp_path: Path, clock: Clock, statement: str
) -> None:
    flow, lease = leased_flow(tmp_path)
    clock.at(LEASE_SECONDS - 2)
    finalize_from_ledger(flow)
    persisted_hash = flow.journal.get(OPERATION).transaction_hash
    with sqlite3.connect(flow.database) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(statement)
    before = snapshot(flow.database)

    with pytest.raises(JournalConflict, match="not owned"):
        late_completion(flow, lease, transaction_hash=persisted_hash)

    assert snapshot(flow.database) == before


def test_r1_validated_failure_is_benign_without_an_event(tmp_path: Path, clock: Clock) -> None:
    flow, lease = leased_flow(tmp_path)
    clock.at(LEASE_SECONDS - 2)
    flow.rippled.include(
        flow.signed, ledger_index=VALIDATED_LEDGER + 3, result="tecUNFUNDED_PAYMENT"
    )
    flow.rippled.advance(VALIDATED_LEDGER + 3)
    assert worker(flow).reconcile(OPERATION).state is TransactionState.VALIDATED_FAILURE
    before = snapshot(flow.database)

    late_completion(flow, lease)

    assert snapshot(flow.database) == before
    assert PaymentJournal(flow.database).pending_settlement_events() == ()


def test_r1_nonfinalized_live_attempt_still_requires_its_owner(tmp_path: Path) -> None:
    flow, lease = leased_flow(tmp_path)
    with pytest.raises(JournalConflict, match="not owned"):
        late_completion(flow, SubmissionLease(lease.attempt_number, "intruder", lease.expires_at))
    late_completion(flow, lease)
    (attempt,) = flow.journal.attempts(OPERATION)
    assert attempt.disposition is SubmissionDisposition.ACKNOWLEDGED


# --- multiple finalizers and an eight-way submit race -----------------------------------------


def test_r1_eight_submitters_and_three_finalizers_keep_one_terminal_effect(
    tmp_path: Path, clock: Clock
) -> None:
    flow = build_flow(tmp_path)
    flow.database = tmp_path / "journal.sqlite3"
    transport = BlockingTransport()
    submitters = [worker(flow, transport=transport) for _ in range(8)]
    barrier = threading.Barrier(len(submitters))
    results: list[TransactionState] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def submit(service: PaymentService) -> None:
        try:
            barrier.wait()
            state = service.submit(OPERATION).state
            with lock:
                results.append(state)
        except BaseException as exc:  # pragma: no cover - asserted below
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=submit, args=(service,)) for service in submitters]
    for thread in threads:
        thread.start()
    assert transport.entered.wait(5)
    for _ in range(200):  # the seven non-owners return without touching the transport
        with lock:
            if len(results) + len(errors) == 7:
                break
        threading.Event().wait(0.01)
    assert errors == [] and results == [TransactionState.SUBMITTED] * 7

    clock.at(LEASE_SECONDS - 2)
    flow.rippled.include(flow.signed, ledger_index=VALIDATED_LEDGER + 3, delivered=delivered())
    flow.rippled.advance(VALIDATED_LEDGER + 3)
    finalizers = [worker(flow) for _ in range(3)]
    finalized: list[TransactionState] = []
    finalizer_threads = [
        threading.Thread(target=lambda f=f: finalized.append(f.reconcile(OPERATION).state))
        for f in finalizers
    ]
    for thread in finalizer_threads:
        thread.start()
    for thread in finalizer_threads:
        thread.join(5)
    assert finalized == [TransactionState.VALIDATED_SUCCESS] * 3

    transport.release.set()
    for thread in threads:
        thread.join(5)
    assert errors == []
    assert results[7:] == [TransactionState.VALIDATED_SUCCESS]
    assert len(transport.submitted) == 1
    assert_one_terminal_effect(flow)


# --- kill in claim -----------------------------------------------------------------------------


def claim_then_die(database: str, operation_id: str, results: Any) -> None:
    journal = PaymentJournal(database)
    lease = journal.begin_submission(operation_id, resubmission=False)
    results.put(None if lease is None else (lease.attempt_number, lease.owner_token))
    results.close()
    results.join_thread()  # flush the report, then die without any cleanup
    os._exit(9)  # killed while holding the lease, before any transport response


def test_r1_kill_in_claim_then_finalization_keeps_one_effect(tmp_path: Path) -> None:
    flow = build_flow(tmp_path)
    flow.database = tmp_path / "journal.sqlite3"
    flow.journal.record_submission_anchor(OPERATION, flow.trusted.validated_ledger(OWNER.account))
    context = get_context("spawn")
    results = context.Queue()
    process = context.Process(target=claim_then_die, args=(str(flow.database), OPERATION, results))
    process.start()
    process.join(20)
    assert process.exitcode == 9
    claimed = results.get(timeout=5)
    assert claimed is not None

    # The dead owner's live lease still fences others from the transport.
    assert worker(flow).reconcile(OPERATION, resubmit_if_pending=True).state is (
        TransactionState.SUBMITTED
    )
    finalize_from_ledger(flow)
    assert_one_terminal_effect(flow)
    assert flow.offline.submitted == []
