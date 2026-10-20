"""The provider boundary: what the gateway hands a provider and what it gets back.

A provider receives immutable Core objects for one approved purchase (the purchase request, the
approved execution profile and the quote) plus the prompt bytes. It never receives the facility,
the reservation, the key registry, the ledger or any decision record. It returns the raw result
of one execution; the gateway builds every piece of Credence evidence from that result itself.

Provider-specific response shapes stop here: an adapter converts its provider's wire format into a
ProviderResult before the gateway sees it.

Owner: Inference.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from credence.core.objects import (
    ExecutionProfile,
    ProviderAttestationBody,
    PurchaseRequest,
    Quote,
    SignedObject,
)
from credence.core.primitives import ProviderId, Sha256Digest


class AttestationCapability(Enum):
    """What evidence a provider can add beyond the gateway's own witness.

    NONE: the provider signs nothing (Tier 1, witnessed). SIGNS_ATTESTATION: the provider signs a
    credence.provider_attestation for every execution (Tier 2, bound and signed).
    """

    NONE = "none"
    SIGNS_ATTESTATION = "signs_attestation"


@dataclass(frozen=True)
class ProviderRequest:
    """One approved purchase, as the provider sees it. The prompt is plaintext and transient."""

    purchase_request: PurchaseRequest
    execution_profile: ExecutionProfile
    quote: Quote
    prompt: bytes


@dataclass(frozen=True)
class ProviderAttestation:
    """A provider's attestation body and its signature wrapper (None if the provider sent none)."""

    body: ProviderAttestationBody
    signature: SignedObject | None


@dataclass(frozen=True)
class ProviderResult:
    """The raw result of one execution.

    response: the response bytes, transient; the gateway digests them and does not store them.
    input_token_count / output_token_count: provider-reported; None when the provider reports none.
    execution_profile_digest: the profile the provider says it served.
    attestation: present only for a SIGNS_ATTESTATION provider.

    Fields are not validated here: the gateway checks the shape and treats a bad one as a
    malformed provider response.
    """

    provider_id: ProviderId
    execution_profile_digest: Sha256Digest
    response: bytes
    input_token_count: int | None
    output_token_count: int | None
    attestation: ProviderAttestation | None


class ProviderUnavailableError(RuntimeError):
    """The provider could not be reached or refused to start the execution."""


class ProviderExecutionError(RuntimeError):
    """The provider started the execution and it failed."""


class InferenceProvider(Protocol):
    """One inference provider. `execute` runs exactly one approved purchase."""

    @property
    def provider_id(self) -> ProviderId: ...

    @property
    def attestation_capability(self) -> AttestationCapability: ...

    def execute(self, request: ProviderRequest) -> ProviderResult: ...
