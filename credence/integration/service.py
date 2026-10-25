"""Integrated purchase service: Inference delivery connected to Core accounting settlement.

The lifecycle is in contracts/INTEGRATION-FLOW.md. In short, for one purchase:

     1. this process owns the database (ProcessOwnership);
     2. the facility's in-process purchase lock is held until the operation is settled or
        terminally reconciled (one in-flight purchase per facility);
     3. the facility must be registered;
     4. its authoritative exposure is read;
     5. one signed purchase per offer is built against the stored drawn amount;
     6. Core qualifies every offer and the cheapest qualifying one is chosen;
     7. the intent is journaled, then Core reserves (SettlementCoordinator.reserve);
     8. the reservation is journaled;
     9. Core's persisted chain head is read;
    10. the journal records EXECUTING, Core records the delivery attempt, then the provider runs
        with no SQLite transaction open;
    11. the gateway builds and signs the evidence (Gateway.deliver);
    12. the evidence and exact response bytes are journaled;
    13. Core settles (SettlementCoordinator.settle), which alone advances the chain;
    14. only after accounting settlement is the response available, and only to a caller holding
        the agent's private key (an agent-signed, single-use ResponseAuthorization);
    15. it is retained until the agent acknowledges it, with a separate signed authorization.

The service exposes no purchase-capable gateway, no ledger and no writable coordinator: reads go
through `accounting` (AccountingView), and every provider call it makes is preceded by Core's
committed `begin_delivery` and claimed through Core's delivery authority (I1e).

Accounting settlement is not XRPL settlement: no XRPL transaction exists here.

Recovery reconciles each in-flight journal entry with Core's reservation, attempt and settlement
records before anything else, and never re-executes a provider request, charges twice, releases a
hold whose delivery may have happened, or fabricates a receipt.

Remote agent signing (I2a, CR-1, provisional): `prepare` selects one offer under Core's policy and
durably stores the exact `PurchaseRequest` for one authenticated agent (`preparation.py`); the
agent verifies and signs that request's existing C3A statement outside the server; `submit_prepared`
requires that signature, refuses any change, and only then runs the request through the same
lifecycle as `purchase` (steps 2-15). `purchase` and `prepare` build requests with one builder.

Agent-scoped reads (I2a, CR-3/CR-4, provisional): operations by agent, the operation of a receipt,
and the lifecycle events Core and the journal recorded with their own timestamps. Nothing is
inferred: a transition whose time was not persisted is not reported.

Owner: Integration (San Antonio).
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from types import TracebackType

from nacl.signing import SigningKey

from credence.core.billing import max_total_cost_value
from credence.core.objects import (
    DecisionRecord,
    ExecutionProfile,
    Facility,
    ProcurementClass,
    PurchaseRequest,
    Quote,
    Reservation,
    ServiceClass,
    SignedObject,
)
from credence.core.primitives import (
    FacilityId,
    KeyId,
    MicroTrlusd,
    Nonce,
    Purpose,
    ReservationId,
    ReservationStatus,
    Sha256Digest,
    Timestamp,
)
from credence.core.reservation_ledger import (
    ReservationLedger,
    ReservationStateError,
    ReserveResult,
    reservation_id_for,
)
from credence.core.settlement_coordinator import (
    AccountingSettlement,
    DeliveryAttempt,
    DeliverySubmission,
    EvidenceBytes,
    FacilityExposure,
    LateDeliveryDispute,
    NoDeliveryReason,
    NoDeliveryRelease,
    PurchaseReference,
    SettlementCoordinator,
    SettlementOutcome,
    SettlementRefused,
    SignedPurchase,
    UnknownDeliveryCause,
)
from credence.core.signatures import (
    KeyRegistry,
    KeyRole,
    KeyStatus,
    RegisteredKey,
    key_id_for,
    sign,
    verify_signed,
)
from credence.inference.gateway import (
    DeliveryCaptured,
    DeliveryNotAuthorized,
    Gateway,
    ProviderFailed,
    ProviderFailureKind,
    Purchase,
)
from credence.inference.market import Selection, qualify_and_select
from credence.inference.payload import PayloadError, payload_digest
from credence.inference.provider import InferenceProvider
from credence.integration.authorization import (
    NotAuthorized,
    ResponseAction,
    ResponseAuthorization,
    verify_response_authorization,
)
from credence.integration.journal import (
    IN_FLIGHT,
    RESPONSE_AVAILABLE,
    Journal,
    KeyRevoked,
    NonceReused,
    Operation,
    OperationState,
    StateRefused,
)
from credence.integration.ownership import DatabaseIdentityError, ProcessOwnership
from credence.integration.preparation import (
    PREPARATION_TTL_SECONDS,
    Preparation,
    PreparationStore,
    market_digest,
    new_preparation_id,
)
from credence.integration.revocation import DurableKeyRegistry, RevocationStore

S = OperationState

# Interruption points, in lifecycle order (tests only). A hook may raise to simulate a crash.
CRASH_POINTS = (
    "after_intent",
    "after_reserve",
    "after_executing_recorded",
    "after_attempt_begun",
    "after_provider_returned",
    "after_evidence_stored",
    "after_settle_committed",
)

_UNKNOWN_CAUSE = {
    ProviderFailureKind.PROVIDER_UNAVAILABLE: UnknownDeliveryCause.PROVIDER_UNAVAILABLE,
    ProviderFailureKind.PROVIDER_EXECUTION_ERROR: UnknownDeliveryCause.PROVIDER_EXECUTION_ERROR,
    ProviderFailureKind.PROVIDER_MALFORMED_RESPONSE: (
        UnknownDeliveryCause.PROVIDER_MALFORMED_RESPONSE
    ),
}


class Refusal(Enum):
    """Refused before any journal entry, reservation or provider call."""

    UNREGISTERED_FACILITY = "UNREGISTERED_FACILITY"
    FACILITY_LIMIT_MISMATCH = "FACILITY_LIMIT_MISMATCH"
    FACILITY_BUSY = "FACILITY_BUSY"
    PROMPT_NOT_UTF8 = "PROMPT_NOT_UTF8"
    NO_OFFERS = "NO_OFFERS"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    # Remote agent signing (I2a, CR-1). Nothing is journaled, reserved or called on any of these.
    AGENT_NOT_AUTHORIZED = "AGENT_NOT_AUTHORIZED"
    AGENT_REVOKED = "AGENT_REVOKED"
    NO_QUALIFYING_OFFER = "NO_QUALIFYING_OFFER"
    ALREADY_SUBMITTED = "ALREADY_SUBMITTED"
    UNKNOWN_PREPARATION = "UNKNOWN_PREPARATION"
    PREPARATION_EXPIRED = "PREPARATION_EXPIRED"
    PREPARATION_STALE = "PREPARATION_STALE"
    PREPARATION_CONSUMED = "PREPARATION_CONSUMED"
    PROMPT_MISMATCH = "PROMPT_MISMATCH"
    REQUEST_MISMATCH = "REQUEST_MISMATCH"
    WRONG_AGENT = "WRONG_AGENT"
    BAD_SIGNATURE = "BAD_SIGNATURE"


class ServiceClosed(RuntimeError):
    pass


class JournalInconsistency(RuntimeError):
    """The journal contradicts Core's authoritative records. Fail closed: nothing is changed."""


