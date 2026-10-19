"""Settlement coordinator.

Tech: Python

Responsibility (System architecture table, build plan):
Consumes verifier decisions and executes the permitted payment: a co-signed provider payment, or release of the reservation

C3B implements ACCOUNTING settlement only (contracts/SETTLEMENT-COORDINATOR.md). No XRPL
transaction is built, signed or submitted, and no SettlementRecord is created: that canonical
object is reserved for XRPL settlement and needs a real transaction hash.

Every state change is one SQLite BEGIN IMMEDIATE transaction on the reservation ledger's database:

- reserve: the signed purchase request is verified here (C3A), the facility's stored accounting
  must match the Facility presented, and the C2 reservation step runs inside the same lock.
- settle: the reservation, its approved policy decision, the facility account, the receipt chain
  head and the replay table are read; signature facts are computed here from the signed receipt
  and attestation (callers cannot supply facts); the real C2 verifier decides. On accept, the
  drawn amount rises by the verified billed amount, the reservation becomes settled, the receipt's
  settlement identity is recorded and the facility's chain head advances, all in one commit.

Rejections are classified. An authenticated terminal delivery failure (gateway signature valid,
Tier 2 provider signature valid, receipt for this reservation's request, and a failure decided by
signed content and Core state) releases the reservation. Any other rejection is recorded but
releases nothing, so an invalid submission cannot free a legitimate pending purchase; expiry still
applies.

The accounting drawn amount is Credence's record of verified spending. It is not confirmed XRPL
loan principal.

Owner: Credence Core.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from credence.core.objects import (
    DecisionRecord,
    DeliveryReceiptBody,
    ExecutionProfile,
    Facility,
    ProcurementClass,
    ProviderAttestationBody,
    PurchaseRequest,
    Quote,
    ServiceClass,
    SignedObject,
)
from credence.core.primitives import (
    DecisionOutcome,
    DecisionStage,
    EvidenceTier,
    FacilityId,
    MicroTrlusd,
    ReasonCode,
    ReservationId,
    ReservationStatus,
    Sha256Digest,
    Timestamp,
)
from credence.core.reservation_ledger import (
    PurchaseContext,
    ReservationLedger,
    ReserveResult,
)
from credence.core.signatures import (
    KeyRegistry,
    policy_signature_facts,
    verifier_signature_facts,
)
from credence.core.verifier import ProviderSignatureFact, VerifierInput, VerifierInputError, verify

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounting_settlements (
    receipt_digest TEXT PRIMARY KEY,
    reservation_id TEXT NOT NULL UNIQUE,
    facility_id TEXT NOT NULL,
    verifier_decision_digest TEXT NOT NULL,
    actual_amount_micro_trlusd INTEGER NOT NULL CHECK (actual_amount_micro_trlusd >= 0),
    settled_at TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state = 'accounting_settled')
);
CREATE TABLE IF NOT EXISTS facility_chain_heads (
    facility_id TEXT PRIMARY KEY,
    receipt_digest TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settlement_disputes (
    dispute_id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('rejected_submission', 'delivery_failure')),
    reservation_id TEXT NOT NULL,
    facility_id TEXT NOT NULL,
    receipt_digest TEXT NOT NULL,
    verifier_decision_digest TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
"""

# Rejections that release the reservation when the submission is authenticated: each is decided
# by gateway- (and provider-) signed content checked against Core's own state. RESPONSE_DIGEST
# depends on the caller's delivered digest, and RECEIPT_SIG_INVALID on missing authentication, so
# neither releases; REPLAYED_RECEIPT cannot concern this reservation's own request.
TERMINAL_REASONS: frozenset[ReasonCode] = frozenset(
    {
        ReasonCode.BINDING_MISMATCH,
        ReasonCode.TOKEN_COUNT_EXCEEDS_QUOTE,
        ReasonCode.BILLED_AMOUNT_MISMATCH,
        ReasonCode.CHAIN_BROKEN,
    }
)

# Fault-injection points, in transaction order (tests only).
FAULT_POINTS = (
    "before_drawn_update",
    "after_drawn_update",
    "after_reservation_settled",
    "after_receipt_recorded",
)


class SettlementState(Enum):
    """Settlement stages. C3B reaches ACCOUNTING_SETTLED only; XRPL_SETTLED is not implemented."""

    DELIVERY_VERIFIED = "delivery_verified"
    ACCOUNTING_SETTLED = "accounting_settled"
    XRPL_SETTLED = "xrpl_settled"


