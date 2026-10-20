"""Settlement coordinator (C3B): atomic accounting settlement on real SQLite, real signatures,
the real C2 policy engine and verifier. Cases A-T of the C3B brief, plus the accounting example,
fault injection and concurrency."""

import dataclasses
import sqlite3
import threading
from dataclasses import replace
from pathlib import Path

import pytest
from c2_world import QUOTE
from c3a_keys import AGENT_REVOKED, ATTACKER, PROVIDER_A
from c3b_world import (
    NOW,
    coordinator,
    current_facility,
    delivery,
    facility,
    purchase,
)

from credence.core.objects import Facility
from credence.core.primitives import (
    MicroTrlusd,
    Nonce,
    ReasonCode,
    ReservationStatus,
    Sha256Digest,
)
from credence.core.reservation_ledger import ReservationLedger, ReservationStateError
from credence.core.settlement_coordinator import (
    FAULT_POINTS,
    DeliverySubmission,
    SettlementCoordinator,
    SettlementOutcome,
    SettlementRefused,
    SettlementState,
    SignedPurchase,
)

OTHER = Sha256Digest.from_hex("cd" * 32)
R = ReasonCode


class World:
    """One registered facility on a fresh database."""

    def __init__(self, tmp_path: Path, base: Facility | None = None, **kwargs: object) -> None:
        self.path = tmp_path / "settlement.sqlite3"
        self.base = base or facility()
        self.coord = coordinator(self.path, self.base, **kwargs)
        self.ledger = ReservationLedger(self.path)

    def reserve(self, index: int, **kwargs: object) -> tuple[SignedPurchase, object]:
        p = purchase(index, current_facility(self.coord, self.base), **kwargs)  # type: ignore[arg-type]
        result = self.coord.reserve(p, NOW)
        assert result.decision.accepted, result.decision.record.reason_code
        assert result.reservation is not None
        return p, result.reservation

    def drawn(self) -> int:
        return self.coord.exposure(self.base.facility_id, NOW).drawn.value

    def held(self) -> int:
        return self.coord.exposure(self.base.facility_id, NOW).held.value

    def status(self, reservation: object) -> ReservationStatus:
        return self.ledger.get(reservation.reservation_id).status  # type: ignore[attr-defined]

    def head(self) -> Sha256Digest | None:
        return self.coord.chain_head(self.base.facility_id)


@pytest.fixture
def w(tmp_path: Path) -> World:
    return World(tmp_path)


def settle_ok(w: World, p: SignedPurchase, reservation: object, **kw: object) -> DeliverySubmission:
    sub = delivery(p, reservation.reservation_id, previous=w.head(), **kw)  # type: ignore[attr-defined, arg-type]
    result = w.coord.settle(sub, NOW.plus_seconds(5))
    assert result.outcome is SettlementOutcome.ACCOUNTING_SETTLED, result.verifier_record
    return sub


# --- A, B: valid settlements -----------------------------------------------------------------


@pytest.mark.parametrize("tier", [1, 2])
def test_valid_settlement(w: World, tier: int) -> None:
    p, reservation = w.reserve(1, tier=tier)
    sub = delivery(p, reservation.reservation_id, previous=None)  # type: ignore[attr-defined]
    assert sub.provider_signature is (None if tier == 1 else sub.provider_signature)
    result = w.coord.settle(sub, NOW.plus_seconds(5))
    assert result.outcome is SettlementOutcome.ACCOUNTING_SETTLED
    assert result.reservation_status is ReservationStatus.SETTLED
    assert result.actual_amount == sub.receipt.price_micro_trlusd == MicroTrlusd(187)
    assert w.drawn() == 187 and w.held() == 0
    assert w.status(reservation) is ReservationStatus.SETTLED
    assert w.head() == sub.receipt.digest()
    assert w.coord.receipt_settled(sub.receipt.digest())
    assert w.coord.disputes() == []


