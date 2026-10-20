"""Facility registration boundary (Integration I1g, Core finding B-1).

A facility identity with any unaccounted ledger history cannot become accounted:
`register_facility` refuses `FACILITY_HAS_HISTORY` and writes nothing. Real SQLite, real
signatures (TEST keys, NOT FOR PRODUCTION), real policy engine and verifier. No XRPL.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest
from c3a_keys import ATTACKER, registry
from c3b_world import NOW, current_facility, delivery, facility, purchase

from credence.core.objects import Facility
from credence.core.primitives import MicroTrlusd, ReservationStatus
from credence.core.reservation_ledger import (
    PurchaseContext,
    ReservationLedger,
    ReservationStateError,
)
from credence.core.settlement_coordinator import (
    FACILITY_HISTORY_TABLES,
    PurchaseReference,
    SettlementCoordinator,
    SettlementOutcome,
    SettlementRefused,
    SignedPurchase,
)

FORGED_HELD = 2_458  # the C3B test quote's maximum cost, as in the Core owner's reproduction


def context(p: SignedPurchase, *, valid: bool = True) -> PurchaseContext:
    """A bare-ledger context with caller-supplied signature facts."""
    return PurchaseContext(
        request=p.request,
        request_signature_valid=valid,
        working_key_current=True,
        facility=p.facility,
        quote=p.quote,
        service_class=p.service_class,
        execution_profile=p.execution_profile,
        procurement_classes=p.procurement_classes,
    )


def snapshot(db: Path) -> dict[str, list[tuple[object, ...]]]:
    with sqlite3.connect(db) as conn:
        names = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        ]
        return {n: sorted(conn.execute(f'SELECT * FROM "{n}"').fetchall()) for n in names}


class Db:
    def __init__(self, tmp_path: Path) -> None:
        self.path = tmp_path / "db.sqlite3"
        self.ledger = ReservationLedger(self.path)
        self.coord = SettlementCoordinator(self.ledger, registry())

    def refused_registration(self, f: Facility) -> str:
        before = snapshot(self.path)
        with pytest.raises(SettlementRefused) as exc:
            self.coord.register_facility(f)
        assert snapshot(self.path) == before  # a refusal writes nothing
        return exc.value.reason

    def accounted(self, f: Facility) -> bool:
        try:
            self.coord.exposure(f.facility_id, NOW)
        except SettlementRefused as exc:
            assert exc.reason == "UNACCOUNTED_FACILITY"
            return False
        return True


# --- B1-A: exposure and nonce poisoning ----------------------------------------------------------


def test_b1a_forged_unaccounted_history_cannot_be_adopted(tmp_path: Path) -> None:
    db = Db(tmp_path)
    inflated = facility(limit=10**12)
    forged = purchase(1, inflated, signer=ATTACKER)
    held = db.ledger.reserve(context(forged), NOW)  # unregistered: the C2 path still accepts
    assert held.reservation is not None
    assert db.ledger.held_amount(inflated.facility_id, NOW).value == FORGED_HELD
    assert db.ledger.nonce_used(inflated.facility_id, forged.request.nonce)

    accounted = facility(limit=1_000)
    assert accounted.facility_id == inflated.facility_id
    assert db.refused_registration(accounted) == "FACILITY_HAS_HISTORY"
    assert not db.accounted(accounted)  # no accounted state, so no exposure above a limit
    assert db.ledger.held_amount(inflated.facility_id, NOW).value == FORGED_HELD  # unchanged
    assert db.ledger.nonce_used(inflated.facility_id, forged.request.nonce)  # still historical
    with sqlite3.connect(db.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM used_nonces").fetchone()[0] == 1
    # the coordinated path stays closed for this identity
    legit = purchase(2, accounted)
    with pytest.raises(SettlementRefused) as exc:
        db.coord.reserve(legit, NOW)
    assert exc.value.reason == "UNACCOUNTED_FACILITY"


# --- B1-D: historical variants -------------------------------------------------------------------


@pytest.mark.parametrize("variant", ["held", "released", "expired", "settled"])
def test_b1d_any_reservation_history_refuses_registration(tmp_path: Path, variant: str) -> None:
    """A facility does not become registrable because its reservations are no longer held."""
    db = Db(tmp_path)
    base = facility()
    result = db.ledger.reserve(context(purchase(1, base)), NOW)
    assert result.reservation is not None
    rid = result.reservation.reservation_id
    if variant == "released":
        db.ledger.release(rid, NOW.plus_seconds(1))
    elif variant == "expired":
        db.ledger.expire(result.reservation.expires_at)
    elif variant == "settled":
        db.ledger.settle(rid, NOW.plus_seconds(1))
    assert db.ledger.held_amount(base.facility_id, NOW).value == (
        FORGED_HELD if variant == "held" else 0
    )
    assert db.refused_registration(base) == "FACILITY_HAS_HISTORY"
    assert not db.accounted(base)


@pytest.mark.parametrize("table", FACILITY_HISTORY_TABLES)
def test_b1d_history_in_any_listed_table_refuses_registration(tmp_path: Path, table: str) -> None:
    """Every table that records facility history is checked (rows planted directly, as a
    stand-in for history from any older or out-of-band writer)."""
    db = Db(tmp_path)
    base = facility()
    with sqlite3.connect(db.path) as conn:
        cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
        notnull = {r[1]: (r[3], r[2]) for r in conn.execute(f'PRAGMA table_info("{table}")')}
        values: list[object] = []
        for col in cols:
            if col == "facility_id":
                values.append(base.facility_id.value)
            elif col == "dispute_id":
                values.append(None)
            elif notnull[col][1] == "INTEGER":
                values.append(0)
            elif notnull[col][1] == "BLOB":
                values.append(b"x")
            else:
                values.append(_text_for(table, col))
        conn.execute(
            f'INSERT INTO "{table}" ({", ".join(cols)}) VALUES ({", ".join("?" for _ in cols)})',
            values,
        )
    assert db.refused_registration(base) == "FACILITY_HAS_HISTORY"


def _text_for(table: str, col: str) -> str:
    constrained = {
        ("reservations", "status"): "released",
        ("reservations", "release_reason"): "expired",
        ("accounting_settlements", "state"): "accounting_settled",
        ("settlement_disputes", "kind"): "rejected_submission",
        ("delivery_attempts", "unknown_cause"): "interrupted",
        ("no_delivery_releases", "reason"): "abandoned_before_execution",
    }
    return constrained.get((table, col), f"history-{col}")


def test_b1d_rejected_policy_decision_alone_is_not_history(tmp_path: Path) -> None:
    """A policy rejection stores a decision record but no reservation, nonce, attempt or
    settlement state, and decision records carry no facility identity. It does not block
    registration (documented in SETTLEMENT-COORDINATOR.md)."""
    db = Db(tmp_path)
    base = facility()
    p = purchase(1, base)
    rejected = db.ledger.reserve(context(p, valid=False), NOW)
    assert rejected.reservation is None
    assert not db.ledger.nonce_used(base.facility_id, p.request.nonce)
    assert db.ledger.decision_record_bytes(rejected.decision.record.digest().value) is not None
    db.coord.register_facility(base)
    assert db.accounted(base)


# --- B1-E: registration race on independent connections ------------------------------------------


def test_b1e_reservation_first_then_registration_refuses(tmp_path: Path) -> None:
    db = Db(tmp_path)
    base = facility()
    other = ReservationLedger(db.path)  # an independent connection per operation
    assert other.reserve(context(purchase(1, base)), NOW).reservation is not None
    assert db.refused_registration(base) == "FACILITY_HAS_HISTORY"


def test_b1e_registration_first_then_bare_reservation_refuses(tmp_path: Path) -> None:
    db = Db(tmp_path)
    base = facility()
    db.coord.register_facility(base)
    other = ReservationLedger(db.path)
    with pytest.raises(ReservationStateError) as exc:
        other.reserve(context(purchase(1, base)), NOW)
    assert exc.value.reason == "USE_SETTLEMENT_COORDINATOR"
    with sqlite3.connect(db.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 0


def _race(path: Path) -> str:
    ledger = ReservationLedger(path)
    coord = SettlementCoordinator(ReservationLedger(path), registry())
    base = facility()
    p = purchase(1, base)
    barrier = threading.Barrier(2)
    out: dict[str, str] = {}

    def reserve() -> None:
        barrier.wait()
        try:
            ledger.reserve(context(p), NOW)
            out["reserve"] = "ok"
        except ReservationStateError as exc:
            out["reserve"] = exc.reason

    def register() -> None:
        barrier.wait()
        try:
            coord.register_facility(base)
            out["register"] = "ok"
        except SettlementRefused as exc:
            out["register"] = exc.reason

    threads = [threading.Thread(target=reserve), threading.Thread(target=register)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    with sqlite3.connect(path) as conn:
        reservations = conn.execute("SELECT COUNT(*) FROM reservations").fetchone()[0]
        accounts = conn.execute("SELECT COUNT(*) FROM facility_accounts").fetchone()[0]
    assert not (reservations and accounts), out  # never both
    if out["reserve"] == "ok":
        assert (out["register"], reservations, accounts) == ("FACILITY_HAS_HISTORY", 1, 0)
        return "reserve"
    assert (out["reserve"], out["register"], reservations, accounts) == (
        "USE_SETTLEMENT_COORDINATOR",
        "ok",
        0,
        1,
    )
    return "register"


def test_b1e_concurrent_reservation_and_registration_have_one_ordering(tmp_path: Path) -> None:
    winners = []
    for round_ in range(40):
        (tmp_path / str(round_)).mkdir()
        winners.append(_race(tmp_path / str(round_) / "db.sqlite3"))
    assert set(winners) <= {"reserve", "register"}


# --- B1-F: normal registration -------------------------------------------------------------------


def test_b1f_fresh_facility_registers_reserves_enforces_limit_and_settles(tmp_path: Path) -> None:
    db = Db(tmp_path)
    base = facility(limit=3_000)
    db.coord.register_facility(base)
    p = purchase(1, current_facility(db.coord, base))
    reserved = db.coord.reserve(p, NOW)
    assert reserved.reservation is not None
    over = db.coord.reserve(purchase(2, current_facility(db.coord, base)), NOW)
    assert over.reservation is None and over.decision.record.reason_code is not None
    assert over.decision.record.reason_code.value == "OVER_LIMIT"
    ref = PurchaseReference(
        reserved.reservation.reservation_id,
        p.request,
        p.quote,
        p.service_class,
        p.execution_profile,
    )
    db.coord.begin_delivery(ref, NOW.plus_seconds(1))
    sub = delivery(p, ref.reservation_id, previous=None)
    result = db.coord.settle(sub, NOW.plus_seconds(5))
    assert result.outcome is SettlementOutcome.ACCOUNTING_SETTLED
    assert db.coord.exposure(base.facility_id, NOW).drawn == result.actual_amount
    assert db.ledger.get(ref.reservation_id).status is ReservationStatus.SETTLED
    assert db.coord.chain_head(base.facility_id) == sub.receipt.digest()
    assert MicroTrlusd(3_000) == db.coord.exposure(base.facility_id, NOW).limit
