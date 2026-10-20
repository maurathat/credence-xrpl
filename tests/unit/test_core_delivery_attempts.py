"""Settlement coordinator, Integration I1c amendments (contracts/requests/0001).

Delivery attempts, the audited no-delivery release, unknown delivery outcomes, authenticated
late-delivery disputes and dispute evidence. Real SQLite, real signatures (TEST keys, NOT FOR
PRODUCTION), the real policy engine and verifier. No XRPL.
"""

from pathlib import Path

import pytest
from c3a_keys import ATTACKER, registry
from c3b_world import NOW, coordinator, current_facility, delivery, facility, purchase

from credence.core.objects import Facility
from credence.core.primitives import ReservationStatus, Timestamp
from credence.core.reservation_ledger import (
    PurchaseContext,
    ReservationLedger,
    ReservationStateError,
)
from credence.core.settlement_coordinator import (
    NoDeliveryReason,
    PurchaseReference,
    SettlementCoordinator,
    SettlementOutcome,
    SettlementRefused,
    SignedPurchase,
    UnknownDeliveryCause,
)


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.path = tmp_path / "settlement.sqlite3"
        self.base: Facility = facility()
        self.coord: SettlementCoordinator = coordinator(self.path, self.base)
        self.ledger = ReservationLedger(self.path)

    def reserve(self, index: int) -> tuple[SignedPurchase, PurchaseReference]:
        p = purchase(index, current_facility(self.coord, self.base))
        result = self.coord.reserve(p, NOW)
        assert result.reservation is not None
        ref = PurchaseReference(
            result.reservation.reservation_id,
            p.request,
            p.quote,
            p.service_class,
            p.execution_profile,
        )
        return p, ref

    def status(self, ref: PurchaseReference) -> ReservationStatus:
        return self.ledger.get(ref.reservation_id).status

    def held(self) -> int:
        return self.coord.exposure(self.base.facility_id, NOW).held.value

    def drawn(self) -> int:
        return self.coord.exposure(self.base.facility_id, NOW).drawn.value

    def expires_at(self, ref: PurchaseReference) -> Timestamp:
        return self.ledger.get(ref.reservation_id).expires_at


@pytest.fixture
def w(tmp_path: Path) -> World:
    return World(tmp_path)


def refused(call: object) -> str:
    with pytest.raises(SettlementRefused) as exc:
        call()  # type: ignore[operator]
    return exc.value.reason


# --- delivery attempts ---------------------------------------------------------------------------


def test_begin_delivery_records_one_attempt(w: World) -> None:
    _, ref = w.reserve(1)
    attempt = w.coord.begin_delivery(ref, NOW.plus_seconds(1))
    assert attempt.reservation_id == ref.reservation_id
    assert attempt.request_digest == ref.request.digest()
    assert w.coord.delivery_attempt(ref.reservation_id) == attempt
    assert refused(lambda: w.coord.begin_delivery(ref, NOW.plus_seconds(2))) == (
        "DELIVERY_ALREADY_ATTEMPTED"
    )
    assert w.status(ref) is ReservationStatus.HELD


def test_begin_delivery_refusals(w: World) -> None:
    _, ref = w.reserve(1)
    _, other = w.reserve(2)
    wrong = PurchaseReference(
        ref.reservation_id, other.request, ref.quote, ref.service_class, ref.execution_profile
    )
    assert refused(lambda: w.coord.begin_delivery(wrong, NOW)) == "WRONG_PURCHASE"
    missing = PurchaseReference(
        type(ref.reservation_id)("reservation-" + "0" * 32),
        ref.request,
        ref.quote,
        ref.service_class,
        ref.execution_profile,
    )
    assert refused(lambda: w.coord.begin_delivery(missing, NOW)) == "NOT_FOUND"
    assert refused(lambda: w.coord.begin_delivery(ref, w.expires_at(ref))) == "EXPIRED"
    assert w.status(ref) is ReservationStatus.RELEASED
    assert w.ledger.release_reason(ref.reservation_id) == "expired"
    assert w.coord.delivery_attempt(ref.reservation_id) is None