def test_only_accounting_settled_is_written(w: World) -> None:
    p, reservation = w.reserve(1)
    settle_ok(w, p, reservation)
    with sqlite3.connect(w.path) as conn:
        states = {row[0] for row in conn.execute("SELECT state FROM accounting_settlements")}
        settlement_tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert states == {SettlementState.ACCOUNTING_SETTLED.value}
    assert "settlement_records" not in settlement_tables  # no canonical SettlementRecord


# --- accounting example ----------------------------------------------------------------------


def test_accounting_example(tmp_path: Path) -> None:
    w = World(tmp_path, facility(limit=1_000_000, drawn=0))
    quote = replace(
        QUOTE,
        input_price_per_million_micro_trlusd=MicroTrlusd(1_000_000),  # 1 micro-tRLUSD per token
        output_price_per_million_micro_trlusd=MicroTrlusd(0),
        max_input_tokens=100_000,
        max_output_tokens=0,
    )
    p, reservation = w.reserve(1, quote=quote)
    assert reservation.maximum_amount_micro_trlusd == MicroTrlusd(100_000)  # type: ignore[attr-defined]
    assert (w.drawn(), w.held()) == (0, 100_000)
    settle_ok(w, p, reservation, input_tokens=60_000, output_tokens=0)
    exposure = w.coord.exposure(w.base.facility_id, NOW)
    assert exposure.drawn == MicroTrlusd(60_000)
    assert exposure.held == MicroTrlusd(0)
    assert exposure.headroom == 940_000  # the unused 40,000 is not spending


# --- C-J: rejections -------------------------------------------------------------------------


def rejected(w: World, sub: DeliverySubmission) -> tuple[SettlementOutcome, ReasonCode | None]:
    drawn_before = w.drawn()
    result = w.coord.settle(sub, NOW.plus_seconds(5))
    assert w.drawn() == drawn_before
    assert result.actual_amount is None
    return result.outcome, result.verifier_record.reason_code


def test_invalid_gateway_signature_is_rejected_without_release(w: World) -> None:
    p, reservation = w.reserve(1)
    sub = delivery(p, reservation.reservation_id, previous=None, gateway=ATTACKER)  # type: ignore[attr-defined]
    assert rejected(w, sub) == (SettlementOutcome.REJECTED_SUBMISSION, R.RECEIPT_SIG_INVALID)
    assert w.status(reservation) is ReservationStatus.HELD
    assert w.coord.disputes()[0][0] == "rejected_submission"
    settle_ok(w, p, reservation)  # the legitimate delivery still settles afterwards


def test_invalid_provider_signature_is_rejected_without_release(w: World) -> None:
    p, reservation = w.reserve(1)
    sub = delivery(p, reservation.reservation_id, previous=None, provider=PROVIDER_A)  # type: ignore[attr-defined]
    assert rejected(w, sub) == (SettlementOutcome.REJECTED_SUBMISSION, R.RECEIPT_SIG_INVALID)
    assert w.status(reservation) is ReservationStatus.HELD


def test_authenticated_binding_mismatch_is_a_terminal_failure(w: World) -> None:
    p, reservation = w.reserve(1)
    sub = delivery(p, reservation.reservation_id, previous=None, nonce=Nonce("9" * 32))  # type: ignore[attr-defined]
    assert rejected(w, sub) == (SettlementOutcome.DELIVERY_FAILED, R.BINDING_MISMATCH)
    assert w.status(reservation) is ReservationStatus.RELEASED
    kind, rid, *_ = w.coord.disputes()[0]
    assert (kind, rid) == ("delivery_failure", reservation.reservation_id.value)  # type: ignore[attr-defined]


def test_receipt_for_another_purchase_does_not_release(w: World) -> None:
    p1, r1 = w.reserve(1)
    p2, r2 = w.reserve(2)
    foreign = delivery(p2, r1.reservation_id, previous=None)  # type: ignore[attr-defined]
    foreign = replace(foreign, request=p1.request)  # approved objects are r1's; receipt is p2's
    assert rejected(w, foreign) == (SettlementOutcome.REJECTED_SUBMISSION, R.BINDING_MISMATCH)
    assert w.status(r1) is ReservationStatus.HELD