class ResponseUnavailable(RuntimeError):
    def __init__(self, operation_id: str, state: OperationState) -> None:
        super().__init__(f"{operation_id}: no response available in state {state.value}")
        self.state = state


class UnknownOperation(KeyError):
    pass


class UnknownPreparation(KeyError):
    pass


__all__ = ["NotAuthorized"]  # re-exported: retrieve and acknowledge raise it


class AccountingView:
    """Read-only access to Core's accounting records for one integration database.

    No reserve, release, settle or provider call is reachable through it."""

    def __init__(self, ledger: ReservationLedger, coordinator: SettlementCoordinator) -> None:
        self.__ledger = ledger
        self.__coordinator = coordinator

    def exposure(self, facility_id: FacilityId, now: Timestamp) -> FacilityExposure:
        return self.__coordinator.exposure(facility_id, now)

    def chain_head(self, facility_id: FacilityId) -> Sha256Digest | None:
        return self.__coordinator.chain_head(facility_id)

    def receipt_settled(self, receipt_digest: Sha256Digest) -> bool:
        return self.__coordinator.receipt_settled(receipt_digest)

    def disputes(self) -> list[tuple[str, str, str, str, str, str, str]]:
        return self.__coordinator.disputes()

    def dispute_evidence(
        self, receipt_digest: Sha256Digest
    ) -> list[tuple[str, str, EvidenceBytes]]:
        return self.__coordinator.dispute_evidence(receipt_digest)

    def late_delivery_disputes(self) -> list[LateDeliveryDispute]:
        return self.__coordinator.late_delivery_disputes()

    def no_delivery_releases(self) -> list[NoDeliveryRelease]:
        return self.__coordinator.no_delivery_releases()

    def delivery_attempt(self, reservation_id: ReservationId) -> DeliveryAttempt | None:
        return self.__coordinator.delivery_attempt(reservation_id)

    def reservation(self, reservation_id: ReservationId) -> Reservation:
        return self.__ledger.get(reservation_id)

    def release_reason(self, reservation_id: ReservationId) -> str | None:
        return self.__ledger.release_reason(reservation_id)

    # Read only (I2a, CR-4).
    def accounting_settlement(self, receipt_digest: Sha256Digest) -> AccountingSettlement | None:
        return self.__coordinator.accounting_settlement(receipt_digest)

    def policy_decision_digest(self, reservation_id: ReservationId) -> str | None:
        return self.__ledger.decision_record_digest(reservation_id)

    def decision_record(self, digest: str) -> DecisionRecord | None:
        data = self.__ledger.decision_record_bytes(digest)
        return None if data is None else DecisionRecord.from_canonical(json.loads(data))


@dataclass(frozen=True)
class Offer:
    quote: Quote
    service_class: ServiceClass
    execution_profile: ExecutionProfile


@dataclass(frozen=True)
class Job:
    """One inference job for one facility. `facility` supplies the terms; Core's stored accounting
    supplies drawn. `idempotency_key` names the job: resubmitting it never buys twice."""

    idempotency_key: str
    facility: Facility
    procurement_classes: tuple[ProcurementClass, ...]
    offers: tuple[Offer, ...]
    prompt: bytes
    request_expires_at: Timestamp


AgentSigner = Callable[[PurchaseRequest], SignedObject]


@dataclass(frozen=True)
class Market:
    """The purchase context for one facility, supplied by the embedding process and never by the
    agent: the facility terms as the operator configured them, the facility's procurement classes
    and the current offers, in order (I2a, CR-1)."""

    facility: Facility
    procurement_classes: tuple[ProcurementClass, ...]
    offers: tuple[Offer, ...]

    def digest(self) -> Sha256Digest:
        return market_digest(
            self.facility,
            self.procurement_classes,
            [(o.quote, o.service_class, o.execution_profile) for o in self.offers],
        )


@dataclass(frozen=True)
class LifecycleEvent:
    """One recorded event of an operation, at the time its own record persisted (I2a, CR-4)."""

    event: str
    at: Timestamp
    source: str
    detail: str | None


@dataclass(frozen=True)
class PurchaseRefused:
    refusal: Refusal
    detail: str


@dataclass(frozen=True)
class PurchaseReport:
    """The journaled state of an operation. Never carries the response."""

    operation_id: str
    state: OperationState
    detail: str | None
    reservation_id: str | None
    provider_id: str | None
    receipt_digest: str | None
    reason_code: str | None

    @classmethod
    def of(cls, op: Operation) -> PurchaseReport:
        return cls(
            operation_id=op.operation_id,
            state=op.state,
            detail=op.detail,
            reservation_id=op.reservation_id,
            provider_id=op.provider_id,
            receipt_digest=op.receipt_digest,
            reason_code=op.reason_code,
        )


@dataclass(frozen=True)
class Reconciliation:
    operation_id: str
    before: OperationState
    after: OperationState


def operation_id_for(idempotency_key: str) -> str:
    return "op-" + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:32]


def nonce_for(idempotency_key: str, offer_index: int) -> Nonce:
    material = f"credence-integration-nonce\x00{idempotency_key}\x00{offer_index}".encode()
    return Nonce(hashlib.sha256(material).hexdigest()[:32])


def purchase_requests(
    idempotency_key: str,
    facility_id: FacilityId,
    offers: Iterable[Offer],
    prompt_digest: Sha256Digest,
    expires_at: Timestamp,
) -> tuple[PurchaseRequest, ...]:
    """The one request builder, shared by `purchase` and `prepare`: one request per offer, built
    from job inputs only (none depends on exposure)."""
    return tuple(
        PurchaseRequest(
            facility_id=facility_id,
            provider_id=offer.quote.provider_id,
            quote_id=offer.quote.quote_id,
            purpose=Purpose.INFERENCE,
            max_total_cost_micro_trlusd=MicroTrlusd(max_total_cost_value(offer.quote)),
            prompt_digest=prompt_digest,
            nonce=nonce_for(idempotency_key, index),
            expires_at=expires_at,
        )
        for index, offer in enumerate(offers)
    )


