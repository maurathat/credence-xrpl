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
- begin_delivery (I1c): durable intent before chargeable execution. A provider may be called for a
  reservation only after Core has recorded a delivery attempt for it.
- settle: the reservation, its approved policy decision, the facility account, the receipt chain
  head and the replay table are read; signature facts are computed here from the signed receipt
  and attestation (callers cannot supply facts); the real C2 verifier decides. On accept, the
  drawn amount rises by the verified billed amount, the reservation becomes settled, the receipt's
  settlement identity is recorded and the facility's chain head advances, all in one commit.

Rejections are classified. An authenticated terminal delivery failure (gateway signature valid,
Tier 2 provider signature valid, receipt for this reservation's request, and a failure decided by
signed content and Core state) releases the reservation. Any other rejection is recorded but
releases nothing, so an invalid submission cannot free a legitimate pending purchase; expiry still
applies. Since I1c, CHAIN_BROKEN alone does not release, and every authenticated rejection keeps
its exact signed evidence.

No-delivery release (I1c) rests on Core's own record, not on a caller's claim: a hold is released
as undelivered only if no delivery attempt was ever begun for it, and once released no attempt can
begin. An authenticated delivery submitted at or after expiry is refused and never settled, and
its evidence is kept as a late-delivery dispute.

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
    VerifierSignatureFacts,
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
CREATE TABLE IF NOT EXISTS dispute_evidence (
    dispute_id INTEGER PRIMARY KEY REFERENCES settlement_disputes (dispute_id),
    receipt BLOB NOT NULL,
    gateway_signature BLOB NOT NULL,
    provider_attestation BLOB,
    provider_signature BLOB,
    delivered_response_digest TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS delivery_attempts (
    reservation_id TEXT PRIMARY KEY,
    facility_id TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    begun_at TEXT NOT NULL,
    execution_claimed_at TEXT,
    unknown_cause TEXT CHECK (unknown_cause IN (
        'provider_unavailable', 'provider_execution_error', 'provider_malformed_response',
        'interrupted'
    )),
    unknown_recorded_at TEXT
);
CREATE TABLE IF NOT EXISTS no_delivery_releases (
    reservation_id TEXT PRIMARY KEY,
    facility_id TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    policy_decision_digest TEXT NOT NULL,
    reason TEXT NOT NULL CHECK (reason IN (
        'provider_not_connected', 'abandoned_before_execution'
    )),
    released_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS late_delivery_disputes (
    receipt_digest TEXT PRIMARY KEY,
    reservation_id TEXT NOT NULL,
    facility_id TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    receipt BLOB NOT NULL,
    gateway_signature BLOB NOT NULL,
    provider_attestation BLOB,
    provider_signature BLOB,
    delivered_response_digest TEXT NOT NULL
);
"""

# Rejections that release the reservation when the submission is authenticated: each is decided
# by gateway- (and provider-) signed content checked against Core's own state. RESPONSE_DIGEST
# depends on the caller's delivered digest, and RECEIPT_SIG_INVALID on missing authentication, so
# neither releases; REPLAYED_RECEIPT cannot concern this reservation's own request. CHAIN_BROKEN
# (I1c) depends on Credence's own sequencing, not on the provider's delivery, so it does not
# release on its own: the evidence is kept and the hold lapses by expiry.
TERMINAL_REASONS: frozenset[ReasonCode] = frozenset(
    {
        ReasonCode.BINDING_MISMATCH,
        ReasonCode.TOKEN_COUNT_EXCEEDS_QUOTE,
        ReasonCode.BILLED_AMOUNT_MISMATCH,
    }
)

# Ledger and coordinator tables that record history for a facility identity (its facility_id).
# Registration refuses an identity that appears in any of them (I1g, Core finding B-1): accounted
# guarantees hold only for facilities that were accounted from their first reservation. (Not
# listed: dispute_evidence, keyed by settlement_disputes; decision_records, which carries no
# facility identity and creates no exposure, nonce, attempt or settlement state on its own.)
FACILITY_HISTORY_TABLES: tuple[str, ...] = (
    "reservations",
    "used_nonces",
    "accounting_settlements",
    "facility_chain_heads",
    "settlement_disputes",
    "delivery_attempts",
    "no_delivery_releases",
    "late_delivery_disputes",
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


class NoDeliveryReason(Enum):
    """Why a hold with no delivery attempt is released. Informational: the release is permitted by
    Core's record that no attempt was begun, not by the reason."""

    PROVIDER_NOT_CONNECTED = "provider_not_connected"
    ABANDONED_BEFORE_EXECUTION = "abandoned_before_execution"


class UnknownDeliveryCause(Enum):
    """Why a begun delivery attempt has no captured evidence. The outcome is unknown: the provider
    may have executed a chargeable request."""

    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_EXECUTION_ERROR = "provider_execution_error"
    PROVIDER_MALFORMED_RESPONSE = "provider_malformed_response"
    INTERRUPTED = "interrupted"


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
class PurchaseReference:
    """A reservation and the approved purchase objects behind it (checked against the policy
    decision that created the reservation)."""

    reservation_id: ReservationId
    request: PurchaseRequest
    quote: Quote
    service_class: ServiceClass
    execution_profile: ExecutionProfile


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


@dataclass(frozen=True)
class DeliveryAttempt:
    """Core's durable record that a provider may be called for this reservation.

    `execution_claimed_at` is set once, by `claim_execution`, immediately before the gateway calls
    the provider. A recorded attempt is not a provider execution; a claim is the supported
    interface's last committed step before one."""

    reservation_id: ReservationId
    facility_id: FacilityId
    request_digest: Sha256Digest
    begun_at: Timestamp
    unknown_cause: UnknownDeliveryCause | None
    execution_claimed_at: Timestamp | None = None

    def same_attempt(self, other: DeliveryAttempt) -> bool:
        """Same reservation, facility, request and begin time (state fields aside)."""
        return (
            self.reservation_id,
            self.facility_id,
            self.request_digest,
            self.begun_at,
        ) == (other.reservation_id, other.facility_id, other.request_digest, other.begun_at)


@dataclass(frozen=True)
class EvidenceBytes:
    """Exact canonical bytes of a submitted delivery's signed evidence."""

    receipt: bytes
    gateway_signature: bytes
    provider_attestation: bytes | None
    provider_signature: bytes | None
    delivered_response_digest: Sha256Digest


@dataclass(frozen=True)
class LateDeliveryDispute:
    receipt_digest: Sha256Digest
    reservation_id: ReservationId
    facility_id: FacilityId
    expires_at: Timestamp
    submitted_at: Timestamp
    evidence: EvidenceBytes


@dataclass(frozen=True)
class AccountingSettlement:
    """Core's accounting-settlement row for one receipt (read only; I2a CR-4). Not XRPL."""

    receipt_digest: Sha256Digest
    reservation_id: ReservationId
    facility_id: FacilityId
    verifier_decision_digest: Sha256Digest
    actual_amount: MicroTrlusd
    settled_at: Timestamp


@dataclass(frozen=True)
class NoDeliveryRelease:
    reservation_id: ReservationId
    facility_id: FacilityId
    request_digest: Sha256Digest
    policy_decision_digest: Sha256Digest
    reason: NoDeliveryReason
    released_at: Timestamp


def _store_record(conn: sqlite3.Connection, record: DecisionRecord) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO decision_records (digest, canonical) VALUES (?, ?)",
        (record.digest().value, record.canonical_bytes()),
    )


def _evidence(sub: DeliverySubmission) -> EvidenceBytes:
    assert sub.gateway_signature is not None  # only authenticated submissions keep evidence
    return EvidenceBytes(
        receipt=sub.receipt.canonical_bytes(),
        gateway_signature=sub.gateway_signature.canonical_bytes(),
        provider_attestation=(
            None if sub.provider_attestation is None else sub.provider_attestation.canonical_bytes()
        ),
        provider_signature=(
            None if sub.provider_signature is None else sub.provider_signature.canonical_bytes()
        ),
        delivered_response_digest=sub.delivered_response_digest,
    )


def _blob(value: object) -> bytes:
    if not isinstance(value, bytes):
        raise TypeError(f"expected a BLOB, got {type(value).__name__}")
    return value


def _evidence_from_row(row: tuple[object, ...]) -> EvidenceBytes:
    receipt, gateway_signature, attestation, provider_signature, delivered = row
    return EvidenceBytes(
        receipt=_blob(receipt),
        gateway_signature=_blob(gateway_signature),
        provider_attestation=None if attestation is None else _blob(attestation),
        provider_signature=None if provider_signature is None else _blob(provider_signature),
        delivered_response_digest=Sha256Digest(str(delivered)),
    )


def _authenticated(
    receipt: DeliveryReceiptBody, facts: VerifierSignatureFacts, approved_request: Sha256Digest
) -> bool:
    """Gateway signature valid, Tier 2 provider signature valid, receipt for the approved request."""
    return (
        facts.gateway_signature_valid
        and (
            receipt.evidence_tier is EvidenceTier.WITNESSED
            or facts.provider_signature is ProviderSignatureFact.VALID
        )
        and receipt.purchase_request_digest == approved_request
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
        """Start accounting for a history-free facility at its limit and drawn amount. Once only.

        The facility identity (its facility_id) must have no ledger or settlement history:
        unaccounted history (reservations, used nonces, standalone deliveries) is never adopted
        (`FACILITY_HAS_HISTORY`). The check and the insert are one BEGIN IMMEDIATE transaction,
        so no reservation from another connection can land between them; a refusal writes
        nothing."""
        with self._ledger.write_transaction() as conn:
            if ReservationLedger.facility_account(conn, facility.facility_id) is not None:
                raise SettlementRefused("FACILITY_ALREADY_REGISTERED", facility.facility_id.value)
            for table in FACILITY_HISTORY_TABLES:
                if conn.execute(
                    f"SELECT 1 FROM {table} WHERE facility_id = ? LIMIT 1",
                    (facility.facility_id.value,),
                ).fetchone():
                    raise SettlementRefused("FACILITY_HAS_HISTORY", facility.facility_id.value)
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

    def accounting_settlement(self, receipt_digest: Sha256Digest) -> AccountingSettlement | None:
        """The accounting-settlement row for this receipt, or None (read only; I2a CR-4)."""
        with self._ledger.write_transaction() as conn:
            row = conn.execute(
                "SELECT receipt_digest, reservation_id, facility_id, verifier_decision_digest, "
                "actual_amount_micro_trlusd, settled_at FROM accounting_settlements "
                "WHERE receipt_digest = ?",
                (receipt_digest.value,),
            ).fetchone()
        if row is None:
            return None
        return AccountingSettlement(
            receipt_digest=Sha256Digest(str(row[0])),
            reservation_id=ReservationId(str(row[1])),
            facility_id=FacilityId(str(row[2])),
            verifier_decision_digest=Sha256Digest(str(row[3])),
            actual_amount=MicroTrlusd(int(row[4])),
            settled_at=Timestamp(str(row[5])),
        )

    def disputes(self) -> list[tuple[str, str, str, str, str, str, str]]:
        """Operational dispute and rejection rows (not canonical objects), oldest first."""
        with self._ledger.write_transaction() as conn:
            rows = conn.execute(
                "SELECT kind, reservation_id, facility_id, receipt_digest, "
                "verifier_decision_digest, reason_code, recorded_at FROM settlement_disputes "
                "ORDER BY dispute_id"
            ).fetchall()
        return [tuple(str(v) for v in row) for row in rows]  # type: ignore[misc]

    def dispute_evidence(
        self, receipt_digest: Sha256Digest
    ) -> list[tuple[str, str, EvidenceBytes]]:
        """(kind, reason code, evidence) for each authenticated rejection of this receipt."""
        with self._ledger.write_transaction() as conn:
            rows = conn.execute(
                "SELECT d.kind, d.reason_code, e.receipt, e.gateway_signature, "
                "e.provider_attestation, e.provider_signature, e.delivered_response_digest "
                "FROM settlement_disputes d JOIN dispute_evidence e USING (dispute_id) "
                "WHERE d.receipt_digest = ? ORDER BY d.dispute_id",
                (receipt_digest.value,),
            ).fetchall()
        return [(str(r[0]), str(r[1]), _evidence_from_row(tuple(r[2:]))) for r in rows]

    def late_delivery_disputes(self) -> list[LateDeliveryDispute]:
        with self._ledger.write_transaction() as conn:
            rows = conn.execute(
                "SELECT receipt_digest, reservation_id, facility_id, expires_at, submitted_at, "
                "receipt, gateway_signature, provider_attestation, provider_signature, "
                "delivered_response_digest FROM late_delivery_disputes ORDER BY submitted_at, "
                "receipt_digest"
            ).fetchall()
        return [
            LateDeliveryDispute(
                receipt_digest=Sha256Digest(str(r[0])),
                reservation_id=ReservationId(str(r[1])),
                facility_id=FacilityId(str(r[2])),
                expires_at=Timestamp(str(r[3])),
                submitted_at=Timestamp(str(r[4])),
                evidence=_evidence_from_row(tuple(r[5:])),
            )
            for r in rows
        ]

    def no_delivery_releases(self) -> list[NoDeliveryRelease]:
        with self._ledger.write_transaction() as conn:
            rows = conn.execute(
                "SELECT reservation_id, facility_id, request_digest, policy_decision_digest, "
                "reason, released_at FROM no_delivery_releases ORDER BY released_at, reservation_id"
            ).fetchall()
        return [
            NoDeliveryRelease(
                reservation_id=ReservationId(str(r[0])),
                facility_id=FacilityId(str(r[1])),
                request_digest=Sha256Digest(str(r[2])),
                policy_decision_digest=Sha256Digest(str(r[3])),
                reason=NoDeliveryReason(str(r[4])),
                released_at=Timestamp(str(r[5])),
            )
            for r in rows
        ]

    def delivery_attempt(self, reservation_id: ReservationId) -> DeliveryAttempt | None:
        with self._ledger.write_transaction() as conn:
            return self._attempt(conn, reservation_id.value)

    @staticmethod
    def _attempt(conn: sqlite3.Connection, rid: str) -> DeliveryAttempt | None:
        row = conn.execute(
            "SELECT reservation_id, facility_id, request_digest, begun_at, unknown_cause, "
            "execution_claimed_at FROM delivery_attempts WHERE reservation_id = ?",
            (rid,),
        ).fetchone()
        if row is None:
            return None
        return DeliveryAttempt(
            reservation_id=ReservationId(str(row[0])),
            facility_id=FacilityId(str(row[1])),
            request_digest=Sha256Digest(str(row[2])),
            begun_at=Timestamp(str(row[3])),
            unknown_cause=None if row[4] is None else UnknownDeliveryCause(str(row[4])),
            execution_claimed_at=None if row[5] is None else Timestamp(str(row[5])),
        )

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
            # The facts in `context` were computed above from the signed request; this internal
            # path is the only way to reserve on a registered facility.
            return self._ledger._reserve_accounted(conn, context, now)

    # --- shared checks -------------------------------------------------------------------

    @staticmethod
    def _approved_policy(conn: sqlite3.Connection, policy_digest: str, rid: str) -> DecisionRecord:
        """The accepting policy decision that created the reservation."""
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
        return policy

    @staticmethod
    def _is_approved(
        policy: DecisionRecord,
        facility_id: FacilityId,
        request: PurchaseRequest,
        quote: Quote,
        service_class: ServiceClass,
        execution_profile: ExecutionProfile,
    ) -> bool:
        approved = policy.input_digests
        return (
            request.digest() == approved[0]
            and quote.digest() == approved[2]
            and service_class.digest() == approved[3]
            and execution_profile.digest() == approved[4]
            and request.facility_id == facility_id
        )

    def _held_and_approved(
        self, conn: sqlite3.Connection, ref: PurchaseReference, now: Timestamp
    ) -> tuple[FacilityId, DecisionRecord, str] | None:
        """Checks shared by begin_delivery and release_undelivered. None: expired (released)."""
        rid = ref.reservation_id.value
        row = conn.execute(
            "SELECT facility_id, expires_at, status, decision_record_digest FROM reservations "
            "WHERE reservation_id = ?",
            (rid,),
        ).fetchone()
        if row is None:
            raise SettlementRefused("NOT_FOUND", rid)
        facility_value, expires_at, status, policy_digest = row
        if status != ReservationStatus.HELD.value:
            raise SettlementRefused("NOT_HELD", rid)
        facility_id = FacilityId(str(facility_value))
        if ReservationLedger.facility_account(conn, facility_id) is None:
            raise SettlementRefused("UNACCOUNTED_FACILITY", facility_id.value)
        if not now.value < expires_at:
            conn.execute(
                "UPDATE reservations SET status = 'released', release_reason = 'expired' "
                "WHERE reservation_id = ?",
                (rid,),
            )
            return None
        policy = self._approved_policy(conn, str(policy_digest), rid)
        if not self._is_approved(
            policy, facility_id, ref.request, ref.quote, ref.service_class, ref.execution_profile
        ):
            raise SettlementRefused("WRONG_PURCHASE", rid)
        return facility_id, policy, str(policy_digest)

    # --- delivery attempts and no-delivery release ---------------------------------------

    def begin_delivery(self, ref: PurchaseReference, now: Timestamp) -> DeliveryAttempt:
        """Record durable intent to call the provider for a live, approved, held reservation.

        At most one attempt per reservation, and none after a no-delivery release."""
        expired = False
        with self._ledger.write_transaction() as conn:
            checked = self._held_and_approved(conn, ref, now)
            if checked is None:
                expired = True
            else:
                facility_id, _, _ = checked
                rid = ref.reservation_id.value
                if self._attempt(conn, rid) is not None:
                    raise SettlementRefused("DELIVERY_ALREADY_ATTEMPTED", rid)
                conn.execute(
                    "INSERT INTO delivery_attempts (reservation_id, facility_id, request_digest, "
                    "begun_at) VALUES (?, ?, ?, ?)",
                    (rid, facility_id.value, ref.request.digest().value, now.value),
                )
        if expired:
            raise SettlementRefused("EXPIRED", ref.reservation_id.value)
        return DeliveryAttempt(
            reservation_id=ref.reservation_id,
            facility_id=facility_id,
            request_digest=ref.request.digest(),
            begun_at=now,
            unknown_cause=None,
        )

    def claim_execution(self, attempt: DeliveryAttempt, now: Timestamp) -> DeliveryAttempt:
        """Authorize exactly one provider call under Core's persisted attempt (I1e).

        The supplied attempt is only an identifier: it must equal the persisted record (no
        fabricated attempt), the attempt must be unclaimed and not marked unknown, and the
        reservation must be held and live. The claim commits before the caller calls the provider.
        """
        rid = attempt.reservation_id.value
        expired = False
        with self._ledger.write_transaction() as conn:
            persisted = self._attempt(conn, rid)
            if persisted is None:
                raise SettlementRefused("NO_DELIVERY_ATTEMPT", rid)
            if not persisted.same_attempt(attempt):
                raise SettlementRefused("ATTEMPT_MISMATCH", rid)
            if persisted.unknown_cause is not None:
                raise SettlementRefused("DELIVERY_OUTCOME_UNKNOWN", rid)
            if persisted.execution_claimed_at is not None:
                raise SettlementRefused("EXECUTION_ALREADY_CLAIMED", rid)
            row = conn.execute(
                "SELECT status, expires_at FROM reservations WHERE reservation_id = ?", (rid,)
            ).fetchone()
            if row is None or row[0] != ReservationStatus.HELD.value:
                raise SettlementRefused("NOT_HELD", rid)
            if not now.value < row[1]:
                conn.execute(
                    "UPDATE reservations SET status = 'released', release_reason = 'expired' "
                    "WHERE reservation_id = ?",
                    (rid,),
                )
                expired = True
            else:
                conn.execute(
                    "UPDATE delivery_attempts SET execution_claimed_at = ? "
                    "WHERE reservation_id = ? AND execution_claimed_at IS NULL",
                    (now.value, rid),
                )
                claimed = self._attempt(conn, rid)
        if expired:
            raise SettlementRefused("EXPIRED", rid)
        assert claimed is not None
        return claimed

    @staticmethod
    def _attempt_refusal(
        attempt: DeliveryAttempt | None, facility_id: FacilityId, request_digest: Sha256Digest
    ) -> str | None:
        """Settlement and late-delivery handling need Core's persisted attempt for this request."""
        if attempt is None:
            return "NO_DELIVERY_ATTEMPT"
        if attempt.facility_id != facility_id or attempt.request_digest != request_digest:
            return "ATTEMPT_MISMATCH"
        return None

    def release_undelivered(
        self, ref: PurchaseReference, reason: NoDeliveryReason, now: Timestamp
    ) -> None:
        """Release a held reservation for which Core never recorded a delivery attempt. Audited."""
        if not isinstance(reason, NoDeliveryReason):
            raise SettlementRefused("INVALID_RELEASE_REASON", repr(reason))
        expired = False
        with self._ledger.write_transaction() as conn:
            checked = self._held_and_approved(conn, ref, now)
            if checked is None:
                expired = True
            else:
                facility_id, _, policy_digest = checked
                rid = ref.reservation_id.value
                if self._attempt(conn, rid) is not None:
                    raise SettlementRefused("DELIVERY_ATTEMPTED", rid)
                conn.execute(
                    "UPDATE reservations SET status = 'released', release_reason = 'released' "
                    "WHERE reservation_id = ? AND status = 'held'",
                    (rid,),
                )
                conn.execute(
                    "INSERT INTO no_delivery_releases (reservation_id, facility_id, "
                    "request_digest, policy_decision_digest, reason, released_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        rid,
                        facility_id.value,
                        ref.request.digest().value,
                        policy_digest,
                        reason.value,
                        now.value,
                    ),
                )
        if expired:
            raise SettlementRefused("EXPIRED", ref.reservation_id.value)

    def record_unknown_delivery(
        self, reservation_id: ReservationId, cause: UnknownDeliveryCause, now: Timestamp
    ) -> DeliveryAttempt:
        """Record that a begun attempt has no captured evidence. Releases nothing, changes no
        accounting, settles nothing. The first recorded cause is kept."""
        if not isinstance(cause, UnknownDeliveryCause):
            raise SettlementRefused("INVALID_UNKNOWN_CAUSE", repr(cause))
        rid = reservation_id.value
        with self._ledger.write_transaction() as conn:
            attempt = self._attempt(conn, rid)
            if attempt is None:
                raise SettlementRefused("NO_DELIVERY_ATTEMPT", rid)
            status = conn.execute(
                "SELECT status FROM reservations WHERE reservation_id = ?", (rid,)
            ).fetchone()
            if status is not None and status[0] == ReservationStatus.SETTLED.value:
                raise SettlementRefused("NOT_HELD", rid)
            conn.execute(
                "UPDATE delivery_attempts SET unknown_cause = ?, unknown_recorded_at = ? "
                "WHERE reservation_id = ? AND unknown_cause IS NULL",
                (cause.value, now.value, rid),
            )
            recorded = self._attempt(conn, rid)
        assert recorded is not None
        return recorded

    # --- settle --------------------------------------------------------------------------

    def settle(self, submission: DeliverySubmission, now: Timestamp) -> SettlementResult:
        """Verify one delivery and settle its reservation in accounting, atomically."""
        with self._ledger.write_transaction() as conn:
            result = self._settle_in(conn, submission, now)
        if isinstance(result, str):  # committed (an expiry release); then refused
            raise SettlementRefused(result, submission.reservation_id.value)
        return result

    def _record_late(
        self,
        conn: sqlite3.Connection,
        sub: DeliverySubmission,
        facility_value: str,
        policy_digest: str,
        expires_at: str,
        now: Timestamp,
    ) -> str:
        """Keep an authenticated late delivery's evidence. Never settles, never moves drawn.

        Returns the refusal to raise: `EXPIRED`, or for an accounted facility `NO_DELIVERY_ATTEMPT`
        or `ATTEMPT_MISMATCH` when Core never authorized this delivery (nothing is recorded)."""
        facility_id = FacilityId(facility_value)
        if ReservationLedger.facility_account(conn, facility_id) is None:
            return "EXPIRED"
        refusal = self._attempt_refusal(
            self._attempt(conn, sub.reservation_id.value), facility_id, sub.request.digest()
        )
        if refusal is not None:
            return refusal
        try:
            policy = self._approved_policy(conn, policy_digest, sub.reservation_id.value)
        except SettlementRefused:
            return "EXPIRED"
        if not self._is_approved(
            policy, facility_id, sub.request, sub.quote, sub.service_class, sub.execution_profile
        ):
            return "EXPIRED"
        facts = verifier_signature_facts(
            sub.receipt,
            sub.gateway_signature,
            sub.provider_attestation,
            sub.provider_signature,
            self._registry,
        )
        if not _authenticated(sub.receipt, facts, policy.input_digests[0]):
            return "EXPIRED"
        evidence = _evidence(sub)
        conn.execute(
            "INSERT OR IGNORE INTO late_delivery_disputes (receipt_digest, reservation_id, "
            "facility_id, expires_at, submitted_at, receipt, gateway_signature, "
            "provider_attestation, provider_signature, delivered_response_digest) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sub.receipt.digest().value,
                sub.reservation_id.value,
                facility_id.value,
                expires_at,
                now.value,
                evidence.receipt,
                evidence.gateway_signature,
                evidence.provider_attestation,
                evidence.provider_signature,
                evidence.delivered_response_digest.value,
            ),
        )
        return "EXPIRED"

    def _settle_in(
        self, conn: sqlite3.Connection, sub: DeliverySubmission, now: Timestamp
    ) -> SettlementResult | str:
        """A result, or a refusal reason to raise after the transaction commits."""
        rid = sub.reservation_id.value
        row = conn.execute(
            "SELECT facility_id, maximum_amount_micro_trlusd, expires_at, status, "
            "decision_record_digest, release_reason FROM reservations WHERE reservation_id = ?",
            (rid,),
        ).fetchone()
        if row is None:
            raise SettlementRefused("NOT_FOUND", rid)
        facility_value, reserved, expires_at, status, policy_digest, release_reason = row
        late = not now.value < expires_at
        if status == ReservationStatus.RELEASED.value and release_reason == "expired" and late:
            # Already lapsed (for example by another reserve's expiry sweep): still a late delivery.
            return self._record_late(conn, sub, str(facility_value), policy_digest, expires_at, now)
        if status != ReservationStatus.HELD.value:
            raise SettlementRefused("NOT_HELD", rid)
        if late:
            conn.execute(
                "UPDATE reservations SET status = 'released', release_reason = 'expired' "
                "WHERE reservation_id = ?",
                (rid,),
            )
            return self._record_late(conn, sub, str(facility_value), policy_digest, expires_at, now)
        facility_id = FacilityId(str(facility_value))
        account = ReservationLedger.facility_account(conn, facility_id)
        if account is None:
            raise SettlementRefused("UNACCOUNTED_FACILITY", facility_id.value)

        # The approved purchase is the one the policy engine accepted for this reservation.
        policy = self._approved_policy(conn, policy_digest, rid)
        approved = policy.input_digests
        if not self._is_approved(
            policy, facility_id, sub.request, sub.quote, sub.service_class, sub.execution_profile
        ):
            raise SettlementRefused("WRONG_PURCHASE", rid)
        # I1e: only a delivery Core authorized (a persisted attempt for this request) can settle.
        refusal = self._attempt_refusal(self._attempt(conn, rid), facility_id, sub.request.digest())
        if refusal is not None:
            raise SettlementRefused(refusal, rid)

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
            authenticated = _authenticated(sub.receipt, facts, approved[0])
            terminal = authenticated and record.reason_code in TERMINAL_REASONS
            cursor = conn.execute(
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
            if authenticated:
                evidence = _evidence(sub)
                conn.execute(
                    "INSERT INTO dispute_evidence (dispute_id, receipt, gateway_signature, "
                    "provider_attestation, provider_signature, delivered_response_digest) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        cursor.lastrowid,
                        evidence.receipt,
                        evidence.gateway_signature,
                        evidence.provider_attestation,
                        evidence.provider_signature,
                        evidence.delivered_response_digest.value,
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