def test_response_digest_mismatch_does_not_release(w: World) -> None:
    """The delivered digest is caller-supplied, so a mismatch cannot free the reservation."""
    p, reservation = w.reserve(1)
    sub = delivery(p, reservation.reservation_id, previous=None, delivered=OTHER)  # type: ignore[attr-defined]
    assert rejected(w, sub) == (SettlementOutcome.REJECTED_SUBMISSION, R.RESPONSE_DIGEST_MISMATCH)
    assert w.status(reservation) is ReservationStatus.HELD


def test_token_count_exceeds_quote_is_terminal(w: World) -> None:
    p, reservation = w.reserve(1)
    sub = delivery(
        p, reservation.reservation_id, previous=None, input_tokens=QUOTE.max_input_tokens + 1
    )  # type: ignore[attr-defined]
    assert rejected(w, sub) == (SettlementOutcome.DELIVERY_FAILED, R.TOKEN_COUNT_EXCEEDS_QUOTE)
    assert w.status(reservation) is ReservationStatus.RELEASED
    assert w.held() == 0


def test_billed_amount_mismatch_is_terminal(w: World) -> None:
    p, reservation = w.reserve(1)
    sub = delivery(p, reservation.reservation_id, previous=None, price_delta=1)  # type: ignore[attr-defined]
    assert rejected(w, sub) == (SettlementOutcome.DELIVERY_FAILED, R.BILLED_AMOUNT_MISMATCH)
    assert w.status(reservation) is ReservationStatus.RELEASED


def test_broken_chain_wrong_and_missing_predecessor(w: World) -> None:
    """I1c: CHAIN_BROKEN alone is not terminal. The hold stays (expiry bounds it), drawn and the
    chain are unchanged, and the authenticated evidence is kept byte for byte."""
    p1, r1 = w.reserve(1)
    first = settle_ok(w, p1, r1)
    drawn = w.drawn()
    p2, r2 = w.reserve(2)
    wrong = delivery(p2, r2.reservation_id, previous=OTHER)  # type: ignore[attr-defined]
    assert rejected(w, wrong) == (SettlementOutcome.REJECTED_SUBMISSION, R.CHAIN_BROKEN)
    p3, r3 = w.reserve(3)
    missing = delivery(p3, r3.reservation_id, previous=None)  # type: ignore[attr-defined]
    assert rejected(w, missing) == (SettlementOutcome.REJECTED_SUBMISSION, R.CHAIN_BROKEN)
    assert w.head() == first.receipt.digest()  # rejected receipts never advance the chain
    assert w.status(r2) is ReservationStatus.HELD and w.status(r3) is ReservationStatus.HELD
    assert w.drawn() == drawn
    for sub in (wrong, missing):
        ((kind, reason, evidence),) = w.coord.dispute_evidence(sub.receipt.digest())
        assert (kind, reason) == ("rejected_submission", "CHAIN_BROKEN")
        assert evidence.receipt == sub.receipt.canonical_bytes()
        assert sub.gateway_signature is not None and sub.provider_attestation is not None
        assert evidence.gateway_signature == sub.gateway_signature.canonical_bytes()
        assert evidence.provider_attestation == sub.provider_attestation.canonical_bytes()
        assert evidence.delivered_response_digest == sub.delivered_response_digest


def test_sequential_chain_settles_in_order(w: World) -> None:
    previous = None
    for index in range(1, 5):
        p, reservation = w.reserve(index)
        sub = settle_ok(w, p, reservation)
        assert sub.receipt.previous_receipt_digest == previous
        previous = sub.receipt.digest()
    assert w.drawn() == 4 * 187


