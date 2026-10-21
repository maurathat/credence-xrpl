"""SQLite transaction journal for reliable XRPL submission and recovery.

Every mutating method owns one short transaction. Network calls are made by the
service only after these methods return, so no SQLite write transaction spans a
submission or lookup.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from credence.xrpl_adapter.preparation import transaction_hash
from credence.xrpl_adapter.types import (
    TERMINAL_STATES,
    Asset,
    DuplicateFinancialOperation,
    JournalConflict,
    PaymentRecord,
    PreparedPayment,
    SettlementEvent,
    SubmissionAttempt,
    SubmissionDisposition,
    TransactionHashMismatch,
    TransactionState,
    TransactionType,
    ValidatedLedgerObservation,
)

_STATE_VALUES = ", ".join(f"'{state.value}'" for state in TransactionState)
_DISPOSITION_VALUES = ", ".join(f"'{disposition.value}'" for disposition in SubmissionDisposition)

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS xrpl_payment_transactions (
    operation_id TEXT PRIMARY KEY,
    payment_authorization_id TEXT NOT NULL UNIQUE,
    transaction_type TEXT NOT NULL CHECK (transaction_type = 'Payment'),
    account TEXT NOT NULL,
    destination TEXT NOT NULL,
    asset_label TEXT NOT NULL CHECK (asset_label = 'tRLUSD'),
    asset_currency_code TEXT NOT NULL,
    asset_issuer TEXT NOT NULL,
    amount_micro_units INTEGER NOT NULL CHECK (amount_micro_units > 0),
    fee_drops INTEGER NOT NULL CHECK (fee_drops > 0),
    network_id INTEGER NOT NULL,
    sequence INTEGER,
    ticket_sequence INTEGER,
    earliest_ledger_sequence INTEGER NOT NULL,
    last_ledger_sequence INTEGER NOT NULL,
    signed_transaction BLOB NOT NULL,
    transaction_hash TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK (state IN ({_STATE_VALUES})),
    last_provisional_status TEXT,
    final_result TEXT,
    final_detail TEXT,
    final_evidence BLOB,
    validated_ledger_index INTEGER,
    validated_ledger_hash TEXT,
    verified_delivered_amount_micro_units INTEGER,
    evidence_verification_version INTEGER NOT NULL DEFAULT 0,
    submission_anchor_ledger_index INTEGER,
    submission_anchor_ledger_hash TEXT,
    submission_anchor_source_id TEXT,
    settlement_status TEXT CHECK (settlement_status IS NULL OR settlement_status = 'XRPL_SETTLED'),
    settlement_effect_claimed INTEGER NOT NULL DEFAULT 0
        CHECK (settlement_effect_claimed IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK ((sequence IS NULL) != (ticket_sequence IS NULL)),
    CHECK (last_ledger_sequence >= earliest_ledger_sequence),
    UNIQUE (account, sequence),
    UNIQUE (account, ticket_sequence)
);
CREATE TABLE IF NOT EXISTS xrpl_submission_attempts (
    operation_id TEXT NOT NULL REFERENCES xrpl_payment_transactions(operation_id),
    attempt_number INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    disposition TEXT CHECK (disposition IN ({_DISPOSITION_VALUES})),
    provisional_status TEXT,
    PRIMARY KEY (operation_id, attempt_number)
);
CREATE INDEX IF NOT EXISTS xrpl_payment_transactions_by_state
    ON xrpl_payment_transactions(state, updated_at);
CREATE TABLE IF NOT EXISTS xrpl_settlement_outbox (
    event_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL UNIQUE REFERENCES xrpl_payment_transactions(operation_id),
    transaction_hash TEXT NOT NULL UNIQUE,
    payload BLOB NOT NULL,
    delivery_attempts INTEGER NOT NULL DEFAULT 0 CHECK (delivery_attempts >= 0),
    last_attempt_at TEXT,
    delivered_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS xrpl_settlement_outbox_pending
    ON xrpl_settlement_outbox(delivered_at, created_at);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class PaymentJournal:
    """Durable signed-transaction persistence and compare-and-set state changes."""

    def __init__(self, database: str | Path) -> None:
        self._database = Path(database)
        self._database.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            self._migrate(connection)
        os.chmod(self._database, 0o600)

    def _migrate(self, connection: sqlite3.Connection) -> None:
        """Upgrade X0 journals without treating old finality as X0s-verified evidence."""
        columns = {
            cast(str, row[1])
            for row in connection.execute("PRAGMA table_info(xrpl_payment_transactions)")
        }
        additions = {
            "validated_ledger_hash": "TEXT",
            "verified_delivered_amount_micro_units": "INTEGER",
            "evidence_verification_version": "INTEGER NOT NULL DEFAULT 0",
            "submission_anchor_ledger_index": "INTEGER",
            "submission_anchor_ledger_hash": "TEXT",
            "submission_anchor_source_id": "TEXT",
        }
        for name, declaration in additions.items():
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE xrpl_payment_transactions ADD COLUMN {name} {declaration}"
                )
        connection.execute(
            """UPDATE xrpl_payment_transactions
               SET state = ?, settlement_status = NULL, settlement_effect_claimed = 0,
                   last_provisional_status = 'X0S_REVERIFICATION_REQUIRED'
               WHERE state IN (?, ?, ?) AND evidence_verification_version = 0""",
            (
                TransactionState.UNKNOWN.value,
                TransactionState.VALIDATED_SUCCESS.value,
                TransactionState.VALIDATED_FAILURE.value,
                TransactionState.EXPIRED_NOT_INCLUDED.value,
            ),
        )
        connection.execute("PRAGMA user_version = 2")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def persist_signed(self, prepared: PreparedPayment, signed_transaction: bytes) -> PaymentRecord:
        """Atomically persist exact signed bytes in PREPARED state."""
        request = prepared.request
        tx_hash = transaction_hash(signed_transaction)
        timestamp = _now()
        try:
            with self._write() as connection:
                connection.execute(
                    """
                    INSERT INTO xrpl_payment_transactions (
                        operation_id, payment_authorization_id, transaction_type,
                        account, destination, asset_label, asset_currency_code,
                        asset_issuer, amount_micro_units, fee_drops, network_id,
                        sequence, ticket_sequence, earliest_ledger_sequence,
                        last_ledger_sequence, signed_transaction, transaction_hash,
                        state, created_at, updated_at
                    ) VALUES (?, ?, 'Payment', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request.operation_id,
                        request.payment_authorization_id,
                        request.account,
                        request.destination,
                        request.asset.label,
                        request.asset.currency_code,
                        request.asset.issuer,
                        request.amount_micro_units,
                        request.fee_drops,
                        request.network_id,
                        request.sequence,
                        request.ticket_sequence,
                        request.earliest_ledger_sequence,
                        request.last_ledger_sequence,
                        signed_transaction,
                        tx_hash,
                        TransactionState.PREPARED.value,
                        timestamp,
                        timestamp,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateFinancialOperation(
                "operation, payment authorization, signed hash, or account sequence is duplicate"
            ) from exc
        return self.get(request.operation_id)

    def get(self, operation_id: str) -> PaymentRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM xrpl_payment_transactions WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        if row is None:
            raise KeyError(operation_id)
        record = _record(row)
        actual_hash = transaction_hash(record.signed_transaction)
        if actual_hash != record.transaction_hash:
            raise TransactionHashMismatch(
                f"{operation_id}: recorded hash does not match persisted signed bytes"
            )
        return record

    def record_submission_anchor(
        self,
        operation_id: str,
        observation: ValidatedLedgerObservation,
    ) -> PaymentRecord:
        """Persist trusted pre-broadcast ledger and sender state exactly once."""
        timestamp = _now()
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM xrpl_payment_transactions WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row is None:
                raise KeyError(operation_id)
            current = _record(row)
            if current.submission_anchor_ledger_index is not None:
                if (
                    current.submission_anchor_ledger_index == observation.ledger_index
                    and current.submission_anchor_ledger_hash == observation.ledger_hash
                    and current.submission_anchor_source_id == observation.source_id
                ):
                    return current
                raise JournalConflict(f"{operation_id}: contradictory submission anchor")
            if current.state is not TransactionState.PREPARED:
                raise JournalConflict(f"{operation_id}: cannot anchor from {current.state.value}")
            if not observation.has_trusted_identity():
                raise JournalConflict(f"{operation_id}: trusted ledger identity is missing")
            if observation.ledger_index >= current.last_ledger_sequence:
                raise JournalConflict(f"{operation_id}: transaction is expired before submission")
            if not observation.account_state_matches(
                account=current.account,
                sequence=current.sequence,
                ticket_sequence=current.ticket_sequence,
            ):
                raise JournalConflict(
                    f"{operation_id}: validated sender state does not match payment"
                )
            connection.execute(
                """UPDATE xrpl_payment_transactions
                   SET submission_anchor_ledger_index = ?, submission_anchor_ledger_hash = ?,
                       submission_anchor_source_id = ?, updated_at = ?
                   WHERE operation_id = ?""",
                (
                    observation.ledger_index,
                    observation.ledger_hash,
                    observation.source_id,
                    timestamp,
                    operation_id,
                ),
            )
        return self.get(operation_id)

    def list_nonterminal(self) -> tuple[PaymentRecord, ...]:
        placeholders = ", ".join("?" for _ in TERMINAL_STATES)
        values = tuple(state.value for state in TERMINAL_STATES)
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT * FROM xrpl_payment_transactions
                    WHERE state NOT IN ({placeholders}) ORDER BY created_at, operation_id""",
                values,
            ).fetchall()
        records = tuple(_record(row) for row in rows)
        for record in records:
            if transaction_hash(record.signed_transaction) != record.transaction_hash:
                raise TransactionHashMismatch(
                    f"{record.operation_id}: recorded hash does not match persisted signed bytes"
                )
        return records

    def begin_submission(self, operation_id: str, *, resubmission: bool) -> int:
        permitted = (
            {TransactionState.PENDING_CONFIRMATION, TransactionState.UNKNOWN}
            if resubmission
            else {TransactionState.PREPARED}
        )
        timestamp = _now()
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM xrpl_payment_transactions WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row is None:
                raise KeyError(operation_id)
            state = TransactionState(cast(str, row["state"]))
            if state not in permitted:
                raise JournalConflict(f"{operation_id}: cannot submit from {state.value}")
            attempt_number = cast(
                int,
                connection.execute(
                    """SELECT COALESCE(MAX(attempt_number), 0) + 1
                       FROM xrpl_submission_attempts WHERE operation_id = ?""",
                    (operation_id,),
                ).fetchone()[0],
            )
            connection.execute(
                """INSERT INTO xrpl_submission_attempts
                   (operation_id, attempt_number, started_at)
                   VALUES (?, ?, ?)""",
                (operation_id, attempt_number, timestamp),
            )
            connection.execute(
                """UPDATE xrpl_payment_transactions
                   SET state = ?, last_provisional_status = NULL, updated_at = ?
                   WHERE operation_id = ?""",
                (TransactionState.SUBMITTED.value, timestamp, operation_id),
            )
        return attempt_number

    def finish_submission(
        self,
        operation_id: str,
        attempt_number: int,
        *,
        disposition: SubmissionDisposition,
        next_state: TransactionState,
        provisional_status: str,
    ) -> None:
        if next_state not in {TransactionState.PENDING_CONFIRMATION, TransactionState.UNKNOWN}:
            raise ValueError("submission can finish only as pending or unknown")
        timestamp = _now()
        with self._write() as connection:
            cursor = connection.execute(
                """UPDATE xrpl_submission_attempts
                   SET completed_at = ?, disposition = ?, provisional_status = ?
                   WHERE operation_id = ? AND attempt_number = ? AND completed_at IS NULL""",
                (
                    timestamp,
                    disposition.value,
                    provisional_status,
                    operation_id,
                    attempt_number,
                ),
            )
            if cursor.rowcount != 1:
                raise JournalConflict(f"{operation_id}: submission attempt already completed")
            cursor = connection.execute(
                """UPDATE xrpl_payment_transactions
                   SET state = ?, last_provisional_status = ?, updated_at = ?
                   WHERE operation_id = ? AND state = ?""",
                (
                    next_state.value,
                    provisional_status,
                    timestamp,
                    operation_id,
                    TransactionState.SUBMITTED.value,
                ),
            )
            if cursor.rowcount != 1:
                raise JournalConflict(f"{operation_id}: submission state changed concurrently")

    def record_reconciliation_state(
        self,
        operation_id: str,
        state: TransactionState,
        detail: str,
    ) -> PaymentRecord:
        if state not in {TransactionState.PENDING_CONFIRMATION, TransactionState.UNKNOWN}:
            raise ValueError("reconciliation observation must be pending or unknown")
        timestamp = _now()
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM xrpl_payment_transactions WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row is None:
                raise KeyError(operation_id)
            current = _record(row)
            if current.state in TERMINAL_STATES:
                if transaction_hash(current.signed_transaction) != current.transaction_hash:
                    raise TransactionHashMismatch(
                        f"{operation_id}: recorded hash does not match persisted signed bytes"
                    )
                return current
            connection.execute(
                """UPDATE xrpl_submission_attempts
                   SET completed_at = ?, disposition = ?, provisional_status = ?
                   WHERE operation_id = ? AND completed_at IS NULL""",
                (
                    timestamp,
                    SubmissionDisposition.TRANSPORT_ERROR.value,
                    "response unavailable; reconciled by transaction hash",
                    operation_id,
                ),
            )
            connection.execute(
                """UPDATE xrpl_payment_transactions
                   SET state = ?, last_provisional_status = ?, updated_at = ?
                   WHERE operation_id = ?""",
                (state.value, detail, timestamp, operation_id),
            )
        return self.get(operation_id)

    def finalize(
        self,
        operation_id: str,
        *,
        state: TransactionState,
        result: str,
        detail: str,
        evidence: bytes,
        validated_ledger_index: int | None,
        validated_ledger_hash: str | None,
        verified_delivered_amount_micro_units: int | None,
        evidence_verification_version: int,
    ) -> PaymentRecord:
        if state not in TERMINAL_STATES:
            raise ValueError("final state is not terminal")
        timestamp = _now()
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM xrpl_payment_transactions WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row is None:
                raise KeyError(operation_id)
            current = _record(row)
            if current.state in TERMINAL_STATES:
                if (
                    current.state == state
                    and current.final_result == result
                    and current.final_detail == detail
                    and current.final_evidence == evidence
                    and current.validated_ledger_index == validated_ledger_index
                    and current.validated_ledger_hash == validated_ledger_hash
                    and current.verified_delivered_amount_micro_units
                    == verified_delivered_amount_micro_units
                    and current.evidence_verification_version == evidence_verification_version
                ):
                    return current
                raise JournalConflict(f"{operation_id}: contradictory terminal evidence")
            connection.execute(
                """UPDATE xrpl_submission_attempts
                   SET completed_at = ?, disposition = ?, provisional_status = ?
                   WHERE operation_id = ? AND completed_at IS NULL""",
                (
                    timestamp,
                    SubmissionDisposition.TRANSPORT_ERROR.value,
                    "response unavailable; final outcome recovered by transaction hash",
                    operation_id,
                ),
            )
            connection.execute(
                """UPDATE xrpl_payment_transactions
                   SET state = ?, final_result = ?, final_detail = ?, final_evidence = ?,
                       validated_ledger_index = ?, validated_ledger_hash = ?,
                       verified_delivered_amount_micro_units = ?,
                       evidence_verification_version = ?, settlement_status = ?, updated_at = ?
                   WHERE operation_id = ?""",
                (
                    state.value,
                    result,
                    detail,
                    evidence,
                    validated_ledger_index,
                    validated_ledger_hash,
                    verified_delivered_amount_micro_units,
                    evidence_verification_version,
                    ("XRPL_SETTLED" if state is TransactionState.VALIDATED_SUCCESS else None),
                    timestamp,
                    operation_id,
                ),
            )
            if state is TransactionState.VALIDATED_SUCCESS:
                if evidence_verification_version != 1:
                    raise JournalConflict(
                        f"{operation_id}: settlement requires X0s-verified evidence"
                    )
                event_id = _settlement_event_id(operation_id, current.transaction_hash)
                payload = _settlement_event_payload(
                    current,
                    ledger_index=validated_ledger_index,
                    ledger_hash=validated_ledger_hash,
                    delivered_amount_micro_units=verified_delivered_amount_micro_units,
                )
                connection.execute(
                    """INSERT INTO xrpl_settlement_outbox (
                           event_id, operation_id, transaction_hash, payload, created_at
                       ) VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(event_id) DO NOTHING""",
                    (event_id, operation_id, current.transaction_hash, payload, timestamp),
                )
        return self.get(operation_id)

    def claim_settlement_effect(self, operation_id: str) -> bool:
        """Claim the single downstream effect allowed by validated success."""
        timestamp = _now()
        with self._write() as connection:
            cursor = connection.execute(
                """UPDATE xrpl_payment_transactions
                   SET settlement_effect_claimed = 1, updated_at = ?
                   WHERE operation_id = ? AND state = ? AND settlement_status = 'XRPL_SETTLED'
                         AND evidence_verification_version = 1
                         AND settlement_effect_claimed = 0""",
                (timestamp, operation_id, TransactionState.VALIDATED_SUCCESS.value),
            )
        return cursor.rowcount == 1

    def pending_settlement_events(self, *, limit: int = 100) -> tuple[SettlementEvent, ...]:
        if isinstance(limit, bool) or limit < 1:
            raise ValueError("limit must be a positive integer")
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM xrpl_settlement_outbox
                   WHERE delivered_at IS NULL ORDER BY created_at, event_id LIMIT ?""",
                (limit,),
            ).fetchall()
        return tuple(_settlement_event(row) for row in rows)

    def begin_settlement_event_delivery(self, event_id: str) -> SettlementEvent:
        timestamp = _now()
        with self._write() as connection:
            cursor = connection.execute(
                """UPDATE xrpl_settlement_outbox
                   SET delivery_attempts = delivery_attempts + 1, last_attempt_at = ?
                   WHERE event_id = ? AND delivered_at IS NULL""",
                (timestamp, event_id),
            )
            if cursor.rowcount != 1:
                row = connection.execute(
                    "SELECT * FROM xrpl_settlement_outbox WHERE event_id = ?", (event_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(event_id)
                return _settlement_event(row)
        return self.get_settlement_event(event_id)

    def mark_settlement_event_delivered(self, event_id: str) -> bool:
        timestamp = _now()
        with self._write() as connection:
            cursor = connection.execute(
                """UPDATE xrpl_settlement_outbox SET delivered_at = ?
                   WHERE event_id = ? AND delivered_at IS NULL""",
                (timestamp, event_id),
            )
        return cursor.rowcount == 1

    def get_settlement_event(self, event_id: str) -> SettlementEvent:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM xrpl_settlement_outbox WHERE event_id = ?", (event_id,)
            ).fetchone()
        if row is None:
            raise KeyError(event_id)
        return _settlement_event(row)

    def attempts(self, operation_id: str) -> tuple[SubmissionAttempt, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT attempt_number, started_at, completed_at, disposition,
                          provisional_status
                   FROM xrpl_submission_attempts
                   WHERE operation_id = ? ORDER BY attempt_number""",
                (operation_id,),
            ).fetchall()
        return tuple(
            SubmissionAttempt(
                attempt_number=cast(int, row["attempt_number"]),
                started_at=cast(str, row["started_at"]),
                completed_at=cast(str | None, row["completed_at"]),
                disposition=(
                    None
                    if row["disposition"] is None
                    else SubmissionDisposition(cast(str, row["disposition"]))
                ),
                provisional_status=cast(str | None, row["provisional_status"]),
            )
            for row in rows
        )


def _record(row: sqlite3.Row) -> PaymentRecord:
    return PaymentRecord(
        operation_id=cast(str, row["operation_id"]),
        payment_authorization_id=cast(str, row["payment_authorization_id"]),
        transaction_type=TransactionType(cast(str, row["transaction_type"])),
        account=cast(str, row["account"]),
        destination=cast(str, row["destination"]),
        asset=Asset(
            label=cast(str, row["asset_label"]),
            currency_code=cast(str, row["asset_currency_code"]),
            issuer=cast(str, row["asset_issuer"]),
        ),
        amount_micro_units=cast(int, row["amount_micro_units"]),
        fee_drops=cast(int, row["fee_drops"]),
        network_id=cast(int, row["network_id"]),
        sequence=cast(int | None, row["sequence"]),
        ticket_sequence=cast(int | None, row["ticket_sequence"]),
        earliest_ledger_sequence=cast(int, row["earliest_ledger_sequence"]),
        last_ledger_sequence=cast(int, row["last_ledger_sequence"]),
        signed_transaction=cast(bytes, row["signed_transaction"]),
        transaction_hash=cast(str, row["transaction_hash"]),
        state=TransactionState(cast(str, row["state"])),
        last_provisional_status=cast(str | None, row["last_provisional_status"]),
        final_result=cast(str | None, row["final_result"]),
        final_detail=cast(str | None, row["final_detail"]),
        final_evidence=cast(bytes | None, row["final_evidence"]),
        validated_ledger_index=cast(int | None, row["validated_ledger_index"]),
        validated_ledger_hash=cast(str | None, row["validated_ledger_hash"]),
        verified_delivered_amount_micro_units=cast(
            int | None, row["verified_delivered_amount_micro_units"]
        ),
        evidence_verification_version=cast(int, row["evidence_verification_version"]),
        submission_anchor_ledger_index=cast(int | None, row["submission_anchor_ledger_index"]),
        submission_anchor_ledger_hash=cast(str | None, row["submission_anchor_ledger_hash"]),
        submission_anchor_source_id=cast(str | None, row["submission_anchor_source_id"]),
        settlement_status=cast(str | None, row["settlement_status"]),
        settlement_effect_claimed=bool(row["settlement_effect_claimed"]),
        created_at=cast(str, row["created_at"]),
        updated_at=cast(str, row["updated_at"]),
    )


def _settlement_event_id(operation_id: str, transaction_hash_value: str) -> str:
    material = f"credence:xrpl-settlement:v1\x00{operation_id}\x00{transaction_hash_value}".encode()
    return hashlib.sha256(material).hexdigest()


def _settlement_event_payload(
    record: PaymentRecord,
    *,
    ledger_index: int | None,
    ledger_hash: str | None,
    delivered_amount_micro_units: int | None,
) -> bytes:
    return json.dumps(
        {
            "delivered_amount_micro_units": delivered_amount_micro_units,
            "evidence_verification_version": 1,
            "event_type": "XRPL_PAYMENT_VALIDATED",
            "ledger_hash": ledger_hash,
            "ledger_index": ledger_index,
            "operation_id": record.operation_id,
            "payment_authorization_id": record.payment_authorization_id,
            "result": "tesSUCCESS",
            "transaction_hash": record.transaction_hash,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _settlement_event(row: sqlite3.Row) -> SettlementEvent:
    return SettlementEvent(
        event_id=cast(str, row["event_id"]),
        operation_id=cast(str, row["operation_id"]),
        payment_authorization_id=json.loads(cast(bytes, row["payload"]))[
            "payment_authorization_id"
        ],
        transaction_hash=cast(str, row["transaction_hash"]),
        payload=cast(bytes, row["payload"]),
        delivery_attempts=cast(int, row["delivery_attempts"]),
        created_at=cast(str, row["created_at"]),
        delivered_at=cast(str | None, row["delivered_at"]),
    )
