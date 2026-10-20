"""Durable operation journal for integrated purchases.

Operational, not canonical: one row per purchase operation, in the same SQLite database as the
settlement coordinator, so recovery reads the journal and Core's authoritative reservation,
attempt and settlement records from one file. Every write is one short BEGIN IMMEDIATE transaction,
and each state change is a compare-and-set on the current state. No write transaction is ever held
across a provider call.

What is kept: the operation identity and idempotency key, the facility, reservation and provider,
the exact signed request and approved purchase objects, the exact signed delivery evidence, digests,
the exact response bytes until the authorized agent acknowledges them, the settlement outcome and
the failure or dispute classification. Prompts are never stored. Credentials and private signing
keys are never stored.

Response retention (hackathon scope): synthetic, non-sensitive demo content only, in a local
database file with owner-only permissions (0600). This is not production-grade protection of
confidential response data. A response is deleted only by acknowledgment; a response that never
becomes available (dispute, late, failed delivery) is kept with the evidence, with no automatic
expiry.

Owner: Integration (San Antonio).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from credence.core.objects import (
    CanonicalObject,
    DeliveryReceiptBody,
    ExecutionProfile,
    ProviderAttestationBody,
    PurchaseRequest,
    Quote,
    ServiceClass,
    SignedObject,
)
from credence.core.primitives import Timestamp


class OperationState(Enum):
    """Lifecycle of one integrated purchase (contracts/INTEGRATION-FLOW.md)."""

    INTENT = "intent"
    RESERVED = "reserved"
    EXECUTING = "executing"
    DELIVERED = "delivered"
    SETTLED = "settled"
    RETRIEVED = "retrieved"
    ACKNOWLEDGED = "acknowledged"
    NO_QUALIFYING_OFFER = "no_qualifying_offer"
    POLICY_REJECTED = "policy_rejected"
    RELEASED_UNDELIVERED = "released_undelivered"
    EXPIRED = "expired"
    ABANDONED = "abandoned"
    UNKNOWN = "unknown"
    DELIVERY_FAILED = "delivery_failed"
    DISPUTED = "disputed"
    LATE = "late"


S = OperationState

# From reservation (or intent) through accounting settlement or terminal reconciliation.
IN_FLIGHT: frozenset[OperationState] = frozenset({S.INTENT, S.RESERVED, S.EXECUTING, S.DELIVERED})
RESPONSE_AVAILABLE: frozenset[OperationState] = frozenset({S.SETTLED, S.RETRIEVED})

_STATES = ", ".join(f"'{s.value}'" for s in OperationState)

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS integration_operations (
    operation_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    facility_id TEXT NOT NULL,
    prompt_digest TEXT NOT NULL,
    provider_id TEXT,
    reservation_id TEXT,
    request_digest TEXT,
    agent_key_id TEXT,
    agent_subject TEXT,
    request BLOB,
    request_signature BLOB,
    quote BLOB,
    service_class BLOB,
    execution_profile BLOB,
    state TEXT NOT NULL CHECK (state IN ({_STATES})),
    detail TEXT,
    receipt BLOB,
    gateway_signature BLOB,
    provider_attestation BLOB,
    provider_signature BLOB,
    receipt_digest TEXT,
    response_digest TEXT,
    response BLOB,
    settlement_outcome TEXT,
    verifier_decision_digest TEXT,
    reason_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    retrieved_at TEXT,
    acknowledged_at TEXT
);
CREATE INDEX IF NOT EXISTS integration_operations_by_facility
    ON integration_operations (facility_id, state);
CREATE TABLE IF NOT EXISTS integration_authorization_nonces (
    agent_key_id TEXT NOT NULL,
    nonce TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('retrieve', 'acknowledge')),
    used_at TEXT NOT NULL,
    PRIMARY KEY (agent_key_id, nonce)
);
"""

_COLUMNS = (
    "operation_id",
    "idempotency_key",
    "facility_id",
    "prompt_digest",
    "provider_id",
    "reservation_id",
    "request_digest",
    "agent_key_id",
    "agent_subject",
    "request",
    "request_signature",
    "quote",
    "service_class",
    "execution_profile",
    "state",
    "detail",
    "receipt",
    "gateway_signature",
    "provider_attestation",
    "provider_signature",
    "receipt_digest",
    "response_digest",
    "response",
    "settlement_outcome",
    "verifier_decision_digest",
    "reason_code",
    "created_at",
    "updated_at",
    "retrieved_at",
    "acknowledged_at",
)