def test_in_flight_receipts_on_one_facility_conflict(w: World) -> None:
    """Why the gateway must issue one receipt at a time per facility."""
    p1, r1 = w.reserve(1)
    p2, r2 = w.reserve(2)
    first = delivery(p1, r1.reservation_id, previous=None)  # type: ignore[attr-defined]
    second = delivery(p2, r2.reservation_id, previous=None)  # type: ignore[attr-defined]
    assert (
        w.coord.settle(first, NOW.plus_seconds(5)).outcome is SettlementOutcome.ACCOUNTING_SETTLED
    )
    assert rejected(w, second) == (SettlementOutcome.REJECTED_SUBMISSION, R.CHAIN_BROKEN)
    assert w.status(r2) is ReservationStatus.HELD  # I1c: not released on CHAIN_BROKEN alone


def test_replayed_receipt(w: World) -> None:
    p1, r1 = w.reserve(1)
    sub = settle_ok(w, p1, r1)
    drawn = w.drawn()
    with pytest.raises(SettlementRefused) as exc:
        w.coord.settle(sub, NOW.plus_seconds(6))  # same reservation again
    assert exc.value.reason == "NOT_HELD"
    p2, r2 = w.reserve(2)
    replay = replace(sub, reservation_id=r2.reservation_id, request=p2.request)  # type: ignore[attr-defined]
    outcome, reason = rejected(w, replay)
    # C2 precedence: the receipt binds to request 1, so BINDING_MISMATCH fires before CHAIN_BROKEN
    # and REPLAYED_RECEIPT. It is not rewritten into REPLAYED_RECEIPT.
    assert (outcome, reason) == (SettlementOutcome.REJECTED_SUBMISSION, R.BINDING_MISMATCH)
    assert w.drawn() == drawn
    assert w.status(r2) is ReservationStatus.HELD


# --- K, L, M, N: refusals --------------------------------------------------------------------


def test_expired_reservation_refuses_and_never_settles_later(w: World) -> None:
    p, reservation = w.reserve(1)
    sub = delivery(p, reservation.reservation_id, previous=None)  # type: ignore[attr-defined]
    expires_at = reservation.expires_at  # type: ignore[attr-defined]
    with pytest.raises(SettlementRefused) as exc:
        w.coord.settle(sub, expires_at)
    assert exc.value.reason == "EXPIRED"
    assert w.status(reservation) is ReservationStatus.RELEASED
    assert w.ledger.release_reason(reservation.reservation_id) == "expired"  # type: ignore[attr-defined]
    assert (w.drawn(), w.held()) == (0, 0)
    with pytest.raises(SettlementRefused) as exc:
        w.coord.settle(sub, expires_at.plus_seconds(-10))  # a delayed receipt
    assert exc.value.reason == "NOT_HELD"


def test_settlement_one_second_before_expiry_succeeds(w: World) -> None:
    p, reservation = w.reserve(1)
    sub = delivery(p, reservation.reservation_id, previous=None)  # type: ignore[attr-defined]
    at = reservation.expires_at.plus_seconds(-1)  # type: ignore[attr-defined]
    assert w.coord.settle(sub, at).outcome is SettlementOutcome.ACCOUNTING_SETTLED


def test_amount_above_reservation_is_refused_atomically(w: World) -> None:
    p, reservation = w.reserve(1)
    with sqlite3.connect(w.path) as conn:  # simulate a corrupted reservation row
        conn.execute(
            "UPDATE reservations SET maximum_amount_micro_trlusd = 100 WHERE reservation_id = ?",
            (reservation.reservation_id.value,),  # type: ignore[attr-defined]
        )
    sub = delivery(p, reservation.reservation_id, previous=None)  # type: ignore[attr-defined]
    with pytest.raises(SettlementRefused) as exc:
        w.coord.settle(sub, NOW.plus_seconds(5))
    assert exc.value.reason == "AMOUNT_EXCEEDS_RESERVATION"
    assert w.drawn() == 0 and w.status(reservation) is ReservationStatus.HELD
    assert not w.coord.receipt_settled(sub.receipt.digest())