# --- no-delivery release -------------------------------------------------------------------------


def test_release_undelivered_is_audited_and_exclusive(w: World) -> None:
    _, ref = w.reserve(1)
    assert w.held() > 0
    w.coord.release_undelivered(ref, NoDeliveryReason.PROVIDER_NOT_CONNECTED, NOW.plus_seconds(1))
    assert w.status(ref) is ReservationStatus.RELEASED
    assert w.ledger.release_reason(ref.reservation_id) == "released"
    assert (w.held(), w.drawn()) == (0, 0)
    (audit,) = w.coord.no_delivery_releases()
    assert audit.reservation_id == ref.reservation_id
    assert audit.request_digest == ref.request.digest()
    assert audit.reason is NoDeliveryReason.PROVIDER_NOT_CONNECTED
    assert audit.released_at == NOW.plus_seconds(1)
    # once released, no attempt can begin and nothing can settle
    assert refused(lambda: w.coord.begin_delivery(ref, NOW.plus_seconds(2))) == "NOT_HELD"
    p = purchase(1, current_facility(w.coord, w.base))
    sub = delivery(p, ref.reservation_id, previous=None)
    assert refused(lambda: w.coord.settle(sub, NOW.plus_seconds(3))) == "NOT_HELD"
    assert refused(
        lambda: w.coord.release_undelivered(
            ref, NoDeliveryReason.PROVIDER_NOT_CONNECTED, NOW.plus_seconds(4)
        )
    ) == ("NOT_HELD")


def test_release_is_refused_once_a_delivery_attempt_exists(w: World) -> None:
    """A caller's claim that delivery failed is not evidence once Core authorized execution."""
    _, ref = w.reserve(1)
    w.coord.begin_delivery(ref, NOW.plus_seconds(1))
    reason = refused(
        lambda: w.coord.release_undelivered(
            ref, NoDeliveryReason.ABANDONED_BEFORE_EXECUTION, NOW.plus_seconds(2)
        )
    )
    assert reason == "DELIVERY_ATTEMPTED"
    assert w.status(ref) is ReservationStatus.HELD and w.coord.no_delivery_releases() == []


def test_invalid_release_attempts(w: World) -> None:
    p, ref = w.reserve(1)
    _, other = w.reserve(2)
    wrong = PurchaseReference(
        ref.reservation_id, other.request, ref.quote, ref.service_class, ref.execution_profile
    )
    release = w.coord.release_undelivered
    assert (
        refused(lambda: release(wrong, NoDeliveryReason.PROVIDER_NOT_CONNECTED, NOW))
        == "WRONG_PURCHASE"
    )
    assert (
        refused(lambda: release(ref, "provider_not_connected", NOW))  # type: ignore[arg-type]
        == "INVALID_RELEASE_REASON"
    )
    # I1e (Core finding C-1): a valid delivery cannot settle without Core's persisted attempt
    sub = delivery(p, ref.reservation_id, previous=None)
    assert refused(lambda: w.coord.settle(sub, NOW.plus_seconds(5))) == "NO_DELIVERY_ATTEMPT"
    assert w.drawn() == 0 and w.status(ref) is ReservationStatus.HELD
    # with the attempt recorded it settles, and a settled reservation cannot be released
    w.coord.begin_delivery(ref, NOW.plus_seconds(1))
    assert w.coord.settle(sub, NOW.plus_seconds(5)).outcome is SettlementOutcome.ACCOUNTING_SETTLED
    assert refused(lambda: release(ref, NoDeliveryReason.PROVIDER_NOT_CONNECTED, NOW)) == "NOT_HELD"
    # expired: lapses as expired, not as a no-delivery release
    assert (
        refused(
            lambda: release(other, NoDeliveryReason.PROVIDER_NOT_CONNECTED, w.expires_at(other))
        )
        == "EXPIRED"
    )
    assert w.ledger.release_reason(other.reservation_id) == "expired"
    assert w.coord.no_delivery_releases() == []
    assert w.status(other) is ReservationStatus.RELEASED