class JournalConflict(RuntimeError):
    """A compare-and-set transition found the operation in an unexpected state."""


class NonceReused(RuntimeError):
    """An authorization nonce was already consumed (persisted; survives restarts)."""


def _decode[T: CanonicalObject](cls: type[T], data: object) -> T | None:
    if data is None:
        return None
    if not isinstance(data, bytes):
        raise TypeError(f"{cls.__name__}: expected canonical bytes")
    return cls.from_canonical(json.loads(data))


@dataclass(frozen=True)
class Operation:
    """One journal row. Canonical members are kept as their exact canonical bytes."""

    row: Mapping[str, object]

    def _text(self, name: str) -> str | None:
        value = self.row[name]
        return None if value is None else str(value)

    @property
    def operation_id(self) -> str:
        return str(self.row["operation_id"])

    @property
    def idempotency_key(self) -> str:
        return str(self.row["idempotency_key"])

    @property
    def facility_id(self) -> str:
        return str(self.row["facility_id"])

    @property
    def prompt_digest(self) -> str:
        return str(self.row["prompt_digest"])

    @property
    def state(self) -> OperationState:
        return OperationState(str(self.row["state"]))

    @property
    def detail(self) -> str | None:
        return self._text("detail")

    @property
    def provider_id(self) -> str | None:
        return self._text("provider_id")

    @property
    def reservation_id(self) -> str | None:
        return self._text("reservation_id")

    @property
    def agent_key_id(self) -> str | None:
        return self._text("agent_key_id")

    @property
    def agent_subject(self) -> str | None:
        return self._text("agent_subject")

    @property
    def receipt_digest(self) -> str | None:
        return self._text("receipt_digest")

    @property
    def response_digest(self) -> str | None:
        return self._text("response_digest")

    @property
    def reason_code(self) -> str | None:
        return self._text("reason_code")

    @property
    def settlement_outcome(self) -> str | None:
        return self._text("settlement_outcome")

    @property
    def response(self) -> bytes | None:
        value = self.row["response"]
        if value is None:
            return None
        if not isinstance(value, bytes):
            raise TypeError("response: expected bytes")
        return value

    @property
    def retrieved_at(self) -> str | None:
        return self._text("retrieved_at")

    @property
    def acknowledged_at(self) -> str | None:
        return self._text("acknowledged_at")

    def request(self) -> PurchaseRequest | None:
        return _decode(PurchaseRequest, self.row["request"])

    def request_signature(self) -> SignedObject | None:
        return _decode(SignedObject, self.row["request_signature"])

    def quote(self) -> Quote | None:
        return _decode(Quote, self.row["quote"])

    def service_class(self) -> ServiceClass | None:
        return _decode(ServiceClass, self.row["service_class"])

    def execution_profile(self) -> ExecutionProfile | None:
        return _decode(ExecutionProfile, self.row["execution_profile"])

    def receipt(self) -> DeliveryReceiptBody | None:
        return _decode(DeliveryReceiptBody, self.row["receipt"])

    def gateway_signature(self) -> SignedObject | None:
        return _decode(SignedObject, self.row["gateway_signature"])

    def provider_attestation(self) -> ProviderAttestationBody | None:
        return _decode(ProviderAttestationBody, self.row["provider_attestation"])

    def provider_signature(self) -> SignedObject | None:
        return _decode(SignedObject, self.row["provider_signature"])


