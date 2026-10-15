"""Reservation ledger on real SQLite: atomic reserve, nonce marking, expiry, settle, release, races."""

import json
import threading
from dataclasses import replace
from pathlib import Path

import pytest
from c2_world import FACILITY, MAX_COST, NOW, REQUEST, PolicyWorld

from credence.core.objects import DecisionRecord
from credence.core.primitives import (
    MicroTrlusd,
    Nonce,
    ReasonCode,
    ReservationId,
    ReservationStatus,
)
from credence.core.reservation_ledger import (
    DEFAULT_TTL_SECONDS,
    ReservationLedger,
    ReservationStateError,
    ReserveResult,
)


@pytest.fixture
def ledger(tmp_path: Path) -> ReservationLedger:
    return ReservationLedger(tmp_path / "ledger.sqlite3")


def world_with_nonce(index: int, *, headroom_for: int | None = None) -> PolicyWorld:
    request = replace(REQUEST, nonce=Nonce(f"{index:032x}"))
    facility = FACILITY
    if headroom_for is not None:
        limit = FACILITY.drawn_micro_trlusd.value + headroom_for
        facility = replace(FACILITY, limit_micro_trlusd=MicroTrlusd(limit))
    return PolicyWorld(request=request, facility=facility)


def stored_record(ledger: ReservationLedger, result: ReserveResult) -> DecisionRecord:
    raw = ledger.decision_record_bytes(result.decision.record.digest().value)
    assert raw is not None
    return DecisionRecord.from_canonical(json.loads(raw))


def test_accept_holds_the_quoted_maximum_and_marks_the_nonce(ledger: ReservationLedger) -> None:
    world = PolicyWorld()
    result = ledger.reserve(world.context(), NOW)
    assert result.decision.accepted and result.reservation is not None
    reservation = result.reservation
    assert reservation.status is ReservationStatus.HELD
    assert reservation.maximum_amount_micro_trlusd == MicroTrlusd(MAX_COST)
    assert reservation.created_at == NOW
    assert reservation.expires_at == NOW.plus_seconds(DEFAULT_TTL_SECONDS)
    assert ledger.get(reservation.reservation_id) == reservation
    assert ledger.nonce_used(world.request.facility_id, world.request.nonce)
    assert ledger.held_amount(world.request.facility_id, NOW) == MicroTrlusd(MAX_COST)
    assert stored_record(ledger, result) == result.decision.record


def test_reject_stores_its_decision_but_holds_nothing(ledger: ReservationLedger) -> None:
    world = replace(PolicyWorld(), request_signature_valid=False)
    result = ledger.reserve(world.context(), NOW)
    assert result.decision.record.reason_code is ReasonCode.SIG_INVALID
    assert result.reservation is None
    assert not ledger.nonce_used(world.request.facility_id, world.request.nonce)
    assert ledger.held_amount(world.request.facility_id, NOW) == MicroTrlusd(0)
    assert stored_record(ledger, result) == result.decision.record


def test_same_request_twice_is_nonce_reused(ledger: ReservationLedger) -> None:
    context = PolicyWorld().context()
    assert ledger.reserve(context, NOW).decision.accepted
    again = ledger.reserve(context, NOW.plus_seconds(1))
    assert again.decision.record.reason_code is ReasonCode.NONCE_REUSED


def test_held_reservations_consume_headroom(ledger: ReservationLedger) -> None:
    first = world_with_nonce(1, headroom_for=2 * MAX_COST - 1)
    second = world_with_nonce(2, headroom_for=2 * MAX_COST - 1)
    assert ledger.reserve(first.context(), NOW).decision.accepted
    result = ledger.reserve(second.context(), NOW)
    assert result.decision.record.reason_code is ReasonCode.OVER_LIMIT


def test_reservation_is_live_until_expires_at_exclusive(ledger: ReservationLedger) -> None:
    result = ledger.reserve(PolicyWorld().context(), NOW)
    assert result.reservation is not None
    expires_at = result.reservation.expires_at
    facility_id = FACILITY.facility_id
    assert ledger.held_amount(facility_id, expires_at.plus_seconds(-1)) == MicroTrlusd(MAX_COST)
    assert ledger.held_amount(facility_id, expires_at) == MicroTrlusd(0)


def test_expiry_frees_headroom_and_is_recorded(ledger: ReservationLedger) -> None:
    first = world_with_nonce(1, headroom_for=MAX_COST)
    held = ledger.reserve(first.context(), NOW).reservation
    assert held is not None
    later = held.expires_at
    assert ledger.expire(later) == (held.reservation_id,)
    assert ledger.get(held.reservation_id).status is ReservationStatus.RELEASED
    assert ledger.release_reason(held.reservation_id) == "expired"
    second = world_with_nonce(2, headroom_for=MAX_COST)
    second = replace(
        second,
        request=replace(second.request, expires_at=later),
        quote=replace(second.quote, valid_until=later),
    )
    assert ledger.reserve(second.context(), later).decision.accepted