def test_release_on_an_unregistered_facility_is_refused(tmp_path: Path) -> None:
    w = World(tmp_path)
    unregistered = SettlementCoordinator(ReservationLedger(tmp_path / "bare.sqlite3"), registry())
    ledger = ReservationLedger(tmp_path / "bare.sqlite3")
    p = purchase(1, w.base)
    result = ledger.reserve(
        PurchaseContext(
            request=p.request,
            request_signature_valid=True,
            working_key_current=True,
            facility=p.facility,
            quote=p.quote,
            service_class=p.service_class,
            execution_profile=p.execution_profile,
            procurement_classes=p.procurement_classes,
        ),
        NOW,
    )
    assert result.reservation is not None
    ref = PurchaseReference(
        result.reservation.reservation_id, p.request, p.quote, p.service_class, p.execution_profile
    )
    assert (
        refused(
            lambda: unregistered.release_undelivered(
                ref, NoDeliveryReason.PROVIDER_NOT_CONNECTED, NOW
            )
        )
        == "UNACCOUNTED_FACILITY"
    )
    assert refused(lambda: unregistered.begin_delivery(ref, NOW)) == "UNACCOUNTED_FACILITY"


def test_bare_ledger_release_is_refused_for_accounted_facilities(w: World) -> None:
    _, ref = w.reserve(1)
    with pytest.raises(ReservationStateError) as exc:
        w.ledger.release(ref.reservation_id, NOW.plus_seconds(1))
    assert exc.value.reason == "USE_SETTLEMENT_COORDINATOR"
    assert w.status(ref) is ReservationStatus.HELD


# --- unknown delivery outcome --------------------------------------------------------------------


def test_unknown_delivery_keeps_the_hold_and_charges_nothing(w: World) -> None:
    _, ref = w.reserve(1)
    held = w.held()
    assert (
        refused(
            lambda: w.coord.record_unknown_delivery(
                ref.reservation_id, UnknownDeliveryCause.INTERRUPTED, NOW
            )
        )
        == "NO_DELIVERY_ATTEMPT"
    )
    w.coord.begin_delivery(ref, NOW.plus_seconds(1))
    attempt = w.coord.record_unknown_delivery(
        ref.reservation_id, UnknownDeliveryCause.PROVIDER_EXECUTION_ERROR, NOW.plus_seconds(2)
    )
    assert attempt.unknown_cause is UnknownDeliveryCause.PROVIDER_EXECUTION_ERROR
    again = w.coord.record_unknown_delivery(
        ref.reservation_id, UnknownDeliveryCause.INTERRUPTED, NOW.plus_seconds(3)
    )
    assert again.unknown_cause is UnknownDeliveryCause.PROVIDER_EXECUTION_ERROR  # first kept
    assert w.status(ref) is ReservationStatus.HELD
    assert (w.held(), w.drawn()) == (held, 0)
    assert w.coord.disputes() == [] and w.coord.chain_head(w.base.facility_id) is None
    assert (
        refused(
            lambda: w.coord.record_unknown_delivery(ref.reservation_id, "interrupted", NOW)  # type: ignore[arg-type]
        )
        == "INVALID_UNKNOWN_CAUSE"
    )


# --- late delivery -------------------------------------------------------------------------------


