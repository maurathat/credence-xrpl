"""X1a: payment outcome classification and the durable idempotent settlement-event consumer.

Both build on the accepted X0t journal without changing it: no new transaction identity, retry
path or settlement-effect channel.

``classify_payment`` reads one journal record and names exactly one of seven outcomes:

| Outcome | Journal record |
| --- | --- |
| ``NOT_SUBMITTED`` | ``PREPARED``: signed bytes persisted, never broadcast |
| ``SUBMISSION_PENDING`` | ``SUBMITTED`` or ``PENDING_CONFIRMATION`` |
| ``VALIDATED_SUCCESS`` | ``VALIDATED_SUCCESS`` with ``XRPL_SETTLED`` |
| ``VALIDATED_FAILURE`` | ``VALIDATED_FAILURE`` whose evidence result is not ``tesSUCCESS`` |
| ``VALIDATED_UNEXPECTED_DELIVERY`` | ``VALIDATED_UNEXPECTED_DELIVERY`` (journal schema version 3, X1c C1) whose evidence result is ``tesSUCCESS``: funds moved, but not as authorized. A transferred-funds exception requiring operator reconciliation, not an unsuccessful transaction |
| ``EXPIRED_NOT_INCLUDED`` | ``EXPIRED_NOT_INCLUDED``, proven by anchored complete history |
| ``UNKNOWN_RECONCILIATION_REQUIRED`` | ``UNKNOWN``, or any record whose stored evidence does not support its state |

The journal state and the stored evidence envelope's ``meta.TransactionResult`` must agree: an
unexpected delivery must show ``tesSUCCESS`` and a ledger failure must not. Any disagreement is
``UNKNOWN_RECONCILIATION_REQUIRED``.

``SqliteSettlementEventConsumer`` is the single settlement-effect authority path for live use. It
applies an X0t outbox event in its own ``BEGIN IMMEDIATE`` transaction, keyed by the outbox's
deterministic ``event_id``, and runs the caller's effect inside that same transaction. A replay of
the same event is a no-op; a different event for the same event ID, operation, payment
authorization or transaction hash is refused. The X0t ``claim_xrpl_settlement`` is not an
authority: since X1c it is refused (``LEGACY_SETTLEMENT_CLAIM_RETIRED``) whenever an outbox event
exists.

Owner: XRPL Integration (X1a, authorized by Maura).
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from enum import Enum
from pathlib import Path

from credence.xrpl_adapter.types import PaymentRecord, SettlementEvent, TransactionState

UNEXPECTED_DELIVERY_RESULTS = frozenset({"DELIVERED_ASSET_MISMATCH", "DELIVERED_AMOUNT_MISMATCH"})
EVENT_TYPE = "XRPL_PAYMENT_VALIDATED"


class PaymentOutcome(Enum):
    NOT_SUBMITTED = "NOT_SUBMITTED"
    SUBMISSION_PENDING = "SUBMISSION_PENDING"
    VALIDATED_SUCCESS = "VALIDATED_SUCCESS"
    VALIDATED_FAILURE = "VALIDATED_FAILURE"
    VALIDATED_UNEXPECTED_DELIVERY = "VALIDATED_UNEXPECTED_DELIVERY"
    EXPIRED_NOT_INCLUDED = "EXPIRED_NOT_INCLUDED"
    UNKNOWN_RECONCILIATION_REQUIRED = "UNKNOWN_RECONCILIATION_REQUIRED"


def _evidence_result(evidence: bytes | None) -> str | None:
    if not evidence:
        return None
    try:
        document = json.loads(evidence)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    meta = document.get("meta") if isinstance(document, dict) else None
    result = meta.get("TransactionResult") if isinstance(meta, dict) else None
    return result if isinstance(result, str) else None


def classify_payment(record: PaymentRecord) -> PaymentOutcome:
    state = record.state
    if state is TransactionState.PREPARED:
        return PaymentOutcome.NOT_SUBMITTED
    if state in (TransactionState.SUBMITTED, TransactionState.PENDING_CONFIRMATION):
        return PaymentOutcome.SUBMISSION_PENDING
    if state is TransactionState.EXPIRED_NOT_INCLUDED:
        return PaymentOutcome.EXPIRED_NOT_INCLUDED
    if state is TransactionState.VALIDATED_SUCCESS:
        if (
            record.settlement_status == "XRPL_SETTLED"
            and _evidence_result(record.final_evidence) == "tesSUCCESS"
        ):
            return PaymentOutcome.VALIDATED_SUCCESS
        return PaymentOutcome.UNKNOWN_RECONCILIATION_REQUIRED
    ledger_result = _evidence_result(record.final_evidence)
    unexpected = record.final_result in UNEXPECTED_DELIVERY_RESULTS
    if state is TransactionState.VALIDATED_UNEXPECTED_DELIVERY:
        if ledger_result == "tesSUCCESS" and unexpected:
            return PaymentOutcome.VALIDATED_UNEXPECTED_DELIVERY
        return PaymentOutcome.UNKNOWN_RECONCILIATION_REQUIRED
    if state is TransactionState.VALIDATED_FAILURE:
        if ledger_result is not None and ledger_result != "tesSUCCESS" and not unexpected:
            return PaymentOutcome.VALIDATED_FAILURE
        return PaymentOutcome.UNKNOWN_RECONCILIATION_REQUIRED
    return PaymentOutcome.UNKNOWN_RECONCILIATION_REQUIRED


class ConflictingSettlementEvent(RuntimeError):
    """An event contradicts one already applied, or its payload contradicts its own fields."""


SettlementEffect = Callable[[sqlite3.Connection, SettlementEvent], None]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS consumed_settlement_events (
    event_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL UNIQUE,
    payment_authorization_id TEXT NOT NULL UNIQUE,
    transaction_hash TEXT NOT NULL UNIQUE,
    payload BLOB NOT NULL,
    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
)
"""


