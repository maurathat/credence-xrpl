"""Inference gateway.

Tech: Python, x402-style flow carried over from AgentLevy

Responsibility (System architecture table, build plan):
Quotes prices, runs the policy check, forwards the call, collects the receipt

Checkpoint 1 (contracts/INFERENCE-GATEWAY-CONTRACT.md): one in-process purchase path over the real
Core interfaces, against in-process providers. In order:

    prompt digest check  -> C3A policy signature facts -> ReservationLedger.reserve (C2 policy)
    -> provider.execute  -> evidence built by the gateway (response digest, Core billing,
    Tier 2 attestation digest, chain link) -> gateway signs the receipt (C3A)
    -> C3A verifier signature facts -> C2 verifier

The gateway decides nothing that Core decides: policy and verification outcomes come only from
the Core engines, and a delivery is reported verified only when the Core verifier accepts. The
reservation stays held in every outcome; settlement, release and disputes belong to C3B.

Not in Checkpoint 1: quoting (the 402 step), network providers, settlement, persistence.

Owner: Inference.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from typing import NoReturn

from nacl.signing import SigningKey

from credence.core.billing import total_cost
from credence.core.objects import (
    DeliveryReceiptBody,
    ExecutionProfile,
    Facility,
    ProcurementClass,
    ProviderAttestationBody,
    PurchaseRequest,
    Quote,
    Reservation,
    ServiceClass,
    SignedObject,
)
from credence.core.policy_engine import PolicyDecision
from credence.core.primitives import (
    DecisionOutcome,
    DomainError,
    EvidenceTier,
    ProviderId,
    Sha256Digest,
    Timestamp,
)
from credence.core.reservation_ledger import PurchaseContext, ReservationLedger
from credence.core.signatures import (
    KeyRegistry,
    policy_signature_facts,
    sign,
    verifier_signature_facts,
)
from credence.core.verifier import VerifierDecision, VerifierInput, verify
from credence.inference.payload import (
    GATEWAY_TOKEN_COUNT_SOURCE,
    PayloadError,
    TokenCountSource,
    payload_digest,
)
from credence.inference.provider import (
    AttestationCapability,
    InferenceProvider,
    ProviderAttestation,
    ProviderRequest,
    ProviderResult,
    ProviderUnavailableError,
)
from credence.inference.receipt_state import (
    InMemorySettledReceipts,
    ReceiptChain,
    SettledReceipts,
)


def quote(*args: object, **kwargs: object) -> NoReturn:
    raise NotImplementedError("Inference gateway.quote: not in Checkpoint 1 (no 402 quoting yet)")


# --- inputs and evidence -----------------------------------------------------------------------


@dataclass(frozen=True)
class Purchase:
    """One signed purchase request with its prompt and the Core objects it was approved against.

    The prompt is plaintext and transient: it is digested, forwarded to the provider and not kept.
    """

    request: PurchaseRequest
    request_signature: SignedObject | None
    prompt: bytes
    facility: Facility
    quote: Quote
    service_class: ServiceClass
    execution_profile: ExecutionProfile
    procurement_classes: tuple[ProcurementClass, ...]


@dataclass(frozen=True)
class DeliveryEvidence:
    """Everything the Core verifier needs for one delivery. No prompt or response text."""

    receipt: DeliveryReceiptBody
    gateway_signature: SignedObject
    provider_attestation: ProviderAttestationBody | None
    provider_signature: SignedObject | None
    request: PurchaseRequest
    quote: Quote
    service_class: ServiceClass
    execution_profile: ExecutionProfile
    token_count_source: TokenCountSource


# --- outcomes ----------------------------------------------------------------------------------


class GatewayRefusal(Enum):
    """The gateway refused before asking Core anything: no reservation, no provider call."""

    PROMPT_NOT_UTF8 = "PROMPT_NOT_UTF8"
    PROMPT_DIGEST_MISMATCH = "PROMPT_DIGEST_MISMATCH"


class ProviderFailureKind(Enum):
    """An operational provider failure. These are not Core reason codes and get no decision record."""

    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PROVIDER_EXECUTION_ERROR = "PROVIDER_EXECUTION_ERROR"
    PROVIDER_MALFORMED_RESPONSE = "PROVIDER_MALFORMED_RESPONSE"


@dataclass(frozen=True)
class Refused:
    refusal: GatewayRefusal
    detail: str


@dataclass(frozen=True)
class PolicyRejected:
    """The Core policy engine rejected the purchase. No reservation; the provider was not called."""

    decision: PolicyDecision

    def __post_init__(self) -> None:
        if self.decision.record.outcome is not DecisionOutcome.REJECT:
            raise ValueError("PolicyRejected needs a rejecting policy decision")


@dataclass(frozen=True)
class ProviderFailed:
    """The provider failed operationally. The reservation is left held for C3B to release."""

    reservation: Reservation
    policy_decision: PolicyDecision
    kind: ProviderFailureKind
    detail: str

    def __post_init__(self) -> None:
        if self.policy_decision.record.outcome is not DecisionOutcome.ACCEPT:
            raise ValueError(
                "ProviderFailed needs the accepting policy decision behind its reservation"
            )


@dataclass(frozen=True)
class DeliveryRejected:
    """The Core verifier rejected the delivery. The response is withheld; the reservation is held."""

    reservation: Reservation
    policy_decision: PolicyDecision
    evidence: DeliveryEvidence
    decision: VerifierDecision

    def __post_init__(self) -> None:
        if self.policy_decision.record.outcome is not DecisionOutcome.ACCEPT:
            raise ValueError(
                "DeliveryRejected needs the accepting policy decision behind its reservation"
            )
        if self.decision.accepted:
            raise ValueError("DeliveryRejected needs a rejecting verifier decision")


@dataclass(frozen=True)
class DeliveryVerified:
    """The Core verifier accepted the delivery. The reservation is left held for C3B settlement."""

    reservation: Reservation
    policy_decision: PolicyDecision
    evidence: DeliveryEvidence
    decision: VerifierDecision
    response: bytes

    def __post_init__(self) -> None:
        if self.policy_decision.record.outcome is not DecisionOutcome.ACCEPT:
            raise ValueError(
                "DeliveryVerified needs the accepting policy decision behind its reservation"
            )
        if not self.decision.accepted:
            raise ValueError("DeliveryVerified needs an accepting verifier decision")


type GatewayOutcome = (
    Refused | PolicyRejected | ProviderFailed | DeliveryRejected | DeliveryVerified
)


class _MalformedResult(ValueError):
    pass


# --- gateway -----------------------------------------------------------------------------------


class Gateway:
    """The Checkpoint 1 inference gateway, in process.

    ledger: Core reservation ledger (policy runs inside reserve). registry: Core C3A key registry,
    which must hold the gateway's own key under the gateway role. gateway_key: signs receipts.
    providers: connected providers by ID. chain: receipt chain heads. settled: settled-receipt
    fact (read only). clock: evaluation and delivery timestamps.
    """

    def __init__(
        self,
        *,
        ledger: ReservationLedger,
        registry: KeyRegistry,
        gateway_key: SigningKey,
        providers: Iterable[InferenceProvider],
        clock: Callable[[], Timestamp],
        chain: ReceiptChain | None = None,
        settled: SettledReceipts | None = None,
    ) -> None:
        self._ledger = ledger
        self._registry = registry
        self._gateway_key = gateway_key
        self._providers: dict[ProviderId, InferenceProvider] = {}
        for provider in providers:
            if provider.provider_id in self._providers:
                raise ValueError(f"provider {provider.provider_id.value} connected twice")
            self._providers[provider.provider_id] = provider
        self._clock = clock
        self._chain = ReceiptChain() if chain is None else chain
        self._settled: SettledReceipts = InMemorySettledReceipts() if settled is None else settled

    @property
    def chain(self) -> ReceiptChain:
        return self._chain

    def purchase(self, purchase: Purchase) -> GatewayOutcome:
        """Run one purchase end to end. See the module docstring for the order of steps."""
        try:
            prompt_digest = payload_digest(purchase.prompt)
        except PayloadError as exc:
            return Refused(GatewayRefusal.PROMPT_NOT_UTF8, str(exc))
        if prompt_digest != purchase.request.prompt_digest:
            return Refused(
                GatewayRefusal.PROMPT_DIGEST_MISMATCH,
                "the prompt does not hash to the signed request's prompt_digest",
            )

        facts = policy_signature_facts(
            purchase.request, purchase.request_signature, purchase.facility, self._registry
        )
        reserved = self._ledger.reserve(
            PurchaseContext(
                request=purchase.request,
                request_signature_valid=facts.request_signature_valid,
                working_key_current=facts.working_key_current,
                facility=purchase.facility,
                quote=purchase.quote,
                service_class=purchase.service_class,
                execution_profile=purchase.execution_profile,
                procurement_classes=purchase.procurement_classes,
            ),
            self._clock(),
        )
        if reserved.reservation is None:
            return PolicyRejected(reserved.decision)
        reservation, policy = reserved.reservation, reserved.decision

        def failed(kind: ProviderFailureKind, detail: str) -> ProviderFailed:
            return ProviderFailed(reservation, policy, kind, detail)

        provider = self._providers.get(purchase.request.provider_id)
        if provider is None:
            return failed(
                ProviderFailureKind.PROVIDER_UNAVAILABLE,
                f"no provider connected for {purchase.request.provider_id.value}",
            )
        started_at = self._clock()
        try:
            result = provider.execute(
                ProviderRequest(
                    purchase_request=purchase.request,
                    execution_profile=purchase.execution_profile,
                    quote=purchase.quote,
                    prompt=purchase.prompt,
                )
            )
        except ProviderUnavailableError as exc:
            return failed(ProviderFailureKind.PROVIDER_UNAVAILABLE, str(exc))
        except Exception as exc:  # any other failure inside the provider is an execution error
            return failed(
                ProviderFailureKind.PROVIDER_EXECUTION_ERROR, f"{type(exc).__name__}: {exc}"
            )
        completed_at = self._clock()

        try:
            evidence = self._evidence(purchase, provider, result, started_at, completed_at)
        except _MalformedResult as exc:
            return failed(ProviderFailureKind.PROVIDER_MALFORMED_RESPONSE, str(exc))

        decision = self.verify_delivery(evidence, result.response)
        if not decision.accepted:
            return DeliveryRejected(reservation, policy, evidence, decision)
        self._chain.advance(
            purchase.request.facility_id,
            evidence.receipt.previous_receipt_digest,
            evidence.receipt.digest(),
        )
        return DeliveryVerified(reservation, policy, evidence, decision, result.response)

    def verify_delivery(
        self, evidence: DeliveryEvidence, delivered_response: bytes
    ) -> VerifierDecision:
        """C3A verifier signature facts, then the C2 verifier. Does not move the chain head.

        delivered_response is digested here, independently of the receipt's response_digest.
        """
        facts = verifier_signature_facts(
            evidence.receipt,
            evidence.gateway_signature,
            evidence.provider_attestation,
            evidence.provider_signature,
            self._registry,
        )
        return verify(
            VerifierInput(
                receipt=evidence.receipt,
                gateway_signature_valid=facts.gateway_signature_valid,
                provider_signature=facts.provider_signature,
                provider_attestation=evidence.provider_attestation,
                request=evidence.request,
                quote=evidence.quote,
                service_class=evidence.service_class,
                execution_profile=evidence.execution_profile,
                delivered_response_digest=payload_digest(delivered_response),
                expected_previous_receipt_digest=self._chain.head(evidence.request.facility_id),
                receipt_already_settled=self._settled.is_settled(evidence.receipt.digest()),
                evaluated_at=self._clock(),
            )
        )

    def _evidence(
        self,
        purchase: Purchase,
        provider: InferenceProvider,
        result: object,
        started_at: Timestamp,
        completed_at: Timestamp,
    ) -> DeliveryEvidence:
        """Build and sign the receipt from the provider's raw result. Refuses a malformed result."""
        if not isinstance(result, ProviderResult):
            raise _MalformedResult(f"provider returned {type(result).__name__}")
        if result.provider_id != provider.provider_id:
            raise _MalformedResult("result names a different provider than the one called")
        if not isinstance(result.execution_profile_digest, Sha256Digest):
            raise _MalformedResult("execution_profile_digest is not a Sha256Digest")
        try:
            response_digest = payload_digest(result.response)
        except PayloadError as exc:
            raise _MalformedResult(f"response: {exc}") from None
        if result.input_token_count is None or result.output_token_count is None:
            raise _MalformedResult("provider reported no token counts; the gateway counts none")

        attestation = result.attestation
        capability = provider.attestation_capability
        if capability is AttestationCapability.NONE and attestation is not None:
            raise _MalformedResult("a provider without attestation capability sent one")
        if capability is AttestationCapability.SIGNS_ATTESTATION:
            if not isinstance(attestation, ProviderAttestation):
                raise _MalformedResult("an attesting provider sent no attestation")
            if not isinstance(attestation.body, ProviderAttestationBody):
                raise _MalformedResult("attestation body is not a ProviderAttestationBody")
            if attestation.signature is not None and not isinstance(
                attestation.signature, SignedObject
            ):
                raise _MalformedResult("attestation signature is not a SignedObject")

        try:
            price = total_cost(purchase.quote, result.input_token_count, result.output_token_count)
            receipt = DeliveryReceiptBody(
                purchase_request_digest=purchase.request.digest(),
                execution_profile_digest=result.execution_profile_digest,
                provider_id=provider.provider_id,
                response_digest=response_digest,
                input_token_count=result.input_token_count,
                output_token_count=result.output_token_count,
                price_micro_trlusd=price,
                started_at=started_at,
                completed_at=completed_at,
                nonce=purchase.request.nonce,
                evidence_tier=(
                    EvidenceTier.WITNESSED if attestation is None else EvidenceTier.BOUND_AND_SIGNED
                ),
                provider_attestation_digest=(
                    None if attestation is None else attestation.body.digest()
                ),
                previous_receipt_digest=self._chain.head(purchase.request.facility_id),
            )
        except DomainError as exc:
            raise _MalformedResult(f"cannot build a receipt from the result: {exc}") from None

        return DeliveryEvidence(
            receipt=receipt,
            gateway_signature=sign(receipt, self._gateway_key),
            provider_attestation=None if attestation is None else attestation.body,
            provider_signature=None if attestation is None else attestation.signature,
            request=purchase.request,
            quote=purchase.quote,
            service_class=purchase.service_class,
            execution_profile=purchase.execution_profile,
            token_count_source=GATEWAY_TOKEN_COUNT_SOURCE,
        )