class Journal:
    def __init__(self, path: Path, *, busy_timeout: float = 30) -> None:
        self._path = str(path)
        self._busy_timeout = busy_timeout
        with self._write() as conn:
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    conn.execute(statement)

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path, timeout=self._busy_timeout, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()

    def _select(self, where: str, args: tuple[object, ...]) -> list[Operation]:
        with self._write() as conn:
            rows = conn.execute(
                f"SELECT {', '.join(_COLUMNS)} FROM integration_operations WHERE {where} "
                "ORDER BY created_at, operation_id",
                args,
            ).fetchall()
        return [Operation(dict(zip(_COLUMNS, row, strict=True))) for row in rows]

    def get(self, operation_id: str) -> Operation | None:
        found = self._select("operation_id = ?", (operation_id,))
        return found[0] if found else None

    def by_key(self, idempotency_key: str) -> Operation | None:
        found = self._select("idempotency_key = ?", (idempotency_key,))
        return found[0] if found else None

    def in_flight(self, facility_id: str | None = None) -> list[Operation]:
        states = tuple(s.value for s in IN_FLIGHT)
        marks = ", ".join("?" for _ in states)
        if facility_id is None:
            return self._select(f"state IN ({marks})", states)
        return self._select(f"facility_id = ? AND state IN ({marks})", (facility_id, *states))

    def in_states(self, states: frozenset[OperationState]) -> list[Operation]:
        values = tuple(s.value for s in states)
        return self._select(f"state IN ({', '.join('?' for _ in values)})", values)

    def create(self, fields: Mapping[str, object], now: Timestamp) -> Operation:
        """Insert a new operation. `fields` must include identity, facility, prompt and state."""
        row = {**fields, "created_at": now.value, "updated_at": now.value}
        unknown = set(row) - set(_COLUMNS)
        if unknown:
            raise ValueError(f"unknown journal columns: {sorted(unknown)}")
        names = ", ".join(row)
        with self._write() as conn:
            conn.execute(
                f"INSERT INTO integration_operations ({names}) "
                f"VALUES ({', '.join('?' for _ in row)})",
                tuple(row.values()),
            )
        created = self.get(str(fields["operation_id"]))
        assert created is not None
        return created

    def transition(
        self,
        operation_id: str,
        expected: OperationState | frozenset[OperationState],
        to: OperationState,
        now: Timestamp,
        **fields: object,
    ) -> Operation:
        """Compare-and-set the state (and any other columns) in one transaction."""
        allowed = expected if isinstance(expected, frozenset) else frozenset({expected})
        changes = {**fields, "state": to.value, "updated_at": now.value}
        unknown = set(changes) - set(_COLUMNS)
        if unknown:
            raise ValueError(f"unknown journal columns: {sorted(unknown)}")
        sets = ", ".join(f"{name} = ?" for name in changes)
        marks = ", ".join("?" for _ in allowed)
        with self._write() as conn:
            updated = conn.execute(
                f"UPDATE integration_operations SET {sets} "
                f"WHERE operation_id = ? AND state IN ({marks})",
                (*changes.values(), operation_id, *(s.value for s in allowed)),
            )
            if updated.rowcount != 1:
                raise JournalConflict(
                    f"{operation_id}: expected {sorted(s.value for s in allowed)}, moving to "
                    f"{to.value}"
                )
        moved = self.get(operation_id)
        assert moved is not None
        return moved

    def authorized_transition(
        self,
        *,
        agent_key_id: str,
        nonce: str,
        action: str,
        operation_id: str,
        expected: OperationState,
        to: OperationState,
        now: Timestamp,
        **fields: object,
    ) -> Operation:
        """Consume an authorization nonce and apply the access it authorizes, in one transaction.

        A reused nonce raises NonceReused; a state conflict raises JournalConflict and rolls the
        nonce back, so only a successful access consumes it."""
        changes = {**fields, "state": to.value, "updated_at": now.value}
        unknown = set(changes) - set(_COLUMNS)
        if unknown:
            raise ValueError(f"unknown journal columns: {sorted(unknown)}")
        sets = ", ".join(f"{name} = ?" for name in changes)
        with self._write() as conn:
            try:
                conn.execute(
                    "INSERT INTO integration_authorization_nonces (agent_key_id, nonce, "
                    "operation_id, action, used_at) VALUES (?, ?, ?, ?, ?)",
                    (agent_key_id, nonce, operation_id, action, now.value),
                )
            except sqlite3.IntegrityError:
                raise NonceReused(operation_id) from None
            updated = conn.execute(
                f"UPDATE integration_operations SET {sets} WHERE operation_id = ? AND state = ?",
                (*changes.values(), operation_id, expected.value),
            )
            if updated.rowcount != 1:
                raise JournalConflict(f"{operation_id}: expected {expected.value}")
        moved = self.get(operation_id)
        assert moved is not None
        return moved
