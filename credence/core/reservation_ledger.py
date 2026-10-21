"""Reservation ledger.

Tech: Python, in the state store

Responsibility (System architecture table, build plan):
Internal accounting holds against a facility for pending purchases; handles concurrent requests and failed deliveries. Not funds locked on XRPL

SQLite (the build plan's hackathon state store), stdlib sqlite3 only. Each write runs in its own
connection inside BEGIN IMMEDIATE, which takes SQLite's write lock before reading. So reading the
held headroom and the nonce, calling the pure policy engine, and writing the reservation, the used
nonce and the decision record happen as one serialized step: two simultaneous requests cannot both
spend the last headroom. A reservation is live while now < expires_at. Headroom counts only live
`held` reservations; settled amounts are in the facility's drawn amount. Expiry sets status
`released` and records release_reason `expired` outside the canonical object. No money moves here.

Facility accounting (C3B): a facility registered in `facility_accounts` has an authoritative stored
limit and drawn amount, maintained only by the settlement coordinator. For such a facility,
the coordinator's internal reserve step refuses a Facility whose limit or drawn differs from the
stored row (stale or conflicting accounting). Since Integration I1e the public `reserve` and
`reserve_in_transaction` refuse a registered facility outright: they take caller-supplied signature
facts, which only the coordinator may compute. The bare `settle` here refuses outright: settlement must go through the
coordinator, which updates drawn in the same transaction. Since Integration I1c the bare `release`
refuses too: a hold on an accounted facility is released only through the coordinator's audited
paths. Unregistered facilities keep the C2 behaviour.

The database must be a file path (each operation opens its own connection).

Owner: Credence Core.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from credence.core.objects import (
    ExecutionProfile,
    Facility,
    ProcurementClass,
    PurchaseRequest,
    Quote,
    Reservation,
    ServiceClass,
)
from credence.core.policy_engine import PolicyDecision, PolicyInput, evaluate
from credence.core.primitives import (
    FacilityId,
    MicroTrlusd,
    Nonce,
    QuoteId,
    ReservationId,
    ReservationStatus,
    Timestamp,
    strict_int,
)

DEFAULT_TTL_SECONDS = 600  # build plan: "a reservation expires 10 minutes after creation (tune it)"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reservations (
    reservation_id TEXT PRIMARY KEY,
    facility_id TEXT NOT NULL,
    quote_id TEXT NOT NULL,
    maximum_amount_micro_trlusd INTEGER NOT NULL CHECK (maximum_amount_micro_trlusd >= 0),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('held', 'settled', 'released')),
    release_reason TEXT CHECK (release_reason IN ('released', 'expired')),
    decision_record_digest TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS reservations_by_facility ON reservations (facility_id, status);
CREATE TABLE IF NOT EXISTS used_nonces (
    facility_id TEXT NOT NULL,
    nonce TEXT NOT NULL,
    reservation_id TEXT NOT NULL,
    PRIMARY KEY (facility_id, nonce)
);
CREATE TABLE IF NOT EXISTS decision_records (
    digest TEXT PRIMARY KEY,
    canonical BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS facility_accounts (
    facility_id TEXT PRIMARY KEY,
    limit_micro_trlusd INTEGER NOT NULL CHECK (limit_micro_trlusd >= 0),
    drawn_micro_trlusd INTEGER NOT NULL CHECK (drawn_micro_trlusd >= 0)
);
"""

_COLUMNS = (
    "reservation_id, facility_id, quote_id, maximum_amount_micro_trlusd, created_at, expires_at, "
    "status"
)


class ReservationStateError(RuntimeError):
    """A settle or release that the reservation's state does not allow. `reason` is a code."""

    def __init__(self, reason: str, reservation_id: str) -> None:
        super().__init__(f"{reason}: {reservation_id}")
        self.reason = reason
        self.reservation_id = reservation_id


@dataclass(frozen=True)
class PurchaseContext:
    """The policy inputs the ledger does not own; it supplies headroom, nonce use and time."""

    request: PurchaseRequest
    request_signature_valid: bool
    working_key_current: bool
    facility: Facility
    quote: Quote
    service_class: ServiceClass
    execution_profile: ExecutionProfile
    procurement_classes: tuple[ProcurementClass, ...]