class SqliteSettlementEventConsumer:
    """Apply each X0t outbox event at most once, in the destination's own durable transaction."""

    def __init__(self, database: str | Path, effect: SettlementEffect) -> None:
        self._database = Path(database)
        self._database.parent.mkdir(parents=True, exist_ok=True)
        self._effect = effect
        with self._write() as connection:
            connection.execute(_SCHEMA)
        os.chmod(self._database, 0o600)

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._database, timeout=30, isolation_level=None)
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def apply(self, event: SettlementEvent) -> None:
        _check_payload(event)
        with self._write() as connection:
            row = connection.execute(
                "SELECT operation_id, payment_authorization_id, transaction_hash, payload "
                "FROM consumed_settlement_events WHERE event_id = ?",
                (event.event_id,),
            ).fetchone()
            if row is not None:
                if tuple(row) != (
                    event.operation_id,
                    event.payment_authorization_id,
                    event.transaction_hash,
                    event.payload,
                ):
                    raise ConflictingSettlementEvent(f"{event.event_id}: contradictory replay")
                return
            try:
                connection.execute(
                    "INSERT INTO consumed_settlement_events (event_id, operation_id, "
                    "payment_authorization_id, transaction_hash, payload) VALUES (?, ?, ?, ?, ?)",
                    (
                        event.event_id,
                        event.operation_id,
                        event.payment_authorization_id,
                        event.transaction_hash,
                        event.payload,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictingSettlementEvent(
                    f"{event.event_id}: operation, authorization or hash already settled"
                ) from exc
            self._effect(connection, event)

    def applied(self, event_id: str) -> bool:
        connection = sqlite3.connect(self._database, timeout=30)
        try:
            row = connection.execute(
                "SELECT 1 FROM consumed_settlement_events WHERE event_id = ?", (event_id,)
            ).fetchone()
        finally:
            connection.close()
        return row is not None


def _check_payload(event: SettlementEvent) -> None:
    try:
        payload = json.loads(event.payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConflictingSettlementEvent(f"{event.event_id}: payload is not JSON") from exc
    expected = {
        "event_type": EVENT_TYPE,
        "result": "tesSUCCESS",
        "operation_id": event.operation_id,
        "payment_authorization_id": event.payment_authorization_id,
        "transaction_hash": event.transaction_hash,
    }
    if not isinstance(payload, dict) or any(payload.get(k) != v for k, v in expected.items()):
        raise ConflictingSettlementEvent(f"{event.event_id}: payload contradicts the event")