class SettlementOutcome(Enum):
    ACCOUNTING_SETTLED = "accounting_settled"
    REJECTED_SUBMISSION = "rejected_submission"
    DELIVERY_FAILED = "delivery_failed"


class SettlementRefused(RuntimeError):
    """The coordinator refused before any decision; nothing changed. `reason` is a code."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


@dataclass(frozen=True)
class SignedPurchase:
    request: PurchaseRequest
    request_signature: SignedObject | None
    facility: Facility
    quote: Quote
    service_class: ServiceClass
    execution_profile: ExecutionProfile
    procurement_classes: tuple[ProcurementClass, ...]


@dataclass(frozen=True)
class DeliverySubmission:
    """A delivery for one reservation, with its signatures, plus the approved purchase objects."""

    reservation_id: ReservationId
    receipt: DeliveryReceiptBody
    gateway_signature: SignedObject | None
    provider_attestation: ProviderAttestationBody | None
    provider_signature: SignedObject | None
    delivered_response_digest: Sha256Digest
    request: PurchaseRequest
    quote: Quote
    service_class: ServiceClass
    execution_profile: ExecutionProfile


@dataclass(frozen=True)
class SettlementResult:
    outcome: SettlementOutcome
    verifier_record: DecisionRecord
    actual_amount: MicroTrlusd | None
    reservation_status: ReservationStatus


@dataclass(frozen=True)
class FacilityExposure:
    limit: MicroTrlusd
    drawn: MicroTrlusd
    held: MicroTrlusd

    @property
    def headroom(self) -> int:
        return self.limit.value - self.drawn.value - self.held.value


def _store_record(conn: sqlite3.Connection, record: DecisionRecord) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO decision_records (digest, canonical) VALUES (?, ?)",
        (record.digest().value, record.canonical_bytes()),
    )


class SettlementCoordinator:
    def __init__(
        self,
        ledger: ReservationLedger,
        registry: KeyRegistry,
        *,
        fault: Callable[[str], None] | None = None,
    ) -> None:
        self._ledger = ledger
        self._registry = registry
        self._fault = fault or (lambda point: None)
        with ledger.write_transaction() as conn:
            for statement in _SCHEMA.split(";"):
                if statement.strip():
                    conn.execute(statement)

    # --- facility accounting -------------------------------------------------------------

    def register_facility(self, facility: Facility) -> None:
        """Start accounting for a facility at its current limit and drawn amount. Once only."""
        with self._ledger.write_transaction() as conn:
            if ReservationLedger.facility_account(conn, facility.facility_id) is not None:
                raise SettlementRefused("FACILITY_ALREADY_REGISTERED", facility.facility_id.value)
            conn.execute(
                "INSERT INTO facility_accounts (facility_id, limit_micro_trlusd, "
                "drawn_micro_trlusd) VALUES (?, ?, ?)",
                (
                    facility.facility_id.value,
                    facility.limit_micro_trlusd.value,
                    facility.drawn_micro_trlusd.value,
                ),
            )

    def exposure(self, facility_id: FacilityId, now: Timestamp) -> FacilityExposure:
        with self._ledger.write_transaction() as conn:
            account = ReservationLedger.facility_account(conn, facility_id)
            if account is None:
                raise SettlementRefused("UNACCOUNTED_FACILITY", facility_id.value)
            held = ReservationLedger._held_amount(conn, facility_id, now)
        return FacilityExposure(MicroTrlusd(account[0]), MicroTrlusd(account[1]), MicroTrlusd(held))

    def chain_head(self, facility_id: FacilityId) -> Sha256Digest | None:
        """The last accounting-settled receipt on the facility: the next receipt's predecessor."""
        with self._ledger.write_transaction() as conn:
            return self._chain_head(conn, facility_id)

    @staticmethod
    def _chain_head(conn: sqlite3.Connection, facility_id: FacilityId) -> Sha256Digest | None:
        row = conn.execute(
            "SELECT receipt_digest FROM facility_chain_heads WHERE facility_id = ?",
            (facility_id.value,),
        ).fetchone()
        return None if row is None else Sha256Digest(str(row[0]))

    def receipt_settled(self, receipt_digest: Sha256Digest) -> bool:
        with self._ledger.write_transaction() as conn:
            return self._receipt_settled(conn, receipt_digest)

    @staticmethod
    def _receipt_settled(conn: sqlite3.Connection, receipt_digest: Sha256Digest) -> bool:
        row = conn.execute(
            "SELECT 1 FROM accounting_settlements WHERE receipt_digest = ?",
            (receipt_digest.value,),
        ).fetchone()
        return row is not None

    def disputes(self) -> list[tuple[str, str, str, str, str, str, str]]:
        """Operational dispute and rejection rows (not canonical objects), oldest first."""
        with self._ledger.write_transaction() as conn:
            rows = conn.execute(
                "SELECT kind, reservation_id, facility_id, receipt_digest, "
                "verifier_decision_digest, reason_code, recorded_at FROM settlement_disputes "
                "ORDER BY dispute_id"
            ).fetchall()
        return [tuple(str(v) for v in row) for row in rows]  # type: ignore[misc]

    # --- reserve -------------------------------------------------------------------------

    def reserve(self, purchase: SignedPurchase, now: Timestamp) -> ReserveResult:
        """Verify the request signature here, then reserve against the stored accounting."""
        facts = policy_signature_facts(
            purchase.request, purchase.request_signature, purchase.facility, self._registry
        )
        context = PurchaseContext(
            request=purchase.request,
            request_signature_valid=facts.request_signature_valid,
            working_key_current=facts.working_key_current,
            facility=purchase.facility,
            quote=purchase.quote,
            service_class=purchase.service_class,
            execution_profile=purchase.execution_profile,
            procurement_classes=purchase.procurement_classes,
        )
        with self._ledger.write_transaction() as conn:
            if ReservationLedger.facility_account(conn, purchase.facility.facility_id) is None:
                raise SettlementRefused("UNACCOUNTED_FACILITY", purchase.facility.facility_id.value)
            return self._ledger.reserve_in_transaction(conn, context, now)

    # --- settle --------------------------------------------------------------------------

    def settle(self, submission: DeliverySubmission, now: Timestamp) -> SettlementResult:
        """Verify one delivery and settle its reservation in accounting, atomically."""
        expired = False
        with self._ledger.write_transaction() as conn:
            result = self._settle_in(conn, submission, now)
            if result is None:
                expired = True
        if expired:
            raise SettlementRefused("EXPIRED", submission.reservation_id.value)
        assert result is not None
        return result

    def _settle_in(
        self, conn: sqlite3.Connection, sub: DeliverySubmission, now: Timestamp
    ) -> SettlementResult | None:
        rid = sub.reservation_id.value
        row = conn.execute(
            "SELECT facility_id, maximum_amount_micro_trlusd, expires_at, status, "
            "decision_record_digest FROM reservations WHERE reservation_id = ?",
            (rid,),
        ).fetchone()
        if row is None:
            raise SettlementRefused("NOT_FOUND", rid)
        facility_value, reserved, expires_at, status, policy_digest = row
        if status != ReservationStatus.HELD.value:
            raise SettlementRefused("NOT_HELD", rid)
        if not now.value < expires_at:
            conn.execute(
                "UPDATE reservations SET status = 'released', release_reason = 'expired' "
                "WHERE reservation_id = ?",
                (rid,),
            )
            return None  # committed as expired; the caller raises EXPIRED
        facility_id = FacilityId(str(facility_value))
        account = ReservationLedger.facility_account(conn, facility_id)
        if account is None:
            raise SettlementRefused("UNACCOUNTED_FACILITY", facility_id.value)

        # The approved purchase is the one the policy engine accepted for this reservation.
        policy_row = conn.execute(
            "SELECT canonical FROM decision_records WHERE digest = ?", (policy_digest,)
        ).fetchone()
        if policy_row is None:
            raise SettlementRefused("POLICY_DECISION_MISSING", rid)
        policy = DecisionRecord.from_canonical(json.loads(bytes(policy_row[0])))
        if (
            policy.decision_stage is not DecisionStage.POLICY
            or policy.outcome is not DecisionOutcome.ACCEPT
        ):
            raise SettlementRefused("POLICY_DECISION_NOT_ACCEPT", rid)
        approved = policy.input_digests
        if (
            sub.request.digest() != approved[0]
            or sub.quote.digest() != approved[2]
            or sub.service_class.digest() != approved[3]
            or sub.execution_profile.digest() != approved[4]
            or sub.request.facility_id != facility_id
        ):
            raise SettlementRefused("WRONG_PURCHASE", rid)

        facts = verifier_signature_facts(
            sub.receipt,
            sub.gateway_signature,
            sub.provider_attestation,
            sub.provider_signature,
            self._registry,
        )
        receipt_digest = sub.receipt.digest()
        try:
            decision = verify(
                VerifierInput(
                    receipt=sub.receipt,
                    gateway_signature_valid=facts.gateway_signature_valid,
                    provider_signature=facts.provider_signature,
                    provider_attestation=sub.provider_attestation,
                    request=sub.request,
                    quote=sub.quote,
                    service_class=sub.service_class,
                    execution_profile=sub.execution_profile,
                    delivered_response_digest=sub.delivered_response_digest,
                    expected_previous_receipt_digest=self._chain_head(conn, facility_id),
                    receipt_already_settled=self._receipt_settled(conn, receipt_digest),
                    evaluated_at=now,
                )
            )
        except VerifierInputError as exc:
            raise SettlementRefused("INCONSISTENT_SUBMISSION", str(exc)) from None
        record = decision.record
        _store_record(conn, record)

        if not decision.accepted:
            assert record.reason_code is not None
            authenticated = (
                facts.gateway_signature_valid
                and (
                    sub.receipt.evidence_tier is EvidenceTier.WITNESSED
                    or facts.provider_signature is ProviderSignatureFact.VALID
                )
                and sub.receipt.purchase_request_digest == approved[0]
            )
            terminal = authenticated and record.reason_code in TERMINAL_REASONS
            conn.execute(
                "INSERT INTO settlement_disputes (kind, reservation_id, facility_id, "
                "receipt_digest, verifier_decision_digest, reason_code, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "delivery_failure" if terminal else "rejected_submission",
                    rid,
                    facility_id.value,
                    receipt_digest.value,
                    record.digest().value,
                    record.reason_code.value,
                    now.value,
                ),
            )
            if terminal:
                conn.execute(
                    "UPDATE reservations SET status = 'released', release_reason = 'released' "
                    "WHERE reservation_id = ?",
                    (rid,),
                )
            return SettlementResult(
                outcome=(
                    SettlementOutcome.DELIVERY_FAILED
                    if terminal
                    else SettlementOutcome.REJECTED_SUBMISSION
                ),
                verifier_record=record,
                actual_amount=None,
                reservation_status=(
                    ReservationStatus.RELEASED if terminal else ReservationStatus.HELD
                ),
            )

        # DELIVERY_VERIFIED: the verifier accepted, so the billed amount equals the frozen formula.
        actual = sub.receipt.price_micro_trlusd.value
        limit, drawn = account
        if not 0 <= actual <= int(reserved):
            raise SettlementRefused("AMOUNT_EXCEEDS_RESERVATION", rid)
        if drawn + actual > limit:
            raise SettlementRefused("LIMIT_EXCEEDED", rid)

        self._fault("before_drawn_update")
        conn.execute(
            "UPDATE facility_accounts SET drawn_micro_trlusd = drawn_micro_trlusd + ? "
            "WHERE facility_id = ?",
            (actual, facility_id.value),
        )
        self._fault("after_drawn_update")
        updated = conn.execute(
            "UPDATE reservations SET status = 'settled', release_reason = NULL "
            "WHERE reservation_id = ? AND status = 'held'",
            (rid,),
        )
        if updated.rowcount != 1:
            raise SettlementRefused("NOT_HELD", rid)
        self._fault("after_reservation_settled")
        conn.execute(
            "INSERT INTO accounting_settlements (receipt_digest, reservation_id, facility_id, "
            "verifier_decision_digest, actual_amount_micro_trlusd, settled_at, state) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                receipt_digest.value,
                rid,
                facility_id.value,
                record.digest().value,
                actual,
                now.value,
                SettlementState.ACCOUNTING_SETTLED.value,
            ),
        )
        conn.execute(
            "INSERT INTO facility_chain_heads (facility_id, receipt_digest) VALUES (?, ?) "
            "ON CONFLICT (facility_id) DO UPDATE SET receipt_digest = excluded.receipt_digest",
            (facility_id.value, receipt_digest.value),
        )
        self._fault("after_receipt_recorded")
        return SettlementResult(
            outcome=SettlementOutcome.ACCOUNTING_SETTLED,
            verifier_record=record,
            actual_amount=MicroTrlusd(actual),
            reservation_status=ReservationStatus.SETTLED,
        )