def test_settle_and_release(ledger: ReservationLedger) -> None:
    held = ledger.reserve(world_with_nonce(1).context(), NOW).reservation
    other = ledger.reserve(world_with_nonce(2).context(), NOW).reservation
    assert held is not None and other is not None
    settled = ledger.settle(held.reservation_id, NOW.plus_seconds(5))
    assert settled.status is ReservationStatus.SETTLED
    assert ledger.held_amount(FACILITY.facility_id, NOW) == MicroTrlusd(MAX_COST)
    with pytest.raises(ReservationStateError) as exc:
        ledger.settle(held.reservation_id, NOW.plus_seconds(6))
    assert exc.value.reason == "NOT_HELD"
    released = ledger.release(other.reservation_id, NOW.plus_seconds(7))
    assert released.status is ReservationStatus.RELEASED
    assert ledger.release_reason(other.reservation_id) == "released"
    with pytest.raises(ReservationStateError) as exc:
        ledger.release(other.reservation_id, NOW)
    assert exc.value.reason == "NOT_HELD"


def test_late_settlement_is_refused_and_the_hold_lapses(ledger: ReservationLedger) -> None:
    held = ledger.reserve(PolicyWorld().context(), NOW).reservation
    assert held is not None
    with pytest.raises(ReservationStateError) as exc:
        ledger.settle(held.reservation_id, held.expires_at)
    assert exc.value.reason == "EXPIRED"
    assert ledger.get(held.reservation_id).status is ReservationStatus.RELEASED
    assert ledger.release_reason(held.reservation_id) == "expired"


def test_unknown_reservation(ledger: ReservationLedger) -> None:
    missing = ReservationId("reservation-missing")
    for call in (lambda: ledger.get(missing), lambda: ledger.settle(missing, NOW)):
        with pytest.raises(ReservationStateError) as exc:
            call()
        assert exc.value.reason == "NOT_FOUND"


def test_state_persists_across_ledger_instances(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    held = ReservationLedger(path).reserve(PolicyWorld().context(), NOW).reservation
    assert held is not None
    reopened = ReservationLedger(path)
    assert reopened.get(held.reservation_id) == held
    assert reopened.held_amount(FACILITY.facility_id, NOW) == MicroTrlusd(MAX_COST)


def test_configuration_is_validated(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        ReservationLedger(":memory:")
    with pytest.raises(ValueError):
        ReservationLedger(tmp_path / "x.sqlite3", ttl_seconds=0)


# --- races on real SQLite ------------------------------------------------------------------


def race(ledger: ReservationLedger, worlds: list[PolicyWorld]) -> list[ReasonCode | None]:
    barrier = threading.Barrier(len(worlds))
    results: list[ReasonCode | None] = [None] * len(worlds)
    errors: list[BaseException] = []

    def run(index: int, world: PolicyWorld) -> None:
        try:
            barrier.wait()
            results[index] = ledger.reserve(world.context(), NOW).decision.record.reason_code
        except BaseException as exc:  # surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(i, w)) for i, w in enumerate(worlds)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors, errors
    return results


@pytest.mark.parametrize("attempt", range(40))
def test_race_to_the_last_headroom(tmp_path: Path, attempt: int) -> None:
    ledger = ReservationLedger(tmp_path / f"race-{attempt}.sqlite3")
    worlds = [world_with_nonce(i, headroom_for=2 * MAX_COST - 1) for i in (1, 2)]
    outcomes = race(ledger, worlds)
    assert sorted(outcomes, key=str) == sorted([None, ReasonCode.OVER_LIMIT], key=str)
    assert ledger.held_amount(FACILITY.facility_id, NOW) == MicroTrlusd(MAX_COST)


@pytest.mark.parametrize("attempt", range(10))
def test_many_racers_never_exceed_the_limit(tmp_path: Path, attempt: int) -> None:
    ledger = ReservationLedger(tmp_path / f"many-{attempt}.sqlite3")
    worlds = [world_with_nonce(i, headroom_for=3 * MAX_COST) for i in range(8)]
    outcomes = race(ledger, worlds)
    assert outcomes.count(None) == 3
    assert outcomes.count(ReasonCode.OVER_LIMIT) == 5
    assert ledger.held_amount(FACILITY.facility_id, NOW) == MicroTrlusd(3 * MAX_COST)


@pytest.mark.parametrize("attempt", range(10))
def test_racing_the_same_nonce_reserves_once(tmp_path: Path, attempt: int) -> None:
    ledger = ReservationLedger(tmp_path / f"nonce-{attempt}.sqlite3")
    outcomes = race(ledger, [PolicyWorld(), PolicyWorld()])
    assert sorted(outcomes, key=str) == sorted([None, ReasonCode.NONCE_REUSED], key=str)