def test_wrong_facility(tmp_path: Path) -> None:
    w = World(tmp_path)
    unregistered = replace(w.base, facility_id=type(w.base.facility_id)("facility-unregistered"))
    with pytest.raises(SettlementRefused) as exc:
        w.coord.reserve(purchase(1, unregistered), NOW)
    assert exc.value.reason == "UNACCOUNTED_FACILITY"
    p, reservation = w.reserve(1)
    other = purchase(2, unregistered)
    sub = replace(
        delivery(p, reservation.reservation_id, previous=None),  # type: ignore[attr-defined]
        request=other.request,
    )
    with pytest.raises(SettlementRefused) as exc:
        w.coord.settle(sub, NOW.plus_seconds(5))
    assert exc.value.reason == "WRONG_PURCHASE"


@pytest.mark.parametrize("member", ["request", "quote", "service_class", "execution_profile"])
def test_wrong_purchase_objects_are_refused(w: World, member: str) -> None:
    p, reservation = w.reserve(1)
    p2, _ = w.reserve(2, quote=replace(QUOTE, max_output_tokens=2000))
    sub = delivery(p, reservation.reservation_id, previous=None)  # type: ignore[attr-defined]
    swapped = replace(sub, **{member: getattr(p2, member)})
    if member == "execution_profile":
        swapped = replace(sub, execution_profile=replace(p.execution_profile, runtime="other"))
    if member == "service_class":
        swapped = replace(sub, service_class=replace(p.service_class, latency_p95_ms=1))
    with pytest.raises(SettlementRefused) as exc:
        w.coord.settle(swapped, NOW.plus_seconds(5))
    assert exc.value.reason == "WRONG_PURCHASE"
    assert w.drawn() == 0 and w.status(reservation) is ReservationStatus.HELD


def test_unknown_reservation(w: World) -> None:
    p, reservation = w.reserve(1)
    sub = replace(
        delivery(p, reservation.reservation_id, previous=None),  # type: ignore[attr-defined]
        reservation_id=type(reservation.reservation_id)("reservation-unknown"),  # type: ignore[attr-defined]
    )
    with pytest.raises(SettlementRefused) as exc:
        w.coord.settle(sub, NOW.plus_seconds(5))
    assert exc.value.reason == "NOT_FOUND"


# --- reserve-side guarantees -----------------------------------------------------------------


def test_reserve_uses_signatures_not_caller_facts(w: World) -> None:
    fields = {f.name for f in dataclasses.fields(SignedPurchase)} | {
        f.name for f in dataclasses.fields(DeliverySubmission)
    }
    facts = {"request_signature_valid", "working_key_current", "gateway_signature_valid"}
    assert not fields & (facts | {"provider_signature_fact", "receipt_already_settled"})
    forged = purchase(1, w.base, signer=ATTACKER)
    assert w.coord.reserve(forged, NOW).decision.record.reason_code is R.SIG_INVALID
    revoked = purchase(2, w.base, signer=AGENT_REVOKED)
    assert w.coord.reserve(revoked, NOW).decision.record.reason_code is R.KEY_REVOKED


def test_stale_or_conflicting_facility_accounting_is_refused(w: World) -> None:
    p, reservation = w.reserve(1)
    settle_ok(w, p, reservation)
    stale = purchase(2, w.base)  # still shows drawn 0
    with pytest.raises(ReservationStateError) as exc:
        w.coord.reserve(stale, NOW)
    assert exc.value.reason == "STALE_FACILITY_ACCOUNTING"
    with pytest.raises(ReservationStateError) as exc:  # the bare C2 ledger path is guarded too
        w.ledger.reserve(_context(purchase(3, w.base)), NOW)
    assert exc.value.reason == "STALE_FACILITY_ACCOUNTING"
    raised = purchase(
        4, replace(current_facility(w.coord, w.base), limit_micro_trlusd=MicroTrlusd(10**9))
    )
    with pytest.raises(ReservationStateError):
        w.coord.reserve(raised, NOW)


