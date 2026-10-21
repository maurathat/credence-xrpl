"""I2a marketplace adapter: the product API over the embedded IntegrationService. OFFLINE PROVIDERS.

Every decision is made below this layer: Core decides eligibility, reservation, verification and
accounting settlement; the Integration service decides preparation, submission, retrieval and
acknowledgment. This adapter authenticates the agent session, checks that the agent may see what
it asks for, validates and decodes input, and serializes. It holds no agent key and accepts none:
the agent signs prepared requests and response authorizations outside the browser and the API.

- Authentication: a bearer session token per agent. Only its SHA-256 is configured on the server.
  It authenticates API calls; it is not a signing key and authorizes no purchase by itself (the
  agent's Ed25519 signature does).
- Authorization: an agent sees only its own facilities (borrower account = its key's subject),
  preparations and operations (signed by its key). Anything else answers 404, as if absent.
- Providers are the offline mocks of `offline_world.py`. Prices are configured demo prices.
- The buyer price cap stays preview-only (CR-2 is not implemented): a purchase with a cap is
  refused, never silently dropped.
- XRPL: the payment status is `not_submitted` from `payment_status.NoXrplPaymentStatus`.
  Accounting settlement is not XRPL settlement.

Owner: API/UI (I2a integration). Provisional: subject to security review.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from credence.api import explanations as text
from credence.api.offline_world import PROVIDER_LABEL, FacilityConfig
from credence.api.payment_status import NoXrplPaymentStatus, PaymentStatusSource
from credence.api.product import (
    PROVIDER_PROFILES,
    ProcurementForm,
    _tier,
    procurement_class_for,
    receipt_checks,
    utc_now,
)
from credence.core.billing import max_total_cost_value
from credence.core.objects import SignedObject
from credence.core.primitives import (
    DomainError,
    Ed25519Signature,
    EvidenceTier,
    FacilityId,
    KeyId,
    Sha256Digest,
    Timestamp,
)
from credence.core.signatures import (
    SIGNATURE_DOMAIN,
    KeyRegistry,
    KeyRole,
    KeyStatus,
    RegisteredKey,
    signature_message,
)
from credence.integration.authorization import (
    NotAuthorized,
    ResponseAction,
    ResponseAuthorization,
)
from credence.integration.journal import RESPONSE_AVAILABLE, Operation, OperationState
from credence.integration.preparation import Preparation
from credence.integration.service import (
    IntegrationService,
    JournalInconsistency,
    Market,
    Offer,
    PurchaseRefused,
    PurchaseReport,
    Refusal,
    ResponseUnavailable,
    ServiceClosed,
    UnknownOperation,
    UnknownPreparation,
)

JSON = dict[str, Any]

SOURCE = "integrated_offline_service"
SOURCE_LABEL = (
    "Integrated IntegrationService in this API process, with offline mock providers (no provider "
    "call), TEST provider and gateway keys and configured demo prices"
)
ACCOUNTING_NOTE = (
    "Accounting settlement is Credence's internal record. It is not an XRPL payment and does not "
    "confirm one."
)
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
_PREPARATION_ID = re.compile(r"^prep-[0-9a-f]{32}$")
_OPERATION_ID = re.compile(r"^op-[0-9a-f]{32}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")

# Refusal -> HTTP status. Codes are passed through unchanged; the API invents none.
REFUSAL_STATUS: dict[Refusal, int] = {
    Refusal.UNREGISTERED_FACILITY: 422,
    Refusal.FACILITY_LIMIT_MISMATCH: 422,
    Refusal.FACILITY_BUSY: 409,
    Refusal.PROMPT_NOT_UTF8: 422,
    Refusal.NO_OFFERS: 422,
    Refusal.IDEMPOTENCY_CONFLICT: 409,
    Refusal.AGENT_NOT_AUTHORIZED: 403,
    Refusal.AGENT_REVOKED: 403,
    Refusal.NO_QUALIFYING_OFFER: 422,
    Refusal.ALREADY_SUBMITTED: 409,
    Refusal.UNKNOWN_PREPARATION: 404,
    Refusal.PREPARATION_EXPIRED: 410,
    Refusal.PREPARATION_STALE: 409,
    Refusal.PREPARATION_CONSUMED: 409,
    Refusal.PROMPT_MISMATCH: 422,
    Refusal.REQUEST_MISMATCH: 422,
    Refusal.WRONG_AGENT: 403,
    Refusal.BAD_SIGNATURE: 403,
}

# Live lifecycle stages, each reached only when the record that proves it exists.
_STAGE_EVIDENCE: tuple[tuple[OperationState, str], ...] = (
    (OperationState.INTENT, "intent_journaled"),
    (OperationState.RESERVED, "reserved"),
    (OperationState.EXECUTING, "delivery_attempt_recorded"),
    (OperationState.DELIVERED, "provider_completed"),
    (OperationState.SETTLED, "accounting_settled"),
    (OperationState.RETRIEVED, "retrieved"),
    (OperationState.ACKNOWLEDGED, "acknowledged"),
)


class MarketplaceError(ValueError):
    """A request the adapter refuses. `code` is stable; `status` is the HTTP status."""

    def __init__(self, code: str, detail: str, status: int) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.status = status


@dataclass(frozen=True)
class AgentPrincipal:
    agent_id: str
    key_id: KeyId


@dataclass(frozen=True)
class Session:
    principal: AgentPrincipal
    subject: str


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AgentDirectory:
    """Agent session tokens, kept only as SHA-256 digests."""

    def __init__(self, entries: Mapping[str, AgentPrincipal]) -> None:
        for digest in entries:
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("token digests must be 64 lower-case hex digits")
        self._entries = dict(entries)

    def authenticate(self, token: str) -> AgentPrincipal | None:
        presented = token_digest(token)
        found = None
        for digest, principal in self._entries.items():
            if hmac.compare_digest(digest, presented):
                found = principal
        return found


@dataclass(frozen=True)
class PrepareRequest:
    form: ProcurementForm  # the prompt field is unused: only its digest is sent
    prompt_digest: str
    facility_id: str | None
    idempotency_key: str | None


def _canonical(obj: Any) -> JSON:
    return {"digest": obj.digest().value, "object": obj.to_canonical()}


def _refused(result: PurchaseRefused) -> MarketplaceError:
    return MarketplaceError(result.refusal.value, result.detail, REFUSAL_STATUS[result.refusal])


class Marketplace:
    def __init__(
        self,
        service: IntegrationService,
        *,
        facilities: Mapping[str, FacilityConfig],
        offers: tuple[Offer, ...],
        directory: AgentDirectory,
        payments: PaymentStatusSource | None = None,
        clock: Callable[[], Timestamp] = utc_now,
    ) -> None:
        self._service = service
        self._facilities = dict(facilities)
        self._offers = offers
        self._directory = directory
        self._payments = payments or NoXrplPaymentStatus()
        self._clock = clock

    # --- health and session --------------------------------------------------------------

    def health(self) -> JSON:
        return {
            "integrated_service": True,
            "providers": "offline_mock",
            "providers_note": PROVIDER_LABEL,
            "live_provider_execution": False,
            "buyer_price_cap": "preview_only",
            "xrpl": "not_connected",
        }

    def authenticate(self, authorization: str | None) -> Session:
        if authorization is None or not authorization.startswith("Bearer "):
            raise MarketplaceError("AUTHENTICATION_REQUIRED", "send an agent session token", 401)
        principal = self._directory.authenticate(authorization.removeprefix("Bearer ").strip())
        if principal is None:
            raise MarketplaceError("INVALID_SESSION", "unknown agent session token", 401)
        key = self._service.public_key(principal.key_id)
        if key is None or key.role is not KeyRole.AGENT:
            raise MarketplaceError("AGENT_NOT_AUTHORIZED", "the agent key is not registered", 403)
        if key.status is not KeyStatus.CURRENT:
            raise MarketplaceError("AGENT_REVOKED", "the agent key is revoked", 403)
        return Session(principal, key.subject)

    def _own_facilities(self, session: Session) -> list[FacilityConfig]:
        return [
            c
            for c in self._facilities.values()
            if c.facility.borrower_account.value == session.subject
        ]

    def _own_facility(self, session: Session, facility_id: str) -> FacilityConfig:
        for config in self._own_facilities(session):
            if config.facility.facility_id.value == facility_id:
                return config
        raise MarketplaceError("UNKNOWN_FACILITY", facility_id, 404)

    def _market(self, config: FacilityConfig) -> Market:
        return Market(config.facility, config.procurement_classes, self._offers)

    def session(self, session: Session) -> JSON:
        return {
            "agent_id": session.principal.agent_id,
            "agent_key_id": session.principal.key_id.value,
            "subject": session.subject,
            "key_status": "current",
            "facility_ids": [c.facility.facility_id.value for c in self._own_facilities(session)],
            "note": "The session token authenticates API calls. It is not a signing key: the "
            "agent's private key never reaches the API or the browser.",
        }

    # --- facilities ----------------------------------------------------------------------

    def facilities(self, session: Session) -> list[JSON]:
        return [self._facility_view(c) for c in self._own_facilities(session)]

    def _facility_view(self, config: FacilityConfig) -> JSON:
        facility = config.facility
        now = self._clock()
        exposure = self._service.accounting.exposure(facility.facility_id, now)
        return {
            "facility_id": facility.facility_id.value,
            "title": config.title,
            "terms": _canonical(facility),
            "terms_source": "Operator configuration of this process (the terms each purchase is "
            "evaluated under). Core stores the limit and drawn amount.",
            "procurement_classes": [_canonical(pc) for pc in config.procurement_classes],
            "minimum_evidence_tier": _tier(facility.minimum_evidence_tier),
            "allowed_providers": [p.value for p in facility.allowed_providers],
            "status": facility.status.value,
            "maturity_at": facility.maturity_at.value,
            "xrpl_loan_id": None if facility.xrpl_loan_id is None else facility.xrpl_loan_id.value,
            "exposure": {
                "at": now.value,
                "limit_micro_trlusd": exposure.limit.value,
                "drawn_micro_trlusd": exposure.drawn.value,
                "held_micro_trlusd": exposure.held.value,
                "headroom_micro_trlusd": exposure.headroom,
                "source": "Core AccountingView.exposure, read now",
            },
            "invariant": "limit = drawn + held + headroom",
            "history_note": "Core keeps no exposure history; this is the current point only "
            "(CR-5, history persistence, is not implemented).",
        }

    # --- prepare and submit (CR-1) -------------------------------------------------------

    def prepare(self, session: Session, body: PrepareRequest) -> JSON:
        if body.form.max_total_cost_micro_trlusd is not None:
            raise MarketplaceError(
                "PRICE_CAP_PREVIEW_ONLY",
                "the buyer price cap is preview-only: the purchase path cannot carry it yet "
                "(CR-2). Clear it to purchase at each quote's own maximum.",
                422,
            )
        if not _DIGEST.match(body.prompt_digest):
            raise MarketplaceError("INVALID_PROMPT_DIGEST", "sha256:<64 hex>", 422)
        if body.idempotency_key is not None and not _IDEMPOTENCY_KEY.match(body.idempotency_key):
            raise MarketplaceError("INVALID_IDEMPOTENCY_KEY", "8-128 of [A-Za-z0-9._:-]", 422)
        try:
            procurement = procurement_class_for(body.form)
        except (ValueError, TypeError, DomainError) as exc:
            raise MarketplaceError("INVALID_PROCUREMENT", str(exc), 422) from exc
        configs = (
            [self._own_facility(session, body.facility_id)]
            if body.facility_id is not None
            else self._own_facilities(session)
        )
        matching = [
            c for c in configs if procurement.digest() in c.facility.allowed_procurement_classes
        ]
        if not matching:
            raise MarketplaceError(
                "PROCUREMENT_NOT_REGISTERED",
                "no facility of this agent allows this procurement class. Purchases are made "
                "only under a procurement class registered on the agent's facility; the dry run "
                "evaluates any procurement.",
                422,
            )
        if len(matching) > 1:
            raise MarketplaceError("FACILITY_REQUIRED", "name the facility", 422)
        # Idempotency keys are namespaced per agent, so one agent's key never names another's.
        key = (
            None
            if body.idempotency_key is None
            else f"agent:{session.principal.key_id.value}:{body.idempotency_key}"
        )
        result = self._service.prepare(
            self._market(matching[0]),
            agent_key_id=session.principal.key_id,
            prompt_digest=Sha256Digest(body.prompt_digest),
            idempotency_key=key,
        )
        if isinstance(result, PurchaseRefused):
            raise _refused(result)
        return self._preparation_view(result)

    def _own_preparation(self, session: Session, preparation_id: str) -> Preparation:
        if not _PREPARATION_ID.match(preparation_id):
            raise MarketplaceError("UNKNOWN_PREPARATION", preparation_id, 404)
        try:
            prepared = self._service.preparation(preparation_id)
        except UnknownPreparation:
            raise MarketplaceError("UNKNOWN_PREPARATION", preparation_id, 404) from None
        if prepared.agent_key_id != session.principal.key_id:
            raise MarketplaceError("UNKNOWN_PREPARATION", preparation_id, 404)
        return prepared

    def preparation(self, session: Session, preparation_id: str) -> JSON:
        return self._preparation_view(self._own_preparation(session, preparation_id))

    def _preparation_view(self, prepared: Preparation) -> JSON:
        request = prepared.request
        return {
            "preparation_id": prepared.preparation_id,
            "operation_id": prepared.operation_id,
            "agent_key_id": prepared.agent_key_id.value,
            "facility_id": prepared.facility_id.value,
            "provider_id": request.provider_id.value,
            "provider": PROVIDER_PROFILES.get(request.provider_id.value),
            "provider_label": PROVIDER_LABEL,
            "quote_id": request.quote_id.value,
            "nonce": request.nonce.value,
            "created_at": prepared.created_at.value,
            "expires_at": prepared.expires_at.value,
            "prompt_digest": prepared.prompt_digest.value,
            "market_digest": prepared.market_digest.value,
            "request": {
                "digest": request.digest().value,
                "object": request.to_canonical(),
                "canonical_json": request.canonical_bytes().decode("utf-8"),
            },
            "signing": {
                "domain": SIGNATURE_DOMAIN,
                "statement_hex": signature_message(request).hex(),
                "note": "Sign this request's existing C3A statement with the agent's own Ed25519 "
                "key, outside the browser, after verifying every field.",
            },
            "price": {
                "max_total_cost_micro_trlusd": request.max_total_cost_micro_trlusd.value,
                "price_kind": "configured_demo",
                "label": "Configured demo price in micro-tRLUSD (tRLUSD is a test token): the "
                "quote's maximum at its token caps; Core reserves it and settles the actual cost.",
            },
            "quote": _canonical(prepared.quote),
            "service_class": _canonical(prepared.service_class),
            "execution_profile": {
                **_canonical(prepared.execution_profile),
                "model_identifier": prepared.execution_profile.model_identifier,
            },
            "evidence": _tier(prepared.service_class.evidence_tier),
        }

    def submit(
        self, session: Session, preparation_id: str, prompt: str, signature: Mapping[str, Any]
    ) -> JSON:
        prepared = self._own_preparation(session, preparation_id)
        config = self._own_facility(session, prepared.facility_id.value)
        try:
            signed = SignedObject.from_canonical(dict(signature))
        except (ValueError, TypeError, KeyError, DomainError) as exc:
            raise MarketplaceError("MALFORMED_SIGNATURE", str(exc), 422) from exc
        try:
            body = prompt.encode("utf-8")
        except UnicodeEncodeError:
            raise MarketplaceError("PROMPT_NOT_UTF8", "the prompt is not UTF-8", 422) from None
        try:
            result = self._service.submit_prepared(
                self._market(config),
                preparation_id=preparation_id,
                signature=signed,
                prompt=body,
                agent_key_id=session.principal.key_id,
            )
        except ServiceClosed:
            raise MarketplaceError("SERVICE_UNAVAILABLE", "the service is closed", 503) from None
        except JournalInconsistency as exc:
            raise MarketplaceError("JOURNAL_INCONSISTENCY", str(exc), 500) from None
        if isinstance(result, PurchaseRefused):
            raise _refused(result)
        return self._report_view(result)

    def _report_view(self, report: PurchaseReport) -> JSON:
        return {
            "operation_id": report.operation_id,
            "state": report.state.value,
            "outcome_class": text.OUTCOME_CLASS[report.state],
            "state_text": text.STATE_TEXT[report.state],
            "detail": report.detail,
            "reservation_id": report.reservation_id,
            "provider_id": report.provider_id,
            "receipt_digest": report.receipt_digest,
            "reason_code": report.reason_code,
            "source": SOURCE,
        }

    # --- operations (CR-3, CR-4) ---------------------------------------------------------

    def _own_operation(self, session: Session, operation_id: str) -> Operation:
        if not _OPERATION_ID.match(operation_id):
            raise MarketplaceError("UNKNOWN_OPERATION", operation_id, 404)
        try:
            op = self._service.journal_entry(operation_id)
        except UnknownOperation:
            raise MarketplaceError("UNKNOWN_OPERATION", operation_id, 404) from None
        if (op.agent_key_id, op.agent_subject) != (
            session.principal.key_id.value,
            session.subject,
        ):
            raise MarketplaceError("UNKNOWN_OPERATION", operation_id, 404)
        return op

    def _summary(self, op: Operation) -> JSON:
        return {
            "operation_id": op.operation_id,
            "facility_id": op.facility_id,
            "provider_id": op.provider_id,
            "state": op.state.value,
            "outcome_class": text.OUTCOME_CLASS[op.state],
            "reason_code": op.reason_code,
            "detail": op.detail,
            "receipt_digest": op.receipt_digest,
            "created_at": str(op.row["created_at"]),
            "updated_at": str(op.row["updated_at"]),
            "source": SOURCE,
        }

    def operations(self, session: Session, *, limit: int, after: str | None) -> JSON:
        if not 1 <= limit <= 100:
            raise MarketplaceError("INVALID_LIMIT", "limit must be between 1 and 100", 422)
        if after is not None:
            self._own_operation(session, after)  # 404 for another agent's cursor
        try:
            page = self._service.operations_for_agent(
                session.principal.key_id, limit=limit, after=after
            )
        except UnknownOperation:
            raise MarketplaceError("UNKNOWN_OPERATION", str(after), 404) from None
        page = [op for op in page if op.agent_subject == session.subject]
        return {
            "operations": [self._summary(op) for op in page],
            "next_after": page[-1].operation_id if len(page) == limit else None,
            "source": SOURCE,
            "source_label": SOURCE_LABEL,
        }

    def operation(self, session: Session, operation_id: str) -> JSON:
        op = self._own_operation(session, operation_id)
        events = self._service.lifecycle_events(op.operation_id)
        names = {e.event for e in events}
        return {
            **self._summary(op),
            "source_label": SOURCE_LABEL,
            "state_text": text.STATE_TEXT[op.state],
            "lifecycle": [
                {"state": state.value, "label": label, "reached": evidence in names}
                for (state, evidence), (_, label) in zip(
                    _STAGE_EVIDENCE, text.LIFECYCLE, strict=True
                )
            ],
            "events": [
                {"event": e.event, "at": e.at.value, "source": e.source, "detail": e.detail}
                for e in events
            ],
            "events_note": "Each event carries the time its own record persisted. Transitions "
            "with no persisted time (for example delivered) are not shown; nothing is inferred.",
            "operation": {
                "idempotency_key": op.idempotency_key,
                "prompt_digest": op.prompt_digest,
                "request_digest": op.row["request_digest"],
                "reservation_id": op.reservation_id,
                "agent_key_id": op.agent_key_id,
                "response_digest": op.response_digest,
                "settlement_outcome": op.settlement_outcome,
                "verifier_decision_digest": op.row["verifier_decision_digest"],
                "retrieved_at": op.retrieved_at,
                "acknowledged_at": op.acknowledged_at,
            },
            "objects": {
                "purchase_request": None if op.request() is None else op.request().to_canonical(),  # type: ignore[union-attr]
                "purchase_request_signature": _maybe(op.request_signature()),
                "quote": _maybe(op.quote()),
                "service_class": _maybe(op.service_class()),
                "execution_profile": _maybe(op.execution_profile()),
            },
            "quote_max_total_cost_micro_trlusd": None
            if op.quote() is None
            else max_total_cost_value(op.quote()),  # type: ignore[arg-type]
            "response": {
                "available_to_agent": op.state in RESPONSE_AVAILABLE,
                "retained": op.response is not None,
                "note": "Released only after accounting settlement, and only against an "
                "Ed25519 ResponseAuthorization signed by the purchasing agent's own key "
                "(single-use nonce, at most 300 s). The UI never holds that key.",
            },
        }

    def exposure(self, session: Session, operation_id: str) -> JSON:
        op = self._own_operation(session, operation_id)
        config = self._own_facility(session, op.facility_id)
        return {"operation_id": op.operation_id, **self._facility_view(config)}

    def payment(self, session: Session, operation_id: str) -> JSON:
        op = self._own_operation(session, operation_id)
        status = self._payments.status(op.operation_id)
        return {
            "operation_id": op.operation_id,
            "xrpl": {
                "state": status.state,
                "source": status.source,
                "transaction_hash": status.transaction_hash,
                "validated_ledger_index": status.validated_ledger_index,
                "last_ledger_sequence": status.last_ledger_sequence,
                "engine_result": status.engine_result,
                "detail": status.detail,
            },
            "accounting": {
                "settlement_outcome": op.settlement_outcome,
                "state": op.state.value,
                "note": ACCOUNTING_NOTE,
            },
        }

    # --- receipts (CR-3, CR-4) -----------------------------------------------------------

    def _public_registry(self, *key_ids: KeyId) -> KeyRegistry:
        keys: list[RegisteredKey] = []
        for key_id in dict.fromkeys(key_ids):
            key = self._service.public_key(key_id)
            if key is not None:
                keys.append(key)
        return KeyRegistry(keys)

    def receipt(self, session: Session, receipt_digest: str) -> JSON:
        if not _DIGEST.match(receipt_digest):
            raise MarketplaceError("UNKNOWN_RECEIPT", receipt_digest, 404)
        try:
            op = self._service.operation_for_receipt(Sha256Digest(receipt_digest))
        except UnknownOperation:
            raise MarketplaceError("UNKNOWN_RECEIPT", receipt_digest, 404) from None
        try:
            op = self._own_operation(session, op.operation_id)
        except MarketplaceError:
            raise MarketplaceError("UNKNOWN_RECEIPT", receipt_digest, 404) from None
        receipt, gateway = op.receipt(), op.gateway_signature()
        request, profile = op.request(), op.execution_profile()
        assert receipt is not None and gateway is not None
        assert request is not None and profile is not None
        attestation, provider_signed = op.provider_attestation(), op.provider_signature()
        key_ids = [gateway.key_id] + ([] if provider_signed is None else [provider_signed.key_id])
        registry = self._public_registry(*key_ids)
        digest = receipt.digest()
        accounting = self._service.accounting
        settled = accounting.accounting_settlement(digest)
        head = accounting.chain_head(FacilityId(op.facility_id))
        verifier_digest = op.row["verifier_decision_digest"]
        decision = (
            None
            if not isinstance(verifier_digest, str)
            else accounting.decision_record(verifier_digest)
        )
        signer = registry.lookup(gateway.key_id)
        tier = receipt.evidence_tier
        return {
            "receipt_digest": digest.value,
            "source": SOURCE,
            "source_label": SOURCE_LABEL,
            "operation_id": op.operation_id,
            "provider_id": receipt.provider_id.value,
            "provider": PROVIDER_PROFILES.get(receipt.provider_id.value),
            "provider_label": PROVIDER_LABEL,
            "model_identifier": profile.model_identifier,
            "execution_profile_digest": receipt.execution_profile_digest.value,
            "evidence": _tier(tier),
            "evidence_ladder": [
                {
                    **_tier(t),
                    "present": t.value <= tier.value,
                    "implemented": t is not EvidenceTier.MEASURED,
                }
                for t in EvidenceTier
            ],
            "receipt": receipt.to_canonical(),
            "gateway_signature": gateway.to_canonical(),
            "gateway_signer": None
            if signer is None
            else {
                "key_id": signer.key_id.value,
                "role": signer.role.value,
                "subject": signer.subject,
                "status": signer.status.value,
            },
            "provider_attestation": _maybe(attestation),
            "provider_signature": _maybe(provider_signed),
            "purchase_request": request.to_canonical(),
            "checks": receipt_checks(
                receipt,
                gateway,
                attestation,
                provider_signed,
                request,
                profile,
                registry,
                op.receipt_digest or "",
                None,
                "the response is released only to the agent through an authorized retrieval; "
                "the receipt's response digest is shown as signed",
            ),
            "checks_note": "Recomputed now by Core's digest and signature functions against the "
            "service's trusted registry (public key records only).",
            "verifier": {
                "outcome": op.settlement_outcome,
                "reason_code": op.reason_code,
                "decision_digest": verifier_digest,
                "decision": None if decision is None else decision.to_canonical(),
            },
            "accounting": {
                "settled": settled is not None,
                "actual_amount_micro_trlusd": None
                if settled is None
                else settled.actual_amount.value,
                "settled_at": None if settled is None else settled.settled_at.value,
                "reservation_id": None if settled is None else settled.reservation_id.value,
                "note": ACCOUNTING_NOTE,
            },
            "disputes": [
                {"kind": kind, "reason_code": reason}
                for kind, reason, _ in accounting.dispute_evidence(digest)
            ],
            "late_delivery": any(
                d.receipt_digest == digest for d in accounting.late_delivery_disputes()
            ),
            "chain": {
                "previous_receipt_digest": None
                if receipt.previous_receipt_digest is None
                else receipt.previous_receipt_digest.value,
                "first_on_facility": receipt.previous_receipt_digest is None,
                "accounting_settled": settled is not None,
                "chain_head": None if head is None else head.value,
                "is_chain_head": head == digest,
                "note": "Core's persisted chain head for the facility, read now.",
            },
            "price_micro_trlusd": receipt.price_micro_trlusd.value,
            "input_token_count": receipt.input_token_count,
            "output_token_count": receipt.output_token_count,
        }

    # --- response retrieval and acknowledgment -------------------------------------------

    def respond(
        self, session: Session, operation_id: str, body: Mapping[str, Any]
    ) -> tuple[str, bytes | None]:
        """("retrieve", bytes) or ("acknowledge", None). Never logs or stores `body`."""
        op = self._own_operation(session, operation_id)
        authorization = _authorization(body)
        if authorization.agent_key_id != session.principal.key_id:
            raise MarketplaceError("WRONG_AGENT", "not this session's agent key", 403)
        try:
            if authorization.action is ResponseAction.RETRIEVE:
                return "retrieve", self._service.retrieve(authorization)
            self._service.acknowledge(authorization)
            return "acknowledge", None
        except NotAuthorized as exc:
            raise MarketplaceError(exc.reason, "authorization refused", 403) from None
        except ResponseUnavailable as exc:
            raise MarketplaceError("RESPONSE_UNAVAILABLE", exc.state.value, 409) from None
        except JournalInconsistency as exc:
            raise MarketplaceError("JOURNAL_INCONSISTENCY", str(exc), 500) from None
        except ServiceClosed:
            raise MarketplaceError("SERVICE_UNAVAILABLE", op.operation_id, 503) from None


def _maybe(obj: Any) -> JSON | None:
    return None if obj is None else obj.to_canonical()


def _authorization(body: Mapping[str, Any]) -> ResponseAuthorization:
    expected = {"operation_id", "action", "agent_key_id", "nonce", "expires_at", "signature"}
    if set(body) != expected or not all(isinstance(body[k], str) for k in expected):
        raise MarketplaceError("MALFORMED_AUTHORIZATION", "unexpected members or types", 403)
    try:
        return ResponseAuthorization(
            operation_id=body["operation_id"],
            action=ResponseAction(body["action"]),
            agent_key_id=KeyId(body["agent_key_id"]),
            nonce=body["nonce"],
            expires_at=Timestamp(body["expires_at"]),
            signature=Ed25519Signature(body["signature"]),
        )
    except (ValueError, TypeError, DomainError):
        raise MarketplaceError("MALFORMED_AUTHORIZATION", "invalid member", 403) from None