def test_authenticated_late_delivery_is_disputed_with_evidence_never_settled(w: World) -> None:
    p, ref = w.reserve(1)
    w.coord.begin_delivery(ref, NOW.plus_seconds(1))
    sub = delivery(p, ref.reservation_id, previous=None)
    late = w.expires_at(ref)
    assert refused(lambda: w.coord.settle(sub, late)) == "EXPIRED"
    assert w.ledger.release_reason(ref.reservation_id) == "expired"
    assert w.drawn() == 0 and w.coord.chain_head(w.base.facility_id) is None
    assert w.coord.disputes() == []  # verifier-rejection rows are unchanged (C3B transcripts)
    (record,) = w.coord.late_delivery_disputes()
    assert record.receipt_digest == sub.receipt.digest()
    assert record.reservation_id == ref.reservation_id
    assert (record.expires_at, record.submitted_at) == (late, late)
    assert record.evidence.receipt == sub.receipt.canonical_bytes()
    assert sub.gateway_signature is not None and sub.provider_signature is not None
    assert record.evidence.gateway_signature == sub.gateway_signature.canonical_bytes()
    assert record.evidence.provider_signature == sub.provider_signature.canonical_bytes()
    # resubmitted later: still refused, still recorded once
    assert refused(lambda: w.coord.settle(sub, late.plus_seconds(30))) == "EXPIRED"
    assert len(w.coord.late_delivery_disputes()) == 1
    # a resubmission timestamped before expiry finds the hold gone (C3B fixture behaviour)
    assert refused(lambda: w.coord.settle(sub, late.plus_seconds(-10))) == "NOT_HELD"


def test_late_delivery_after_an_expiry_sweep_is_still_disputed(w: World) -> None:
    p, ref = w.reserve(1)
    w.coord.begin_delivery(ref, NOW.plus_seconds(1))
    late = w.expires_at(ref)
    assert w.ledger.expire(late) == (ref.reservation_id,)
    sub = delivery(p, ref.reservation_id, previous=None)
    assert refused(lambda: w.coord.settle(sub, late.plus_seconds(5))) == "EXPIRED"
    assert [d.receipt_digest for d in w.coord.late_delivery_disputes()] == [sub.receipt.digest()]


def test_unauthenticated_late_delivery_keeps_no_evidence(w: World) -> None:
    p, ref = w.reserve(1)
    w.coord.begin_delivery(ref, NOW.plus_seconds(1))
    forged = delivery(p, ref.reservation_id, previous=None, gateway=ATTACKER)
    assert refused(lambda: w.coord.settle(forged, w.expires_at(ref))) == "EXPIRED"
    assert w.coord.late_delivery_disputes() == []


# --- dispute evidence ----------------------------------------------------------------------------


def test_only_authenticated_rejections_keep_evidence(w: World) -> None:
    p1, r1 = w.reserve(1)
    w.coord.begin_delivery(r1, NOW.plus_seconds(1))
    over = delivery(p1, r1.reservation_id, previous=None, price_delta=1)
    assert w.coord.settle(over, NOW.plus_seconds(5)).outcome is SettlementOutcome.DELIVERY_FAILED
    ((kind, reason, evidence),) = w.coord.dispute_evidence(over.receipt.digest())
    assert (kind, reason) == ("delivery_failure", "BILLED_AMOUNT_MISMATCH")
    assert evidence.receipt == over.receipt.canonical_bytes()
    p2, r2 = w.reserve(2)
    w.coord.begin_delivery(r2, NOW.plus_seconds(1))
    forged = delivery(p2, r2.reservation_id, previous=None, gateway=ATTACKER)
    result = w.coord.settle(forged, NOW.plus_seconds(5))
    assert result.outcome is SettlementOutcome.REJECTED_SUBMISSION
    assert w.coord.dispute_evidence(forged.receipt.digest()) == []
    assert len(w.coord.disputes()) == 2


# --- I1e: authorized execution (Core findings C-1, C-2) -----------------------------------------


def test_missing_attempt_late_delivery_is_refused_and_not_recorded(w: World) -> None:
    """A late delivery Core never authorized keeps no evidence and is refused as unauthorized."""
    p, ref = w.reserve(1)
    sub = delivery(p, ref.reservation_id, previous=None)
    late = w.expires_at(ref)
    assert refused(lambda: w.coord.settle(sub, late)) == "NO_DELIVERY_ATTEMPT"
    assert w.ledger.release_reason(ref.reservation_id) == "expired"  # expiry still committed
    assert w.coord.late_delivery_disputes() == [] and w.drawn() == 0