def _context(p: SignedPurchase):  # type: ignore[no-untyped-def]
    from credence.core.reservation_ledger import PurchaseContext

    return PurchaseContext(
        request=p.request,
        request_signature_valid=True,
        working_key_current=True,
        facility=p.facility,
        quote=p.quote,
        service_class=p.service_class,
        execution_profile=p.execution_profile,
        procurement_classes=p.procurement_classes,
    )


def test_bare_ledger_settle_is_refused_for_accounted_facilities(w: World) -> None:
    _, reservation = w.reserve(1)
    with pytest.raises(ReservationStateError) as exc:
        w.ledger.settle(reservation.reservation_id, NOW.plus_seconds(5))  # type: ignore[attr-defined]
    assert exc.value.reason == "USE_SETTLEMENT_COORDINATOR"
    assert w.status(reservation) is ReservationStatus.HELD and w.drawn() == 0


def test_register_facility_once(w: World) -> None:
    with pytest.raises(SettlementRefused) as exc:
        w.coord.register_facility(w.base)
    assert exc.value.reason == "FACILITY_ALREADY_REGISTERED"


# --- Q: fault injection ----------------------------------------------------------------------


class Injected(RuntimeError):
    pass


@pytest.mark.parametrize("point", FAULT_POINTS)
def test_failure_at_every_transition_point_rolls_back(tmp_path: Path, point: str) -> None:
    def fault(at: str) -> None:
        if at == point:
            raise Injected(at)

    w = World(tmp_path, fault=fault)
    p, reservation = w.reserve(1)
    sub = delivery(p, reservation.reservation_id, previous=None)  # type: ignore[attr-defined]
    with pytest.raises(Injected):
        w.coord.settle(sub, NOW.plus_seconds(5))
    assert w.drawn() == 0
    assert w.status(reservation) is ReservationStatus.HELD
    assert not w.coord.receipt_settled(sub.receipt.digest())
    assert w.head() is None
    healthy = SettlementCoordinator(w.ledger, w.coord._registry)
    assert healthy.settle(sub, NOW.plus_seconds(6)).outcome is SettlementOutcome.ACCOUNTING_SETTLED


# --- R: restart ------------------------------------------------------------------------------


def test_restart_then_replay(tmp_path: Path) -> None:
    w = World(tmp_path)
    p, reservation = w.reserve(1)
    sub = settle_ok(w, p, reservation)
    restarted = coordinator(w.path)  # new process: same file, no registration
    assert restarted.receipt_settled(sub.receipt.digest())
    assert restarted.chain_head(w.base.facility_id) == sub.receipt.digest()
    with pytest.raises(SettlementRefused) as exc:
        restarted.settle(sub, NOW.plus_seconds(6))
    assert exc.value.reason == "NOT_HELD"
    assert restarted.exposure(w.base.facility_id, NOW).drawn == MicroTrlusd(187)
    with pytest.raises(sqlite3.IntegrityError):  # the receipt identity is unique in storage
        with sqlite3.connect(w.path) as conn:
            conn.execute(
                "INSERT INTO accounting_settlements VALUES (?, 'r', 'f', 'd', 1, 't', "
                "'accounting_settled')",
                (sub.receipt.digest().value,),
            )


# --- O, P, S: concurrency --------------------------------------------------------------------