@dataclass(frozen=True)
class ReserveResult:
    decision: PolicyDecision
    reservation: Reservation | None


def reservation_id_for(request: PurchaseRequest) -> ReservationId:
    """Deterministic: one reservation per purchase request."""
    return ReservationId("reservation-" + request.digest().hex[:32])


def _row_to_reservation(row: tuple[str, str, str, int, str, str, str]) -> Reservation:
    reservation_id, facility_id, quote_id, amount, created_at, expires_at, status = row
    return Reservation(
        reservation_id=ReservationId(reservation_id),
        facility_id=FacilityId(facility_id),
        quote_id=QuoteId(quote_id),
        maximum_amount_micro_trlusd=MicroTrlusd(amount),
        created_at=Timestamp(created_at),
        expires_at=Timestamp(expires_at),
        status=ReservationStatus(status),
    )


class ReservationLedger:
    def __init__(
        self, path: str | Path, *, ttl_seconds: int = DEFAULT_TTL_SECONDS, busy_timeout: float = 30
    ) -> None:
        if str(path) == ":memory:":
            raise ValueError("the reservation ledger needs a file path, not :memory:")
        self._path = str(path)
        self._ttl = strict_int(ttl_seconds, "ttl_seconds", minimum=1)
        self._busy_timeout = busy_timeout
        with self._read() as conn:
            conn.executescript(_SCHEMA)  # CREATE ... IF NOT EXISTS; idempotent

    # --- connections -----------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path, timeout=self._busy_timeout, isolation_level=None)

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
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

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    # --- queries ---------------------------------------------------------------------------

    @staticmethod
    def _held_amount(conn: sqlite3.Connection, facility_id: FacilityId, now: Timestamp) -> int:
        row = conn.execute(
            "SELECT COALESCE(SUM(maximum_amount_micro_trlusd), 0) FROM reservations "
            "WHERE facility_id = ? AND status = 'held' AND expires_at > ?",
            (facility_id.value, now.value),
        ).fetchone()
        return int(row[0])

    def held_amount(self, facility_id: FacilityId, now: Timestamp) -> MicroTrlusd:
        """The sum of live held reservations on the facility at `now`."""
        with self._read() as conn:
            return MicroTrlusd(self._held_amount(conn, facility_id, now))

    def nonce_used(self, facility_id: FacilityId, nonce: Nonce) -> bool:
        with self._read() as conn:
            row = conn.execute(
                "SELECT 1 FROM used_nonces WHERE facility_id = ? AND nonce = ?",
                (facility_id.value, nonce.value),
            ).fetchone()
        return row is not None

    def get(self, reservation_id: ReservationId) -> Reservation:
        with self._read() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM reservations WHERE reservation_id = ?",
                (reservation_id.value,),
            ).fetchone()
        if row is None:
            raise ReservationStateError("NOT_FOUND", reservation_id.value)
        return _row_to_reservation(row)

    def release_reason(self, reservation_id: ReservationId) -> str | None:
        with self._read() as conn:
            row = conn.execute(
                "SELECT release_reason FROM reservations WHERE reservation_id = ?",
                (reservation_id.value,),
            ).fetchone()
        if row is None:
            raise ReservationStateError("NOT_FOUND", reservation_id.value)
        return None if row[0] is None else str(row[0])

    def decision_record_digest(self, reservation_id: ReservationId) -> str | None:
        """The digest of the accepting policy decision that created the reservation (read only;
        I2a CR-4). None when no such reservation exists."""
        with self._read() as conn:
            row = conn.execute(
                "SELECT decision_record_digest FROM reservations WHERE reservation_id = ?",
                (reservation_id.value,),
            ).fetchone()
        return None if row is None else str(row[0])

    def decision_record_bytes(self, digest_value: str) -> bytes | None:
        with self._read() as conn:
            row = conn.execute(
                "SELECT canonical FROM decision_records WHERE digest = ?", (digest_value,)
            ).fetchone()
        return None if row is None else bytes(row[0])

    # --- writes ----------------------------------------------------------------------------

    @staticmethod
    def _expire(conn: sqlite3.Connection, now: Timestamp) -> list[str]:
        rows = conn.execute(
            "SELECT reservation_id FROM reservations WHERE status = 'held' AND expires_at <= ? "
            "ORDER BY reservation_id",
            (now.value,),
        ).fetchall()
        conn.execute(
            "UPDATE reservations SET status = 'released', release_reason = 'expired' "
            "WHERE status = 'held' AND expires_at <= ?",
            (now.value,),
        )
        return [str(r[0]) for r in rows]

    def expire(self, now: Timestamp) -> tuple[ReservationId, ...]:
        """Release every held reservation whose expires_at <= now; return their IDs."""
        with self._write() as conn:
            return tuple(ReservationId(r) for r in self._expire(conn, now))

    def reserve(self, context: PurchaseContext, now: Timestamp) -> ReserveResult:
        """Run the policy engine against current headroom and, on accept, hold the quoted maximum.

        Reading headroom and nonce use, deciding, and writing the reservation, the used nonce and
        the decision record are one BEGIN IMMEDIATE transaction.

        Unregistered facilities only. `PurchaseContext` carries caller-supplied signature facts,
        so a registered (accounted) facility is refused (`USE_SETTLEMENT_COORDINATOR`): the
        settlement coordinator reserves those, computing the facts itself from signed objects.
        """
        with self._write() as conn:
            return self.reserve_in_transaction(conn, context, now)

    @contextmanager
    def write_transaction(self) -> Iterator[sqlite3.Connection]:
        """One BEGIN IMMEDIATE transaction on this ledger's database (commit or roll back)."""
        with self._write() as conn:
            yield conn

    @staticmethod
    def facility_account(
        conn: sqlite3.Connection, facility_id: FacilityId
    ) -> tuple[int, int] | None:
        """(limit, drawn) stored for a registered facility, or None."""
        row = conn.execute(
            "SELECT limit_micro_trlusd, drawn_micro_trlusd FROM facility_accounts "
            "WHERE facility_id = ?",
            (facility_id.value,),
        ).fetchone()
        return None if row is None else (int(row[0]), int(row[1]))

    def reserve_in_transaction(
        self, conn: sqlite3.Connection, context: PurchaseContext, now: Timestamp
    ) -> ReserveResult:
        """`reserve`, inside a transaction the caller opened with `write_transaction`.

        Unregistered facilities only (`USE_SETTLEMENT_COORDINATOR` otherwise), as for `reserve`.
        """
        facility_id = context.facility.facility_id
        if self.facility_account(conn, facility_id) is not None:
            raise ReservationStateError("USE_SETTLEMENT_COORDINATOR", facility_id.value)
        return self._reserve_step(conn, context, now)

    def _reserve_accounted(
        self, conn: sqlite3.Connection, context: PurchaseContext, now: Timestamp
    ) -> ReserveResult:
        """Internal to the settlement coordinator (Integration I1e). Not a public interface.

        The coordinator calls this inside its own BEGIN IMMEDIATE transaction for a registered
        facility, after computing the signature facts in `context` itself from the signed request
        and the key registry. No other caller may use it: it trusts those facts.
        """
        if self.facility_account(conn, context.facility.facility_id) is None:
            # The coordinator refuses unregistered facilities before calling this.
            raise ValueError("internal accounted reserve needs a registered facility")
        return self._reserve_step(conn, context, now)

    def _reserve_step(
        self, conn: sqlite3.Connection, context: PurchaseContext, now: Timestamp
    ) -> ReserveResult:
        facility_id = context.facility.facility_id
        account = self.facility_account(conn, facility_id)
        if account is not None and account != (
            context.facility.limit_micro_trlusd.value,
            context.facility.drawn_micro_trlusd.value,
        ):
            raise ReservationStateError("STALE_FACILITY_ACCOUNTING", facility_id.value)
        self._expire(conn, now)
        reserved = self._held_amount(conn, facility_id, now)
        nonce_row = conn.execute(
            "SELECT 1 FROM used_nonces WHERE facility_id = ? AND nonce = ?",
            (facility_id.value, context.request.nonce.value),
        ).fetchone()
        decision = evaluate(
            PolicyInput(
                request=context.request,
                request_signature_valid=context.request_signature_valid,
                working_key_current=context.working_key_current,
                facility=context.facility,
                quote=context.quote,
                service_class=context.service_class,
                execution_profile=context.execution_profile,
                procurement_classes=context.procurement_classes,
                currently_reserved=MicroTrlusd(reserved),
                nonce_already_used=nonce_row is not None,
                evaluated_at=now,
            )
        )
        record = decision.record
        conn.execute(
            "INSERT OR IGNORE INTO decision_records (digest, canonical) VALUES (?, ?)",
            (record.digest().value, record.canonical_bytes()),
        )
        if decision.reservation_amount is None:
            return ReserveResult(decision=decision, reservation=None)
        reservation = Reservation(
            reservation_id=reservation_id_for(context.request),
            facility_id=facility_id,
            quote_id=context.quote.quote_id,
            maximum_amount_micro_trlusd=decision.reservation_amount,
            created_at=now,
            expires_at=now.plus_seconds(self._ttl),
            status=ReservationStatus.HELD,
        )
        conn.execute(
            f"INSERT INTO reservations ({_COLUMNS}, decision_record_digest) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                reservation.reservation_id.value,
                facility_id.value,
                reservation.quote_id.value,
                reservation.maximum_amount_micro_trlusd.value,
                reservation.created_at.value,
                reservation.expires_at.value,
                reservation.status.value,
                record.digest().value,
            ),
        )
        conn.execute(
            "INSERT INTO used_nonces (facility_id, nonce, reservation_id) VALUES (?, ?, ?)",
            (facility_id.value, context.request.nonce.value, reservation.reservation_id.value),
        )
        return ReserveResult(decision=decision, reservation=reservation)

    def _transition(
        self, reservation_id: ReservationId, now: Timestamp, *, to: ReservationStatus
    ) -> Reservation:
        expired = False
        with self._write() as conn:
            row = conn.execute(
                "SELECT status, expires_at, facility_id FROM reservations WHERE reservation_id = ?",
                (reservation_id.value,),
            ).fetchone()
            if row is None:
                raise ReservationStateError("NOT_FOUND", reservation_id.value)
            status, expires_at, facility_id = row
            if self.facility_account(conn, FacilityId(facility_id)) is not None:
                # Accounted facility: settle and release go through the settlement coordinator.
                raise ReservationStateError("USE_SETTLEMENT_COORDINATOR", reservation_id.value)
            if status != ReservationStatus.HELD.value:
                raise ReservationStateError("NOT_HELD", reservation_id.value)
            if to is ReservationStatus.SETTLED and not now.value < expires_at:
                # A late delivery is not paid automatically; the hold lapses instead.
                conn.execute(
                    "UPDATE reservations SET status = 'released', release_reason = 'expired' "
                    "WHERE reservation_id = ?",
                    (reservation_id.value,),
                )
                expired = True
            else:
                conn.execute(
                    "UPDATE reservations SET status = ?, release_reason = ? "
                    "WHERE reservation_id = ?",
                    (
                        to.value,
                        "released" if to is ReservationStatus.RELEASED else None,
                        reservation_id.value,
                    ),
                )
        if expired:
            raise ReservationStateError("EXPIRED", reservation_id.value)
        return self.get(reservation_id)

    def settle(self, reservation_id: ReservationId, now: Timestamp) -> Reservation:
        """Mark a live held reservation settled. Expired holds are released and refused.

        Refused for a registered (accounted) facility: use the settlement coordinator.
        """
        return self._transition(reservation_id, now, to=ReservationStatus.SETTLED)

    def release(self, reservation_id: ReservationId, now: Timestamp) -> Reservation:
        """Release a held reservation on an unregistered facility.

        Refused for a registered (accounted) facility: the settlement coordinator releases those,
        only on an authenticated terminal delivery failure or an audited no-delivery release."""
        return self._transition(reservation_id, now, to=ReservationStatus.RELEASED)