def test_claim_execution_requires_the_persisted_attempt(w: World) -> None:
    from dataclasses import replace

    from credence.core.settlement_coordinator import DeliveryAttempt

    _, ref = w.reserve(1)
    fabricated = DeliveryAttempt(
        reservation_id=ref.reservation_id,
        facility_id=ref.request.facility_id,
        request_digest=ref.request.digest(),
        begun_at=NOW,
        unknown_cause=None,
    )
    assert refused(lambda: w.coord.claim_execution(fabricated, NOW)) == "NO_DELIVERY_ATTEMPT"
    attempt = w.coord.begin_delivery(ref, NOW.plus_seconds(1))
    assert refused(lambda: w.coord.claim_execution(fabricated, NOW)) == "ATTEMPT_MISMATCH"
    forged = replace(attempt, request_digest=ref.quote.digest())
    assert refused(lambda: w.coord.claim_execution(forged, NOW)) == "ATTEMPT_MISMATCH"
    claimed = w.coord.claim_execution(attempt, NOW.plus_seconds(2))
    assert claimed.execution_claimed_at == NOW.plus_seconds(2)
    assert w.coord.delivery_attempt(ref.reservation_id) == claimed
    assert (
        refused(lambda: w.coord.claim_execution(attempt, NOW.plus_seconds(3)))
        == "EXECUTION_ALREADY_CLAIMED"
    )


def test_claim_is_refused_after_unknown_release_or_expiry(w: World) -> None:
    _, ref = w.reserve(1)
    attempt = w.coord.begin_delivery(ref, NOW.plus_seconds(1))
    w.coord.record_unknown_delivery(
        ref.reservation_id, UnknownDeliveryCause.INTERRUPTED, NOW.plus_seconds(2)
    )
    assert (
        refused(lambda: w.coord.claim_execution(attempt, NOW.plus_seconds(3)))
        == "DELIVERY_OUTCOME_UNKNOWN"
    )
    _, late = w.reserve(2)
    late_attempt = w.coord.begin_delivery(late, NOW.plus_seconds(1))
    assert refused(lambda: w.coord.claim_execution(late_attempt, w.expires_at(late))) == "EXPIRED"
    assert w.ledger.release_reason(late.reservation_id) == "expired"


def test_settlement_needs_the_attempt_for_this_request(w: World) -> None:
    import sqlite3

    p, ref = w.reserve(1)
    w.coord.begin_delivery(ref, NOW.plus_seconds(1))
    with sqlite3.connect(w.path) as conn:  # tamper: the attempt names a different request
        conn.execute(
            "UPDATE delivery_attempts SET request_digest = ? WHERE reservation_id = ?",
            (ref.quote.digest().value, ref.reservation_id.value),
        )
    sub = delivery(p, ref.reservation_id, previous=None)
    assert refused(lambda: w.coord.settle(sub, NOW.plus_seconds(5))) == "ATTEMPT_MISMATCH"
    assert w.drawn() == 0