def run_threads(targets: list[object]) -> list[object]:
    barrier = threading.Barrier(len(targets))
    results: list[object] = [None] * len(targets)

    def run(i: int, fn: object) -> None:
        barrier.wait()
        try:
            results[i] = fn()  # type: ignore[operator]
        except Exception as exc:  # recorded for assertions
            results[i] = exc

    threads = [threading.Thread(target=run, args=(i, t)) for i, t in enumerate(targets)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


@pytest.mark.parametrize("attempt", range(15))
def test_two_workers_settle_one_reservation(tmp_path: Path, attempt: int) -> None:
    w = World(tmp_path)
    p, reservation = w.reserve(1)
    sub = delivery(p, reservation.reservation_id, previous=None)  # type: ignore[attr-defined]
    results = run_threads([lambda: w.coord.settle(sub, NOW.plus_seconds(5))] * 2)
    settled = [r for r in results if not isinstance(r, Exception)]
    refused = [r for r in results if isinstance(r, SettlementRefused)]
    assert len(settled) == 1 and len(refused) == 1 and refused[0].reason == "NOT_HELD"
    assert w.drawn() == 187


@pytest.mark.parametrize("attempt", range(15))
def test_two_workers_settle_one_receipt_against_two_reservations(
    tmp_path: Path, attempt: int
) -> None:
    w = World(tmp_path)
    p1, r1 = w.reserve(1)
    p2, r2 = w.reserve(2)
    sub = delivery(p1, r1.reservation_id, previous=None)  # type: ignore[attr-defined]
    stolen = replace(sub, reservation_id=r2.reservation_id, request=p2.request)  # type: ignore[attr-defined]
    results = run_threads(
        [
            lambda: w.coord.settle(sub, NOW.plus_seconds(5)),
            lambda: w.coord.settle(stolen, NOW.plus_seconds(5)),
        ]
    )
    outcomes = sorted(r.outcome.value for r in results)  # type: ignore[attr-defined]
    assert outcomes == ["accounting_settled", "rejected_submission"]
    assert w.drawn() == 187
    assert w.status(r2) is ReservationStatus.HELD


@pytest.mark.parametrize("attempt", range(15))
def test_concurrent_settle_and_expire_respect_the_boundary(tmp_path: Path, attempt: int) -> None:
    w = World(tmp_path)
    p, reservation = w.reserve(1)
    sub = delivery(p, reservation.reservation_id, previous=None)  # type: ignore[attr-defined]
    expires_at = reservation.expires_at  # type: ignore[attr-defined]
    results = run_threads(
        [
            lambda: w.coord.settle(sub, expires_at.plus_seconds(-1)),
            lambda: w.ledger.expire(expires_at),
        ]
    )
    status = w.status(reservation)
    if status is ReservationStatus.SETTLED:
        assert w.drawn() == 187 and results[1] == ()
    else:
        assert status is ReservationStatus.RELEASED
        assert isinstance(results[0], SettlementRefused) and results[0].reason == "NOT_HELD"
        assert w.drawn() == 0
    assert w.held() == 0


@pytest.mark.parametrize("attempt", range(8))
def test_concurrent_reserve_and_settle_never_exceed_the_limit(tmp_path: Path, attempt: int) -> None:
    max_cost = 2458
    w = World(tmp_path, facility(limit=3 * max_cost))
    first = [w.reserve(i) for i in (1, 2, 3)]
    violations: list[str] = []
    stop = threading.Event()

    def monitor() -> None:
        while not stop.is_set():
            e = w.coord.exposure(w.base.facility_id, NOW)
            if e.drawn.value + e.held.value > e.limit.value:
                violations.append(str(e))

    def settle_chain() -> None:
        for p, reservation in first:
            settle_ok(w, p, reservation)

    def reserver(index: int) -> object:
        for _ in range(200):
            try:
                result = w.coord.reserve(purchase(index, current_facility(w.coord, w.base)), NOW)
                return result.decision.record.reason_code
            except ReservationStateError as exc:  # stale drawn: re-read and retry
                assert exc.reason == "STALE_FACILITY_ACCOUNTING"
        return "gave-up"

    watcher = threading.Thread(target=monitor)
    watcher.start()
    results = run_threads([settle_chain, *(lambda i=i: reserver(i) for i in (10, 11, 12, 13))])
    stop.set()
    watcher.join()
    assert not violations
    assert all(r in (None, R.OVER_LIMIT) for r in results[1:]), results
    final = w.coord.exposure(w.base.facility_id, NOW)
    assert final.drawn.value + final.held.value <= final.limit.value
    assert final.drawn == MicroTrlusd(3 * 187)