class IntegrationService:
    """The single process that serves one integration database."""

    def __init__(
        self,
        database: Path,
        *,
        registry: KeyRegistry,
        gateway_key: SigningKey,
        providers: Iterable[InferenceProvider],
        clock: Callable[[], Timestamp],
        recover: bool = True,
        crash_hook: Callable[[str], None] | None = None,
        coordinator_fault: Callable[[str], None] | None = None,
        operator: str = "integration-operator",
    ) -> None:
        """`registry` is the operator's key registry. While the service is open, its `revoke`
        persists the revocation in this database before it takes effect, and persisted
        revocations override any registry supplied later (I1g, F1). `operator` names the trusted
        administrative actor recorded with each revocation."""
        # I1k: ownership is bound to the database's canonical identity; from here on only the
        # canonical path is used, and every checkpoint verifies it still names the owned file.
        self._ownership = ProcessOwnership.acquire(Path(database), allow_create=True)
        self._database = self._ownership.identity.path
        self._identity_failure: DatabaseIdentityError | None = None
        # Serializes revocation commits with purchase admission (I1g, F1).
        self._revocation_gate = threading.RLock()
        self._durable_registry: DurableKeyRegistry | None = None
        try:
            self._ownership.restrict_permissions()
            revocations = RevocationStore(self._database, verify=self._ownership.verify)
            revocations.all()  # fail closed on unreadable or malformed revocation state
            self._durable_registry = DurableKeyRegistry(
                registry, revocations, actor=operator, clock=clock, gate=self._revocation_gate
            )
            self._durable_registry.bind()
            self._ledger = ReservationLedger(self._database)
            self._coordinator = SettlementCoordinator(
                self._ledger, self._durable_registry, fault=coordinator_fault
            )
            self._journal = Journal(self._database, verify=self._ownership.verify)
            self._preparations = PreparationStore(self._database, verify=self._ownership.verify)
            self._gateway = Gateway(
                ledger=self._ledger,
                registry=self._durable_registry,
                gateway_key=gateway_key,
                providers=providers,
                clock=clock,
                delivery_authority=self._coordinator,
            )
            self._accounting = AccountingView(self._ledger, self._coordinator)
        except BaseException:
            self._release()
            raise
        self._clock = clock
        self._registry: KeyRegistry = self._durable_registry
        self._revocations = revocations
        self._crash = crash_hook or (lambda point: None)
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        if recover:
            try:
                self.recover()
            except BaseException:
                self._release()  # fail closed, and do not leave the database owned
                raise

    # --- lifecycle -----------------------------------------------------------------------

    def _release(self) -> None:
        if self._durable_registry is not None:
            self._durable_registry.unbind()
        self._ownership.release()

    def close(self) -> None:
        self._release()

    def __enter__(self) -> IntegrationService:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def _require_open(self) -> None:
        if not self._ownership.held:
            raise ServiceClosed(str(self._database))
        self._check_identity()

    def _check_identity(self) -> None:
        """I1k checkpoint: the canonical path still names the owned file. A mismatch poisons the
        service: every later operation is refused until it is closed."""
        if self._identity_failure is not None:
            raise self._identity_failure
        try:
            self._ownership.verify()
        except DatabaseIdentityError as exc:
            self._identity_failure = exc
            raise

    @contextmanager
    def _facility_lock(self, facility_id: str) -> Iterator[None]:
        with self._locks_guard:
            lock = self._locks.setdefault(facility_id, threading.Lock())
        with lock:
            yield

    # --- read-only views -----------------------------------------------------------------

    @property
    def database(self) -> Path:
        """The canonical path of the owned database (I1k)."""
        return self._database

    @property
    def accounting(self) -> AccountingView:
        """Read-only Core accounting. The service exposes no gateway, ledger or coordinator."""
        return self._accounting

    def operation(self, operation_id: str) -> PurchaseReport:
        op = self._journal.get(operation_id)
        if op is None:
            raise UnknownOperation(operation_id)
        return PurchaseReport.of(op)

    def journal_entry(self, operation_id: str) -> Operation:
        op = self._journal.get(operation_id)
        if op is None:
            raise UnknownOperation(operation_id)
        return op

    def pending_operations(self) -> list[Operation]:
        """Operations not yet settled or terminally reconciled (what recover() would act on)."""
        return self._journal.in_flight()

    def register_facility(self, facility: Facility) -> None:
        self._require_open()
        self._coordinator.register_facility(facility)

    # --- agent-scoped reads (I2a, CR-3 and CR-4) --------------------------------------------

    def public_key(self, key_id: KeyId) -> RegisteredKey | None:
        """The registry's public record of a key (key ID, role, subject, durable status)."""
        return self._registry.lookup(key_id)

    def operations_for_agent(
        self, agent_key_id: KeyId, *, limit: int, after: str | None = None
    ) -> list[Operation]:
        """Operations whose purchase this agent key signed (with its current registry subject),
        oldest first. `after` is the last operation ID of the previous page; it must belong to
        the same agent (otherwise UnknownOperation)."""
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        key = self._registry.lookup(agent_key_id)
        if key is None or key.role is not KeyRole.AGENT:
            return []
        cursor = None
        if after is not None:
            op = self._journal.get(after)
            if op is None or (op.agent_key_id, op.agent_subject) != (
                agent_key_id.value,
                key.subject,
            ):
                raise UnknownOperation(after)
            cursor = (str(op.row["created_at"]), op.operation_id)
        return self._journal.for_agent(agent_key_id.value, key.subject, limit=limit, after=cursor)

    def operation_for_receipt(self, receipt_digest: Sha256Digest) -> Operation:
        op = self._journal.by_receipt(receipt_digest.value)
        if op is None:
            raise UnknownOperation(receipt_digest.value)
        return op

    def lifecycle_events(self, operation_id: str) -> list[LifecycleEvent]:
        """Every event of the operation whose time a record persisted, oldest first.

        Sources: the journal (intent, retrieval, acknowledgment, latest transition), Core's
        reservation, policy decision, delivery attempt, no-delivery release, accounting
        settlement, dispute and late-delivery records, the gateway-signed receipt's
        provider-reported times, and the agent key's persisted revocation. Transitions whose
        time is not persisted (for example `delivered`) are not reported."""
        op = self.journal_entry(operation_id)
        ordered: list[tuple[str, int, LifecycleEvent]] = []

        def add(event: str, at: str | None, source: str, detail: str | None = None) -> None:
            if at is not None:
                ordered.append(
                    (at, len(ordered), LifecycleEvent(event, Timestamp(at), source, detail))
                )

        add("intent_journaled", str(op.row["created_at"]), "integration_operations.created_at")
        if op.reservation_id is not None:
            rid = ReservationId(op.reservation_id)
            policy_digest = self._accounting.policy_decision_digest(rid)
            policy = (
                None if policy_digest is None else self._accounting.decision_record(policy_digest)
            )
            if policy is not None:
                add(
                    "policy_decision",
                    policy.decided_at.value,
                    "decision_records (policy)",
                    policy.outcome.value
                    + ("" if policy.reason_code is None else f" {policy.reason_code.value}"),
                )
            try:
                reservation = self._ledger.get(rid)
            except ReservationStateError:
                reservation = None
            if reservation is not None:
                add(
                    "reserved",
                    reservation.created_at.value,
                    "reservations.created_at",
                    f"hold {reservation.maximum_amount_micro_trlusd.value} micro-tRLUSD until "
                    f"{reservation.expires_at.value}",
                )
            attempt = self._coordinator.delivery_attempt(rid)
            if attempt is not None:
                add(
                    "delivery_attempt_recorded",
                    attempt.begun_at.value,
                    "delivery_attempts.begun_at",
                )
                if attempt.execution_claimed_at is not None:
                    add(
                        "execution_claimed",
                        attempt.execution_claimed_at.value,
                        "delivery_attempts.execution_claimed_at",
                    )
            for release in self._coordinator.no_delivery_releases():
                if release.reservation_id == rid:
                    add(
                        "released_undelivered",
                        release.released_at.value,
                        "no_delivery_releases.released_at",
                        release.reason.value,
                    )
        receipt = op.receipt()
        if receipt is not None:
            add(
                "provider_started",
                receipt.started_at.value,
                "receipt.started_at (provider-reported, gateway-signed)",
            )
            add(
                "provider_completed",
                receipt.completed_at.value,
                "receipt.completed_at (provider-reported, gateway-signed)",
            )
            digest = receipt.digest()
            settled = self._coordinator.accounting_settlement(digest)
            if settled is not None:
                add(
                    "accounting_settled",
                    settled.settled_at.value,
                    "accounting_settlements.settled_at",
                    f"{settled.actual_amount.value} micro-tRLUSD drawn (accounting, not XRPL)",
                )
            for kind, _, _, receipt_digest, _, reason, recorded_at in self._coordinator.disputes():
                if receipt_digest == digest.value:
                    add(
                        "settlement_dispute",
                        recorded_at,
                        "settlement_disputes.recorded_at",
                        f"{kind} {reason}",
                    )
            for late in self._coordinator.late_delivery_disputes():
                if late.receipt_digest == digest:
                    add(
                        "late_delivery_dispute",
                        late.submitted_at.value,
                        "late_delivery_disputes.submitted_at",
                        f"reservation expired {late.expires_at.value}",
                    )
        add("retrieved", op.retrieved_at, "integration_operations.retrieved_at")
        add("acknowledged", op.acknowledged_at, "integration_operations.acknowledged_at")
        if op.agent_key_id is not None:
            revocation = self._revocations.get(KeyId(op.agent_key_id))
            if revocation is not None:
                add(
                    "agent_key_revoked",
                    revocation.revoked_at.value,
                    "integration_key_revocations.revoked_at",
                )
        add(
            "latest_transition",
            str(op.row["updated_at"]),
            "integration_operations.updated_at",
            f"journal state {op.state.value}",
        )
        return [event for _, _, event in sorted(ordered, key=lambda e: (e[0], e[1]))]

    # --- purchase ------------------------------------------------------------------------

    def purchase(self, job: Job, signer: AgentSigner) -> PurchaseReport | PurchaseRefused:
        """Run one job through the integrated lifecycle. The response is never returned here."""
        self._require_open()
        if not job.offers:
            return PurchaseRefused(Refusal.NO_OFFERS, "a job needs at least one offer")
        try:
            prompt_digest = payload_digest(job.prompt)
        except PayloadError as exc:
            return PurchaseRefused(Refusal.PROMPT_NOT_UTF8, str(exc))
        fid = job.facility.facility_id.value
        with self._facility_lock(fid):
            existing = self._journal.by_key(job.idempotency_key)
            if existing is not None:
                if (existing.facility_id, existing.prompt_digest) != (fid, prompt_digest.value):
                    return PurchaseRefused(
                        Refusal.IDEMPOTENCY_CONFLICT,
                        f"{job.idempotency_key} names a different job",
                    )
                return PurchaseReport.of(existing)  # replay: never executes again
            if self._journal.in_flight(fid):
                return PurchaseRefused(
                    Refusal.FACILITY_BUSY, f"{fid} has an unreconciled operation; run recover()"
                )
            return self._purchase_locked(job, signer, prompt_digest)

    # --- remote agent signing: prepare, then submit (I2a, CR-1) ----------------------------

    def _agent_refusal(self, agent_key_id: KeyId, facility: Facility) -> PurchaseRefused | None:
        """The key must be a registered agent key for the facility's borrower, and current."""
        key = self._registry.lookup(agent_key_id)
        if (
            key is None
            or key.role is not KeyRole.AGENT
            or key.subject != facility.borrower_account.value
        ):
            return PurchaseRefused(
                Refusal.AGENT_NOT_AUTHORIZED,
                f"{agent_key_id.value} is not a registered agent key for this facility",
            )
        assert self._durable_registry is not None
        if self._durable_registry.persisted_revocation(agent_key_id) or (
            key.status is not KeyStatus.CURRENT
        ):
            return PurchaseRefused(Refusal.AGENT_REVOKED, agent_key_id.value)
        return None

    def _provisional_selection(
        self, facility: Facility, market: Market, requests: tuple[PurchaseRequest, ...]
    ) -> Selection:
        """Core's qualification and Inference's routing rule, unchanged, over the unsigned
        requests, against this database's real holds and used nonces.

        The agent has not signed yet, so each request is signed in-process by an ephemeral
        sandbox key registered, for this evaluation only, as an agent key for the facility's
        borrower. The key is never stored or returned, and this selection authorizes nothing:
        the agent's own signature, its key status and Core's reservation decide at submission.
        The prompt is not used by qualification and is not available here."""
        sandbox_key = SigningKey.generate()
        registry = KeyRegistry(
            [
                RegisteredKey(
                    key_id_for(sandbox_key.verify_key),
                    KeyRole.AGENT,
                    facility.borrower_account.value,
                    KeyStatus.CURRENT,
                )
            ]
        )
        candidates = [
            Purchase(
                request=request,
                request_signature=sign(request, sandbox_key),
                prompt=b"",
                facility=facility,
                quote=offer.quote,
                service_class=offer.service_class,
                execution_profile=offer.execution_profile,
                procurement_classes=market.procurement_classes,
            )
            for request, offer in zip(requests, market.offers, strict=True)
        ]
        del sandbox_key
        return qualify_and_select(
            candidates, registry=registry, ledger=self._ledger, now=self._clock()
        )

    def prepare(
        self,
        market: Market,
        *,
        agent_key_id: KeyId,
        prompt_digest: Sha256Digest,
        idempotency_key: str | None = None,
    ) -> Preparation | PurchaseRefused:
        """Select one offer under Core's policy and durably store the exact request for the agent
        to sign. Reserves nothing, journals nothing and calls no provider.

        The request is bound to `agent_key_id` (the authenticated agent), the facility, the
        chosen offer and an opaque single-use preparation identity, and expires after
        PREPARATION_TTL_SECONDS. Its fields are exactly those `purchase` would build for the same
        idempotency key and offer."""
        self._require_open()
        if not market.offers:
            return PurchaseRefused(Refusal.NO_OFFERS, "a job needs at least one offer")
        refused = self._agent_refusal(agent_key_id, market.facility)
        if refused is not None:
            return refused
        preparation_id = new_preparation_id()
        key = idempotency_key if idempotency_key is not None else f"prepared:{preparation_id}"
        fid = market.facility.facility_id.value
        with self._facility_lock(fid):
            existing = self._journal.by_key(key)
            if existing is not None:
                return PurchaseRefused(Refusal.ALREADY_SUBMITTED, existing.operation_id)
            facility = self._accounted_facility(market.facility)
            if isinstance(facility, PurchaseRefused):
                return facility
            now = self._clock()
            expires_at = now.plus_seconds(PREPARATION_TTL_SECONDS)
            requests = purchase_requests(
                key, facility.facility_id, market.offers, prompt_digest, expires_at
            )
            selection = self._provisional_selection(facility, market, requests)
            if selection.chosen is None:
                reasons = sorted(
                    {q.reason_code.value for q in selection.qualifications if q.reason_code}
                )
                return PurchaseRefused(Refusal.NO_QUALIFYING_OFFER, ",".join(reasons))
            index = requests.index(selection.chosen.purchase.request)
            offer = market.offers[index]
            key_record = self._registry.lookup(agent_key_id)
            assert key_record is not None
            preparation = Preparation(
                preparation_id=preparation_id,
                idempotency_key=key,
                operation_id=operation_id_for(key),
                agent_key_id=agent_key_id,
                agent_subject=key_record.subject,
                facility_id=facility.facility_id,
                market_digest=market.digest(),
                offer_index=index,
                request=requests[index],
                quote=offer.quote,
                service_class=offer.service_class,
                execution_profile=offer.execution_profile,
                prompt_digest=prompt_digest,
                created_at=now,
                expires_at=expires_at,
            )
            self._preparations.insert(preparation)
            return preparation

    def preparation(self, preparation_id: str) -> Preparation:
        prepared = self._preparations.get(preparation_id)
        if prepared is None:
            raise UnknownPreparation(preparation_id)
        return prepared

    def submit_prepared(
        self,
        market: Market,
        *,
        preparation_id: str,
        signature: SignedObject,
        prompt: bytes,
        agent_key_id: KeyId,
    ) -> PurchaseReport | PurchaseRefused:
        """Purchase exactly the prepared request, under the agent's own signature.

        `agent_key_id` is the authenticated caller; `market` is the embedding process's current
        view of the facility and its offers. Refused, with nothing journaled, reserved or
        called, if the caller, prompt, request, signing key or market differs from the
        preparation, if the signature does not verify under a current agent key for the
        facility's borrower, if the preparation expired, or if Core's policy or the routing rule
        would now choose differently. Another offer is never substituted. An identical
        resubmission returns the journaled operation and never buys twice, across restarts."""
        self._require_open()
        try:
            prompt_digest = payload_digest(prompt)
        except PayloadError as exc:
            return PurchaseRefused(Refusal.PROMPT_NOT_UTF8, str(exc))
        prepared = self._preparations.get(preparation_id)
        if prepared is None:
            return PurchaseRefused(Refusal.UNKNOWN_PREPARATION, preparation_id)
        if prepared.agent_key_id != agent_key_id:
            return PurchaseRefused(Refusal.WRONG_AGENT, "not the preparation's agent")
        if market.facility.facility_id != prepared.facility_id:
            return PurchaseRefused(Refusal.PREPARATION_STALE, "a different facility")
        with self._facility_lock(prepared.facility_id.value):
            existing = self._journal.by_key(prepared.idempotency_key)
            if existing is not None:
                if (
                    existing.row["request_digest"] == prepared.request_digest.value
                    and existing.prompt_digest == prompt_digest.value
                    and existing.row["request_signature"] == signature.canonical_bytes()
                ):
                    return PurchaseReport.of(existing)  # identical retry: never executes again
                return PurchaseRefused(Refusal.PREPARATION_CONSUMED, existing.operation_id)
            refused = self._check_submission(market, prepared, signature, prompt_digest)
            if refused is not None:
                return refused
            if self._journal.in_flight(prepared.facility_id.value):
                return PurchaseRefused(
                    Refusal.FACILITY_BUSY,
                    f"{prepared.facility_id.value} has an unreconciled operation; run recover()",
                )
            facility = self._accounted_facility(market.facility)
            if isinstance(facility, PurchaseRefused):
                return facility
            requests = purchase_requests(
                prepared.idempotency_key,
                prepared.facility_id,
                market.offers,
                prepared.prompt_digest,
                prepared.expires_at,
            )
            if requests[prepared.offer_index] != prepared.request:
                return PurchaseRefused(Refusal.PREPARATION_STALE, "the request no longer rebuilds")
            selection = self._provisional_selection(facility, market, requests)
            if selection.chosen is None or selection.chosen.purchase.request != prepared.request:
                return PurchaseRefused(
                    Refusal.PREPARATION_STALE,
                    "Core's policy or the offers now select differently; prepare again",
                )
            owner = self._registry.lookup(agent_key_id)
            assert owner is not None
            candidate = Purchase(
                request=prepared.request,
                request_signature=signature,
                prompt=prompt,
                facility=facility,
                quote=prepared.quote,
                service_class=prepared.service_class,
                execution_profile=prepared.execution_profile,
                procurement_classes=market.procurement_classes,
            )
            return self._select_and_run(
                prepared.idempotency_key, prompt_digest, [candidate], owner=owner
            )

    def _check_submission(
        self,
        market: Market,
        prepared: Preparation,
        signature: SignedObject,
        prompt_digest: Sha256Digest,
    ) -> PurchaseRefused | None:
        if prompt_digest != prepared.prompt_digest:
            return PurchaseRefused(Refusal.PROMPT_MISMATCH, "the prompt is not the prepared one")
        if not isinstance(signature, SignedObject):
            return PurchaseRefused(Refusal.BAD_SIGNATURE, "MALFORMED_SIGNATURE")
        if signature.object_digest != prepared.request_digest:
            return PurchaseRefused(Refusal.REQUEST_MISMATCH, "signed a different request")
        if signature.key_id != prepared.agent_key_id:
            return PurchaseRefused(Refusal.WRONG_AGENT, "signed by another key")
        refused = self._agent_refusal(prepared.agent_key_id, market.facility)
        if refused is not None:
            return refused
        key = self._registry.lookup(prepared.agent_key_id)
        if key is None or key.subject != prepared.agent_subject:
            return PurchaseRefused(Refusal.AGENT_NOT_AUTHORIZED, "agent subject changed")
        verification = verify_signed(
            prepared.request,
            signature,
            self._registry,
            role=KeyRole.AGENT,
            subject=market.facility.borrower_account.value,
        )
        if not verification.signature_valid:
            failure = verification.failure
            return PurchaseRefused(
                Refusal.BAD_SIGNATURE, "UNKNOWN" if failure is None else failure.value
            )
        if not verification.key_current:
            return PurchaseRefused(Refusal.AGENT_REVOKED, prepared.agent_key_id.value)
        if not self._clock().value < prepared.expires_at.value:
            return PurchaseRefused(Refusal.PREPARATION_EXPIRED, prepared.expires_at.value)
        if market.digest() != prepared.market_digest:
            return PurchaseRefused(
                Refusal.PREPARATION_STALE, "the facility terms or offers changed; prepare again"
            )
        return None

    def _accounted_facility(self, presented: Facility) -> Facility | PurchaseRefused:
        """The presented facility with Core's stored drawn, or a refusal (steps 3-4)."""
        facility_id = presented.facility_id
        try:
            exposure = self._coordinator.exposure(facility_id, self._clock())
        except SettlementRefused as exc:
            if exc.reason == "UNACCOUNTED_FACILITY":
                return PurchaseRefused(Refusal.UNREGISTERED_FACILITY, facility_id.value)
            raise
        if exposure.limit != presented.limit_micro_trlusd:
            return PurchaseRefused(
                Refusal.FACILITY_LIMIT_MISMATCH,
                f"stored limit {exposure.limit.value}, presented "
                f"{presented.limit_micro_trlusd.value}",
            )
        return replace(presented, drawn_micro_trlusd=exposure.drawn)

    def _purchase_locked(
        self, job: Job, signer: AgentSigner, prompt_digest: Sha256Digest
    ) -> PurchaseReport | PurchaseRefused:
        facility = self._accounted_facility(job.facility)
        if isinstance(facility, PurchaseRefused):
            return facility
        requests = purchase_requests(
            job.idempotency_key,
            facility.facility_id,
            job.offers,
            prompt_digest,
            job.request_expires_at,
        )
        candidates = [
            Purchase(
                request=request,
                request_signature=signer(request),
                prompt=job.prompt,
                facility=facility,
                quote=offer.quote,
                service_class=offer.service_class,
                execution_profile=offer.execution_profile,
                procurement_classes=job.procurement_classes,
            )
            for request, offer in zip(requests, job.offers, strict=True)
        ]
        return self._select_and_run(job.idempotency_key, prompt_digest, candidates, owner=None)

    def _select_and_run(
        self,
        idempotency_key: str,
        prompt_digest: Sha256Digest,
        candidates: list[Purchase],
        *,
        owner: RegisteredKey | None,
    ) -> PurchaseReport:
        """Steps 6-15 for signed candidates of one job. `owner` is the agent key the caller has
        already verified signed them (remote signing); it is recorded on a `no_qualifying_offer`
        entry so the agent can read it. The legacy in-process path passes None, as before."""
        facility_id = candidates[0].request.facility_id
        now = self._clock()
        operation_id = operation_id_for(idempotency_key)
        selection = self._gateway.select(candidates)
        if selection.chosen is None:
            reasons = sorted(
                {q.reason_code.value for q in selection.qualifications if q.reason_code}
            )
            fields: dict[str, object] = {
                "operation_id": operation_id,
                "idempotency_key": idempotency_key,
                "facility_id": facility_id.value,
                "prompt_digest": prompt_digest.value,
                "state": S.NO_QUALIFYING_OFFER.value,
                "detail": ",".join(reasons),
            }
            if owner is not None and len(candidates) == 1:
                only = candidates[0]
                fields |= {
                    "agent_key_id": owner.key_id.value,
                    "agent_subject": owner.subject,
                    "provider_id": only.request.provider_id.value,
                    "request_digest": only.request.digest().value,
                    "request": only.request.canonical_bytes(),
                    "request_signature": None
                    if only.request_signature is None
                    else only.request_signature.canonical_bytes(),
                }
            op = self._journal.create(fields, now)
            return PurchaseReport.of(op)

        chosen = selection.chosen.purchase
        signature = chosen.request_signature
        signer_key = None if signature is None else self._registry.lookup(signature.key_id)
        reservation_id = reservation_id_for(chosen.request)
        self._journal.create(
            {
                "operation_id": operation_id,
                "idempotency_key": idempotency_key,
                "facility_id": facility_id.value,
                "prompt_digest": prompt_digest.value,
                "provider_id": chosen.request.provider_id.value,
                "reservation_id": reservation_id.value,
                "request_digest": chosen.request.digest().value,
                "agent_key_id": None if signature is None else signature.key_id.value,
                "agent_subject": None if signer_key is None else signer_key.subject,
                "request": chosen.request.canonical_bytes(),
                "request_signature": None if signature is None else signature.canonical_bytes(),
                "quote": chosen.quote.canonical_bytes(),
                "service_class": chosen.service_class.canonical_bytes(),
                "execution_profile": chosen.execution_profile.canonical_bytes(),
                "state": S.INTENT.value,
            },
            now,
        )
        self._crash("after_intent")

        with self._revocation_gate:  # a revocation committed first is seen by the policy check
            self._check_identity()  # before Core's reservation write
            signature = chosen.request_signature
            if signature is not None:
                assert self._durable_registry is not None
                self._durable_registry.persisted_revocation(signature.key_id)  # fail closed
            reserved = self._reserve(chosen)
        self._crash("after_reserve")
        return self._after_reserve(operation_id, reservation_id, chosen, reserved)

    def _reserve(self, chosen: Purchase) -> ReserveResult:
        return self._coordinator.reserve(
            SignedPurchase(
                request=chosen.request,
                request_signature=chosen.request_signature,
                facility=chosen.facility,
                quote=chosen.quote,
                service_class=chosen.service_class,
                execution_profile=chosen.execution_profile,
                procurement_classes=chosen.procurement_classes,
            ),
            self._clock(),
        )

    def _after_reserve(
        self,
        operation_id: str,
        reservation_id: ReservationId,
        chosen: Purchase,
        reserved: ReserveResult,
    ) -> PurchaseReport:
        if reserved.reservation is None:
            code = reserved.decision.record.reason_code
            op = self._journal.transition(
                operation_id,
                S.INTENT,
                S.POLICY_REJECTED,
                self._clock(),
                reason_code=None if code is None else code.value,
            )
            return PurchaseReport.of(op)
        assert reserved.reservation.reservation_id == reservation_id
        self._journal.transition(operation_id, S.INTENT, S.RESERVED, self._clock())
        return PurchaseReport.of(self._deliver_and_settle(operation_id, chosen, reserved))

    def _reference(self, op: Operation) -> PurchaseReference:
        request, quote = op.request(), op.quote()
        service_class, profile = op.service_class(), op.execution_profile()
        assert request is not None and quote is not None
        assert service_class is not None and profile is not None and op.reservation_id
        return PurchaseReference(
            ReservationId(op.reservation_id), request, quote, service_class, profile
        )

    def _deliver_and_settle(
        self, operation_id: str, chosen: Purchase, reserved: ReserveResult
    ) -> Operation:
        """From RESERVED: delivery attempt, provider call, evidence, settlement."""
        op = self._journal.get(operation_id)
        assert op is not None and reserved.reservation is not None
        ref = self._reference(op)
        if not self._gateway.connected(chosen.request.provider_id):
            return self._release_undelivered(op, NoDeliveryReason.PROVIDER_NOT_CONNECTED)

        head = self._coordinator.chain_head(chosen.request.facility_id)
        # Admitted before a revocation, but not yet executing: a revocation committed first stops
        # the provider call (I1g, F1). The gate orders this check against revocation commits.
        with self._revocation_gate:
            self._check_identity()  # before Core records the delivery attempt
            if self._agent_revoked(op):
                return self._release_undelivered(
                    op,
                    NoDeliveryReason.ABANDONED_BEFORE_EXECUTION,
                    detail="agent key revoked before execution",
                )
            self._journal.transition(operation_id, S.RESERVED, S.EXECUTING, self._clock())
            self._crash("after_executing_recorded")
            try:
                attempt = self._coordinator.begin_delivery(ref, self._clock())
            except SettlementRefused as exc:
                if exc.reason != "EXPIRED":
                    raise
                return self._journal.transition(
                    operation_id, S.EXECUTING, S.EXPIRED, self._clock(), detail="EXPIRED"
                )
        self._crash("after_attempt_begun")

        try:
            outcome = self._gateway.deliver(chosen, reserved, attempt, previous_receipt_digest=head)
        except DeliveryNotAuthorized as exc:
            if exc.reason != "EXPIRED":
                raise  # left EXECUTING: recovery treats it as unknown (fail closed)
            return self._journal.transition(
                operation_id, S.EXECUTING, S.EXPIRED, self._clock(), detail="EXPIRED"
            )
        self._crash("after_provider_returned")
        if isinstance(outcome, ProviderFailed):
            self._coordinator.record_unknown_delivery(
                ref.reservation_id, _UNKNOWN_CAUSE[outcome.kind], self._clock()
            )
            return self._journal.transition(
                operation_id, S.EXECUTING, S.UNKNOWN, self._clock(), detail=outcome.kind.value
            )
        assert isinstance(outcome, DeliveryCaptured)
        evidence = outcome.evidence
        self._journal.transition(
            operation_id,
            S.EXECUTING,
            S.DELIVERED,
            self._clock(),
            receipt=evidence.receipt.canonical_bytes(),
            gateway_signature=evidence.gateway_signature.canonical_bytes(),
            provider_attestation=(
                None
                if evidence.provider_attestation is None
                else evidence.provider_attestation.canonical_bytes()
            ),
            provider_signature=(
                None
                if evidence.provider_signature is None
                else evidence.provider_signature.canonical_bytes()
            ),
            receipt_digest=evidence.receipt.digest().value,
            response_digest=payload_digest(outcome.response).value,
            response=outcome.response,
        )
        self._crash("after_evidence_stored")
        return self._settle(operation_id)

    def _submission(self, op: Operation) -> DeliverySubmission:
        ref = self._reference(op)
        receipt, gateway_signature = op.receipt(), op.gateway_signature()
        assert receipt is not None and gateway_signature is not None and op.response is not None
        return DeliverySubmission(
            reservation_id=ref.reservation_id,
            receipt=receipt,
            gateway_signature=gateway_signature,
            provider_attestation=op.provider_attestation(),
            provider_signature=op.provider_signature(),
            delivered_response_digest=payload_digest(op.response),
            request=ref.request,
            quote=ref.quote,
            service_class=ref.service_class,
            execution_profile=ref.execution_profile,
        )

    def _settle(self, operation_id: str) -> Operation:
        """From DELIVERED: submit the stored evidence to Core."""
        op = self._journal.get(operation_id)
        assert op is not None and op.state is S.DELIVERED
        self._check_identity()  # before Core's settlement write
        try:
            result = self._coordinator.settle(self._submission(op), self._clock())
        except SettlementRefused as exc:
            if exc.reason == "EXPIRED":
                return self._journal.transition(
                    operation_id, S.DELIVERED, S.LATE, self._clock(), detail="EXPIRED"
                )
            return self._journal.transition(
                operation_id, S.DELIVERED, S.DISPUTED, self._clock(), detail=exc.reason
            )
        self._crash("after_settle_committed")
        record = result.verifier_record
        to = {
            SettlementOutcome.ACCOUNTING_SETTLED: S.SETTLED,
            SettlementOutcome.DELIVERY_FAILED: S.DELIVERY_FAILED,
            SettlementOutcome.REJECTED_SUBMISSION: S.DISPUTED,
        }[result.outcome]
        return self._journal.transition(
            operation_id,
            S.DELIVERED,
            to,
            self._clock(),
            settlement_outcome=result.outcome.value,
            verifier_decision_digest=record.digest().value,
            reason_code=None if record.reason_code is None else record.reason_code.value,
        )

    def _agent_revoked(self, op: Operation) -> bool:
        if op.agent_key_id is None:
            return True  # no agent key: nothing authorizes execution
        assert self._durable_registry is not None
        if self._durable_registry.persisted_revocation(KeyId(op.agent_key_id)):
            return True
        key = self._registry.lookup(KeyId(op.agent_key_id))
        return key is None or key.status is not KeyStatus.CURRENT

    def _release_undelivered(
        self, op: Operation, reason: NoDeliveryReason, *, detail: str | None = None
    ) -> Operation:
        try:
            self._coordinator.release_undelivered(self._reference(op), reason, self._clock())
        except SettlementRefused as exc:
            if exc.reason != "EXPIRED":
                raise
            return self._journal.transition(
                op.operation_id, op.state, S.EXPIRED, self._clock(), detail="EXPIRED"
            )
        return self._journal.transition(
            op.operation_id,
            op.state,
            S.RELEASED_UNDELIVERED,
            self._clock(),
            detail=detail or reason.value,
        )

    # --- response retrieval --------------------------------------------------------------

    def _authorize(self, authorization: ResponseAuthorization, action: ResponseAction) -> Operation:
        op = self._journal.get(authorization.operation_id)
        if op is None:
            raise NotAuthorized("UNKNOWN_OPERATION")
        verify_response_authorization(
            authorization,
            action=action,
            operation_id=op.operation_id,
            agent_key_id=op.agent_key_id,
            agent_subject=op.agent_subject,
            registry=self._registry,
            now=self._clock(),
        )
        return op

    def retrieve(self, authorization: ResponseAuthorization) -> bytes:
        """The response, only after accounting settlement, and only for a caller holding the
        requesting agent's key (an agent-signed, single-use authorization for this operation).

        Atomic (I1g, F2): revocation, availability, nonce consumption, the retrieval record and
        the read of the exact bytes are one transaction; a refusal consumes no nonce."""
        self._require_open()
        op = self._authorize(authorization, ResponseAction.RETRIEVE)
        if op.state in RESPONSE_AVAILABLE and not self._coordinator.receipt_settled(
            Sha256Digest(str(op.receipt_digest))
        ):
            raise JournalInconsistency(f"{op.operation_id}: journal settled, Core not settled")
        try:
            return self._journal.authorized_retrieve(
                agent_key_id=authorization.agent_key_id.value,
                nonce=authorization.nonce,
                operation_id=op.operation_id,
                now=self._clock(),
            )
        except NonceReused:
            raise NotAuthorized("NONCE_REUSED") from None
        except KeyRevoked:
            raise NotAuthorized("REVOKED_KEY") from None
        except StateRefused as exc:
            raise ResponseUnavailable(op.operation_id, exc.state or op.state) from None

    def acknowledge(self, authorization: ResponseAuthorization) -> None:
        """The agent confirms it holds the response; only then is the retained copy deleted.
        Atomic like `retrieve`; acknowledgment requires a completed retrieval."""
        self._require_open()
        op = self._authorize(authorization, ResponseAction.ACKNOWLEDGE)
        try:
            self._journal.authorized_acknowledge(
                agent_key_id=authorization.agent_key_id.value,
                nonce=authorization.nonce,
                operation_id=op.operation_id,
                now=self._clock(),
            )
        except NonceReused:
            raise NotAuthorized("NONCE_REUSED") from None
        except KeyRevoked:
            raise NotAuthorized("REVOKED_KEY") from None
        except StateRefused as exc:
            raise ResponseUnavailable(op.operation_id, exc.state or op.state) from None

    # --- recovery ------------------------------------------------------------------------

    def recover(self) -> list[Reconciliation]:
        """Reconcile every in-flight operation with Core's records, oldest first."""
        self._require_open()
        for op in self._journal.in_states(RESPONSE_AVAILABLE):
            digest = op.receipt_digest
            if digest is None or not self._coordinator.receipt_settled(Sha256Digest(digest)):
                raise JournalInconsistency(f"{op.operation_id}: journal settled, Core not settled")
        done = []
        for op in self._journal.in_flight():
            with self._facility_lock(op.facility_id):
                current = self._journal.get(op.operation_id)
                assert current is not None
                if current.state not in IN_FLIGHT:
                    continue
                after = self._recover_one(current)
                done.append(Reconciliation(op.operation_id, current.state, after.state))
        return done

    def _reservation_status(self, op: Operation) -> tuple[ReservationStatus, str | None] | None:
        assert op.reservation_id is not None
        rid = ReservationId(op.reservation_id)
        try:
            reservation = self._ledger.get(rid)
        except ReservationStateError as exc:
            if exc.reason == "NOT_FOUND":
                return None
            raise
        return reservation.status, self._ledger.release_reason(rid)

    def _recover_one(self, op: Operation) -> Operation:
        now = self._clock
        if op.state is S.INTENT:
            status = self._reservation_status(op)
            if status is None:
                return self._journal.transition(
                    op.operation_id, S.INTENT, S.ABANDONED, now(), detail="no reservation in Core"
                )
            if status[0] is ReservationStatus.SETTLED:
                raise JournalInconsistency(f"{op.operation_id}: settled with no evidence")
            op = self._journal.transition(op.operation_id, S.INTENT, S.RESERVED, now())

        if op.state in (S.RESERVED, S.EXECUTING):
            assert op.reservation_id is not None
            attempt = self._coordinator.delivery_attempt(ReservationId(op.reservation_id))
            status = self._reservation_status(op)
            assert status is not None
            if attempt is None:
                # Core never authorized execution, so no provider was called.
                if status[0] is ReservationStatus.HELD:
                    return self._release_undelivered(
                        op, NoDeliveryReason.ABANDONED_BEFORE_EXECUTION
                    )
                if status == (ReservationStatus.RELEASED, "expired"):
                    return self._journal.transition(
                        op.operation_id, op.state, S.EXPIRED, now(), detail="EXPIRED"
                    )
                if status[0] is ReservationStatus.RELEASED and self._released_undelivered(op):
                    return self._journal.transition(
                        op.operation_id,
                        op.state,
                        S.RELEASED_UNDELIVERED,
                        now(),
                        detail="reconciled with Core",
                    )
                raise JournalInconsistency(f"{op.operation_id}: {status} with no attempt")
            if status[0] is ReservationStatus.SETTLED:
                raise JournalInconsistency(f"{op.operation_id}: settled with no evidence")
            # The provider may have executed and nothing was captured: unknown, never retried.
            self._coordinator.record_unknown_delivery(
                attempt.reservation_id, UnknownDeliveryCause.INTERRUPTED, now()
            )
            return self._journal.transition(
                op.operation_id, op.state, S.UNKNOWN, now(), detail="interrupted"
            )

        assert op.state is S.DELIVERED
        digest = Sha256Digest(str(op.receipt_digest))
        if self._coordinator.receipt_settled(digest):
            return self._journal.transition(
                op.operation_id,
                S.DELIVERED,
                S.SETTLED,
                now(),
                settlement_outcome=SettlementOutcome.ACCOUNTING_SETTLED.value,
                detail="reconciled with Core",
            )
        for kind, _, _, receipt, _, reason, _ in self._coordinator.disputes():
            if receipt == digest.value:
                to = S.DELIVERY_FAILED if kind == "delivery_failure" else S.DISPUTED
                return self._journal.transition(
                    op.operation_id,
                    S.DELIVERED,
                    to,
                    now(),
                    reason_code=reason,
                    detail="reconciled with Core",
                )
        if any(d.receipt_digest == digest for d in self._coordinator.late_delivery_disputes()):
            return self._journal.transition(
                op.operation_id, S.DELIVERED, S.LATE, now(), detail="reconciled with Core"
            )
        return self._settle(op.operation_id)  # evidence is durable: submit it now

    def _released_undelivered(self, op: Operation) -> bool:
        return any(
            r.reservation_id.value == op.reservation_id
            for r in self._coordinator.no_delivery_releases()
        )
