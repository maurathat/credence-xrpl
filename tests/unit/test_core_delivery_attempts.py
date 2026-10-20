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
    # settled reservations cannot be released
    sub = delivery(p, ref.reservation_id, previous=None)
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
    late = w.expires_at(ref)
    assert w.ledger.expire(late) == (ref.reservation_id,)
    sub = delivery(p, ref.reservation_id, previous=None)
    assert refused(lambda: w.coord.settle(sub, late.plus_seconds(5))) == "EXPIRED"
    assert [d.receipt_digest for d in w.coord.late_delivery_disputes()] == [sub.receipt.digest()]


def test_unauthenticated_late_delivery_keeps_no_evidence(w: World) -> None:
    p, ref = w.reserve(1)
    forged = delivery(p, ref.reservation_id, previous=None, gateway=ATTACKER)
    assert refused(lambda: w.coord.settle(forged, w.expires_at(ref))) == "EXPIRED"
    assert w.coord.late_delivery_disputes() == []


# --- dispute evidence ----------------------------------------------------------------------------


def test_only_authenticated_rejections_keep_evidence(w: World) -> None:
    p1, r1 = w.reserve(1)
    over = delivery(p1, r1.reservation_id, previous=None, price_delta=1)
    assert w.coord.settle(over, NOW.plus_seconds(5)).outcome is SettlementOutcome.DELIVERY_FAILED
    ((kind, reason, evidence),) = w.coord.dispute_evidence(over.receipt.digest())
    assert (kind, reason) == ("delivery_failure", "BILLED_AMOUNT_MISMATCH")
    assert evidence.receipt == over.receipt.canonical_bytes()
    p2, r2 = w.reserve(2)
    forged = delivery(p2, r2.reservation_id, previous=None, gateway=ATTACKER)
    result = w.coord.settle(forged, NOW.plus_seconds(5))
    assert result.outcome is SettlementOutcome.REJECTED_SUBMISSION
    assert w.coord.dispute_evidence(forged.receipt.digest()) == []
    assert len(w.coord.disputes()) == 2
