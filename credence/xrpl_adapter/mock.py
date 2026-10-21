"""Deterministic injected transport for X0 offline adversarial tests."""

from __future__ import annotations

import threading

from credence.xrpl_adapter.types import (
    LookupResult,
    LookupUnavailable,
    SettlementEvent,
    SubmissionResponse,
    SubmissionTransportError,
    ValidatedLedgerObservation,
)


class DeterministicMockTransport:
    """Submission, lookup, and validated-ledger seams with no network access."""

    offline_only = True

    def __init__(self, *, ledger: ValidatedLedgerObservation) -> None:
        self._lock = threading.Lock()
        self._submission_outcomes: list[SubmissionResponse | Exception] = []
        self._lookups: dict[str, LookupResult | Exception] = {}
        self._ledger: ValidatedLedgerObservation | Exception = ledger
        self.submitted: list[tuple[bytes, str]] = []

    def queue_submission(self, outcome: SubmissionResponse | Exception) -> None:
        with self._lock:
            self._submission_outcomes.append(outcome)

    def set_lookup(self, transaction_hash: str, outcome: LookupResult | Exception) -> None:
        with self._lock:
            self._lookups[transaction_hash] = outcome

    def set_ledger(self, outcome: ValidatedLedgerObservation | Exception) -> None:
        with self._lock:
            self._ledger = outcome

    def submit(self, signed_transaction: bytes, transaction_hash: str) -> SubmissionResponse:
        with self._lock:
            self.submitted.append((signed_transaction, transaction_hash))
            if not self._submission_outcomes:
                raise SubmissionTransportError("no deterministic submission outcome configured")
            outcome = self._submission_outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def lookup(self, transaction_hash: str) -> LookupResult:
        with self._lock:
            outcome = self._lookups.get(transaction_hash)
        if outcome is None:
            raise LookupUnavailable("no deterministic lookup outcome configured")
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def validated_ledger(self, account: str) -> ValidatedLedgerObservation:
        with self._lock:
            outcome = self._ledger
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class DeterministicSettlementConsumer:
    """Idempotent offline outbox consumer with deterministic crash injection."""

    def __init__(self) -> None:
        self.applied: dict[str, bytes] = {}
        self._failures_before_apply = 0
        self._failures_after_apply = 0

    def fail_before_apply(self, count: int = 1) -> None:
        self._failures_before_apply += count

    def fail_after_apply(self, count: int = 1) -> None:
        self._failures_after_apply += count

    def apply(self, event: SettlementEvent) -> None:
        if self._failures_before_apply:
            self._failures_before_apply -= 1
            raise RuntimeError("injected failure before durable consumer application")
        prior = self.applied.setdefault(event.event_id, event.payload)
        if prior != event.payload:
            raise RuntimeError("event identity was replayed with contradictory payload")
        if self._failures_after_apply:
            self._failures_after_apply -= 1
            raise RuntimeError("injected failure after durable consumer application")
