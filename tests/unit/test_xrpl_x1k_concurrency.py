"""X1k: pre-expiry evidence validation and durable resubmission ownership."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import replace
from multiprocessing import get_context
from pathlib import Path
from typing import Any

import pytest
from test_xrpl_payment_readiness import (
    payment_request,
    service_for,
    signed_blob,
    trusted_ledger,
)
from test_xrpl_x1a_flow import build_flow
from x1a_world import OPERATION, OWNER

from credence.xrpl_adapter.journal import PaymentJournal
from credence.xrpl_adapter.mock import DeterministicMockTransport
from credence.xrpl_adapter.service import PaymentService
from credence.xrpl_adapter.types import (
    JournalConflict,
    LookupDisposition,
    LookupResult,
    SubmissionDisposition,
    SubmissionResponse,
    SubmissionTimeout,
    TransactionState,
    ValidatedLedgerObservation,
)


class StaticObserver:
    def __init__(self, observation: ValidatedLedgerObservation) -> None:
        self.observation = observation

    def validated_ledger(self, account: str) -> ValidatedLedgerObservation:
        return self.observation


def restarted_service(
    flow: Any,
    *,
    journal: PaymentJournal | None = None,
    transport: Any | None = None,
    observer: Any | None = None,
) -> PaymentService:
    return PaymentService(
        journal=journal or flow.journal,
        preparer=flow.service._preparer,
        inspector=flow.service._inspector,
        transport=transport or flow.offline,
        lookup=flow.trusted,
        ledger_observer=observer or flow.trusted,
    )


def timed_out_flow(tmp_path: Path) -> Any:
    flow = build_flow(tmp_path)
    flow.offline.queue_submission(SubmissionTimeout("response lost"))
    assert flow.service.submit(OPERATION).state is TransactionState.UNKNOWN
    flow.rippled.advance(105)
    return flow


@pytest.mark.parametrize(
    ("change", "detail"),
    [
        ({"network_id": 1}, "LEDGER_SOURCE_IDENTITY_DISAGREES_WITH_SUBMISSION_ANCHOR"),
        (
            {"source_id": "different-source"},
            "LEDGER_SOURCE_IDENTITY_DISAGREES_WITH_SUBMISSION_ANCHOR",
        ),
        (
            {"node_public_key": "different-node"},
            "LEDGER_SOURCE_IDENTITY_DISAGREES_WITH_SUBMISSION_ANCHOR",
        ),
        ({"build_version": "3.4.1"}, "LEDGER_SOURCE_IDENTITY_DISAGREES_WITH_SUBMISSION_ANCHOR"),
        (
            {"ledger_index": 99, "ledger_hash": f"{99:064X}"},
            "VALIDATED_LEDGER_CONTRADICTS_SUBMISSION_ANCHOR",
        ),
    ],
)
def test_preexpiry_identity_contradiction_never_rebroadcasts(
    tmp_path: Path, change: dict[str, object], detail: str
) -> None:
    flow = timed_out_flow(tmp_path)
    observation = flow.trusted.validated_ledger(OWNER.account)
    flow.offline.queue_submission(SubmissionResponse(True, "tesSUCCESS", "must stay unused"))

    result = restarted_service(
        flow, observer=StaticObserver(replace(observation, **change))
    ).reconcile(OPERATION, resubmit_if_pending=True)

    assert (result.state, result.last_provisional_status) == (TransactionState.UNKNOWN, detail)
    assert len(flow.offline.submitted) == 1


def test_preexpiry_changed_sender_sequence_never_rebroadcasts(tmp_path: Path) -> None:
    flow = timed_out_flow(tmp_path)
    flow.rippled.accounts[OWNER.account]["Sequence"] += 1
    flow.offline.queue_submission(SubmissionResponse(True, "tesSUCCESS", "must stay unused"))

    result = flow.service.reconcile(OPERATION, resubmit_if_pending=True)

    assert (result.state, result.last_provisional_status) == (
        TransactionState.UNKNOWN,
        "SENDER_SEQUENCE_OR_TICKET_CONSUMPTION_UNRESOLVED",
    )
    assert len(flow.offline.submitted) == 1


def test_preexpiry_consumed_ticket_never_rebroadcasts(tmp_path: Path) -> None:
    request = replace(payment_request(), sequence=None, ticket_sequence=77)
    transport = DeterministicMockTransport(ledger=trusted_ledger())
    transport.queue_submission(SubmissionTimeout("response lost"))
    service, _ = service_for(tmp_path / "ticket-journal.sqlite3", transport)
    record = service.prepare_and_persist(request, signed_blob(request))
    assert service.submit(record.operation_id).state is TransactionState.UNKNOWN
    transport.set_lookup(record.transaction_hash, LookupResult(LookupDisposition.NOT_FOUND))
    transport.set_ledger(trusted_ledger(21, available_tickets=()))
    transport.queue_submission(SubmissionResponse(True, "tesSUCCESS", "must stay unused"))

    result = service.reconcile(record.operation_id, resubmit_if_pending=True)

    assert (result.state, result.last_provisional_status) == (
        TransactionState.UNKNOWN,
        "SENDER_SEQUENCE_OR_TICKET_CONSUMPTION_UNRESOLVED",
    )
    assert len(transport.submitted) == 1


def test_absent_durable_anchor_identity_never_rebroadcasts(tmp_path: Path) -> None:
    flow = timed_out_flow(tmp_path)
    with sqlite3.connect(tmp_path / "journal.sqlite3") as connection:
        connection.execute(
            "UPDATE xrpl_payment_transactions SET submission_anchor_node_public_key = NULL"
        )
    flow.offline.queue_submission(SubmissionResponse(True, "tesSUCCESS", "must stay unused"))

    result = flow.service.reconcile(OPERATION, resubmit_if_pending=True)

    assert (result.state, result.last_provisional_status) == (
        TransactionState.UNKNOWN,
        "TRUSTED_SUBMISSION_ANCHOR_IDENTITY_MISSING",
    )
    assert len(flow.offline.submitted) == 1


def test_anchor_network_must_match_persisted_payment_network(tmp_path: Path) -> None:
    flow = timed_out_flow(tmp_path)
    observation = replace(flow.trusted.validated_ledger(OWNER.account), network_id=1)
    with sqlite3.connect(tmp_path / "journal.sqlite3") as connection:
        connection.execute("UPDATE xrpl_payment_transactions SET submission_anchor_network_id = 1")
    flow.offline.queue_submission(SubmissionResponse(True, "tesSUCCESS", "must stay unused"))

    result = restarted_service(flow, observer=StaticObserver(observation)).reconcile(
        OPERATION, resubmit_if_pending=True
    )

    assert (result.state, result.last_provisional_status) == (
        TransactionState.UNKNOWN,
        "SUBMISSION_ANCHOR_NETWORK_CONTRADICTS_PAYMENT",
    )
    assert len(flow.offline.submitted) == 1


class BlockingTransport:
    offline_only = True

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.submitted: list[tuple[bytes, str]] = []

    def submit(self, signed_transaction: bytes, transaction_hash: str) -> SubmissionResponse:
        self.submitted.append((signed_transaction, transaction_hash))
        self.entered.set()
        if not self.release.wait(5):
            raise AssertionError("test did not release the submission")
        return SubmissionResponse(True, "tesSUCCESS", "same transaction")


def claim_from_process(database: str, operation_id: str, start: Any, results: Any) -> None:
    journal = PaymentJournal(database)
    if not start.wait(5):
        raise AssertionError("claim processes did not start")
    lease = journal.begin_submission(operation_id, resubmission=True)
    results.put(None if lease is None else lease.attempt_number)


def test_two_journals_allow_only_one_active_resubmission_owner(tmp_path: Path) -> None:
    flow = timed_out_flow(tmp_path)
    transport = BlockingTransport()
    first = restarted_service(
        flow,
        journal=PaymentJournal(tmp_path / "journal.sqlite3"),
        transport=transport,
    )
    second = restarted_service(
        flow,
        journal=PaymentJournal(tmp_path / "journal.sqlite3"),
        transport=transport,
    )
    results: list[TransactionState] = []
    errors: list[BaseException] = []

    def run_first() -> None:
        try:
            results.append(first.reconcile(OPERATION, resubmit_if_pending=True).state)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    worker = threading.Thread(target=run_first)
    worker.start()
    assert transport.entered.wait(5)
    second_result = second.reconcile(OPERATION, resubmit_if_pending=True)
    repeated_result = second.reconcile(OPERATION, resubmit_if_pending=True)
    transport.release.set()
    worker.join()

    record = flow.journal.get(OPERATION)
    assert errors == []
    assert second_result.state is TransactionState.SUBMITTED
    assert repeated_result.state is TransactionState.SUBMITTED
    assert results == [TransactionState.PENDING_CONFIRMATION]
    assert transport.submitted == [(record.signed_transaction, record.transaction_hash)]
    attempts = flow.journal.attempts(OPERATION)
    assert [attempt.disposition for attempt in attempts] == [
        SubmissionDisposition.TIMEOUT,
        SubmissionDisposition.ACKNOWLEDGED,
    ]


def test_submission_claim_is_durable_across_processes(tmp_path: Path) -> None:
    timed_out_flow(tmp_path)
    context = get_context("spawn")
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=claim_from_process,
            args=(str(tmp_path / "journal.sqlite3"), OPERATION, start, results),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(10)
        assert process.exitcode == 0

    assert sorted((results.get(timeout=2), results.get(timeout=2)), key=str) == [2, None]
    journal = PaymentJournal(tmp_path / "journal.sqlite3")
    attempts = journal.attempts(OPERATION)
    assert [attempt.completed_at is None for attempt in attempts] == [False, True]


def test_only_lease_owner_can_complete_attempt(tmp_path: Path) -> None:
    flow = build_flow(tmp_path)
    flow.journal.record_submission_anchor(OPERATION, flow.trusted.validated_ledger(OWNER.account))
    lease = flow.journal.begin_submission(OPERATION, resubmission=False)
    assert lease is not None

    with pytest.raises(JournalConflict, match="not owned"):
        flow.journal.finish_submission(
            OPERATION,
            replace(lease, owner_token="different-owner"),
            disposition=SubmissionDisposition.ACKNOWLEDGED,
            next_state=TransactionState.PENDING_CONFIRMATION,
            provisional_status="tesSUCCESS",
        )

    flow.journal.finish_submission(
        OPERATION,
        lease,
        disposition=SubmissionDisposition.ACKNOWLEDGED,
        next_state=TransactionState.PENDING_CONFIRMATION,
        provisional_status="tesSUCCESS",
    )
    assert flow.journal.attempts(OPERATION)[0].disposition is SubmissionDisposition.ACKNOWLEDGED


def test_expired_owner_cannot_complete_after_recovery_claim(tmp_path: Path) -> None:
    flow = build_flow(tmp_path)
    flow.journal.record_submission_anchor(OPERATION, flow.trusted.validated_ledger(OWNER.account))
    abandoned = flow.journal.begin_submission(OPERATION, resubmission=False)
    assert abandoned is not None
    with sqlite3.connect(tmp_path / "journal.sqlite3") as connection:
        connection.execute(
            "UPDATE xrpl_submission_attempts SET lease_expires_at = ? WHERE completed_at IS NULL",
            ("2000-01-01T00:00:00Z",),
        )

    recovered = flow.journal.begin_submission(OPERATION, resubmission=True)
    assert recovered is not None
    assert recovered.attempt_number == abandoned.attempt_number + 1
    with pytest.raises(JournalConflict, match="not owned"):
        flow.journal.finish_submission(
            OPERATION,
            abandoned,
            disposition=SubmissionDisposition.ACKNOWLEDGED,
            next_state=TransactionState.PENDING_CONFIRMATION,
            provisional_status="stale owner",
        )

    flow.journal.finish_submission(
        OPERATION,
        recovered,
        disposition=SubmissionDisposition.ACKNOWLEDGED,
        next_state=TransactionState.PENDING_CONFIRMATION,
        provisional_status="tesSUCCESS",
    )
    assert [attempt.disposition for attempt in flow.journal.attempts(OPERATION)] == [
        SubmissionDisposition.TRANSPORT_ERROR,
        SubmissionDisposition.ACKNOWLEDGED,
    ]


def test_expired_crash_lease_is_recoverable_with_same_transaction(tmp_path: Path) -> None:
    flow = build_flow(tmp_path)
    flow.offline.queue_submission(RuntimeError("simulated process death after claim"))
    with pytest.raises(RuntimeError, match="process death"):
        flow.service.submit(OPERATION)
    flow.rippled.advance(105)

    # The live lease prevents another process from using the transport.
    before = len(flow.offline.submitted)
    assert (
        flow.service.reconcile(OPERATION, resubmit_if_pending=True).state
        is TransactionState.SUBMITTED
    )
    assert len(flow.offline.submitted) == before

    # Simulate passage beyond the documented lease bound without sleeping.
    with sqlite3.connect(tmp_path / "journal.sqlite3") as connection:
        connection.execute(
            "UPDATE xrpl_submission_attempts SET lease_expires_at = ? WHERE completed_at IS NULL",
            ("2000-01-01T00:00:00Z",),
        )
    flow.offline.queue_submission(SubmissionResponse(True, "tesSUCCESS", "same transaction"))
    recovered = flow.service.reconcile(OPERATION, resubmit_if_pending=True)

    record = flow.journal.get(OPERATION)
    assert recovered.state is TransactionState.PENDING_CONFIRMATION
    assert set(flow.offline.submitted) == {(record.signed_transaction, record.transaction_hash)}
    assert [attempt.disposition for attempt in flow.journal.attempts(OPERATION)] == [
        SubmissionDisposition.TRANSPORT_ERROR,
        SubmissionDisposition.ACKNOWLEDGED,
    ]