def test_forged_signature_cannot_reserve_or_consume_a_nonce_on_a_registered_facility(
    w: World,
) -> None:
    """Core finding C-2: the bare ledger with caller-supplied facts is closed; the coordinator
    computes facts itself, so a forged signature consumes neither headroom nor the nonce."""
    forged = purchase(7, current_facility(w.coord, w.base), signer=ATTACKER)
    context = PurchaseContext(
        request=forged.request,
        request_signature_valid=True,  # the lie the bare ledger used to accept
        working_key_current=True,
        facility=forged.facility,
        quote=forged.quote,
        service_class=forged.service_class,
        execution_profile=forged.execution_profile,
        procurement_classes=forged.procurement_classes,
    )
    for call in (
        lambda: w.ledger.reserve(context, NOW),
        lambda: _in_transaction(w, context),
    ):
        with pytest.raises(ReservationStateError) as exc:
            call()
        assert exc.value.reason == "USE_SETTLEMENT_COORDINATOR"
    rejected = w.coord.reserve(forged, NOW)
    assert rejected.reservation is None
    assert rejected.decision.record.reason_code is not None
    assert rejected.decision.record.reason_code.value == "SIG_INVALID"
    assert w.held() == 0
    assert not w.ledger.nonce_used(w.base.facility_id, forged.request.nonce)
    # the legitimate agent's purchase with the same nonce still reserves
    legit = purchase(7, current_facility(w.coord, w.base))
    assert legit.request.nonce == forged.request.nonce
    accepted = w.coord.reserve(legit, NOW)
    assert accepted.reservation is not None and w.held() > 0


def _in_transaction(w: World, context: PurchaseContext) -> None:
    with w.ledger.write_transaction() as conn:
        w.ledger.reserve_in_transaction(conn, context, NOW)


def _race_once(path: Path) -> str:
    """begin_delivery and release_undelivered start together on separate connections."""
    import threading

    w = World(path)
    _, ref = w.reserve(1)
    barrier = threading.Barrier(2)
    outcomes: dict[str, str] = {}

    def begin() -> None:
        barrier.wait()
        try:
            w.coord.begin_delivery(ref, NOW.plus_seconds(1))
            outcomes["begin"] = "ok"
        except SettlementRefused as exc:
            outcomes["begin"] = exc.reason

    def release() -> None:
        barrier.wait()
        try:
            w.coord.release_undelivered(
                ref, NoDeliveryReason.ABANDONED_BEFORE_EXECUTION, NOW.plus_seconds(1)
            )
            outcomes["release"] = "ok"
        except SettlementRefused as exc:
            outcomes["release"] = exc.reason

    threads = [threading.Thread(target=begin), threading.Thread(target=release)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    attempt = w.coord.delivery_attempt(ref.reservation_id)
    if outcomes["begin"] == "ok":
        assert outcomes["release"] == "DELIVERY_ATTEMPTED"
        assert attempt is not None and w.status(ref) is ReservationStatus.HELD
        assert w.coord.no_delivery_releases() == []
        return "begin"
    assert (outcomes["release"], outcomes["begin"]) == ("ok", "NOT_HELD")
    assert attempt is None and w.status(ref) is ReservationStatus.RELEASED
    assert len(w.coord.no_delivery_releases()) == 1
    return "release"


def test_begin_delivery_and_release_race_has_exactly_one_winner(tmp_path: Path) -> None:
    """Both run in BEGIN IMMEDIATE transactions; exactly one wins in every round and the loser
    sees the winner's committed state. Never both an attempt and a no-delivery release."""
    winners = []
    for round_ in range(40):
        (tmp_path / f"race-{round_}").mkdir()
        winners.append(_race_once(tmp_path / f"race-{round_}"))
    assert set(winners) <= {"begin", "release"}


def test_valid_signed_delivery_without_a_core_attempt_cannot_settle(w: World) -> None:
    """Adversarial case 2 / Core finding C-1: a fully valid, gateway- and provider-signed Tier 2
    delivery for a held reservation, but no persisted attempt. Before I1e it settled (drawn rose);
    now it is refused before the verifier, charging nothing and recording no dispute."""
    p, ref = w.reserve(1)
    sub = delivery(p, ref.reservation_id, previous=None)
    assert sub.gateway_signature is not None and sub.provider_signature is not None
    assert refused(lambda: w.coord.settle(sub, NOW.plus_seconds(5))) == "NO_DELIVERY_ATTEMPT"
    assert w.drawn() == 0 and w.coord.disputes() == []
    assert w.coord.chain_head(w.base.facility_id) is None
    assert not w.coord.receipt_settled(sub.receipt.digest())
    assert w.status(ref) is ReservationStatus.HELD
