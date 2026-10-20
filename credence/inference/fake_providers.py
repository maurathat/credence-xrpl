"""Deterministic in-process fake providers for Checkpoint 1.

Synthetic: no network, no model, no credentials. They exist to drive the real gateway and Core path
reproducibly. Neither stands for a real provider: the Tier 1 fake does not behave like Nebius, and
the Tier 2 fake runs no model and hashes no weights file.

- FakeWitnessedProvider (Tier 1): returns a response and token counts; signs nothing.
- FakeSigningProvider (Tier 2): also builds the credence.provider_attestation for what it says it
  served and signs it through Core C3A (`credence.core.signatures.sign`).

Responses and token counts are fixed functions of the request, so every run is byte-identical. The
token count rule is synthetic (one token per started 4 bytes); it is not any real tokenizer.

Owner: Inference.
"""

from __future__ import annotations

from nacl.signing import SigningKey

from credence.core.objects import ExecutionProfile, ProviderAttestationBody
from credence.core.primitives import ProviderId
from credence.core.signatures import sign
from credence.inference.payload import payload_digest
from credence.inference.provider import (
    AttestationCapability,
    ProviderAttestation,
    ProviderRequest,
    ProviderResult,
)

SYNTHETIC_BYTES_PER_TOKEN = 4

FAKE_TIER1_PROVIDER_ID = ProviderId("synthetic-witnessed-provider")
FAKE_TIER2_PROVIDER_ID = ProviderId("synthetic-signing-provider")


def synthetic_token_count(data: bytes) -> int:
    """ceil(len(data) / 4): the fakes' token rule. Synthetic; not any real tokenizer."""
    return -(-len(data) // SYNTHETIC_BYTES_PER_TOKEN)


def synthetic_response(provider_id: ProviderId, request: ProviderRequest) -> bytes:
    """A fixed response that depends only on the provider and the prompt's length."""
    return (
        f"Synthetic response from {provider_id.value} to a {len(request.prompt)}-byte prompt."
    ).encode()


class FakeWitnessedProvider:
    """Tier 1 fake. Reports the profile it was built with; never signs. `calls` counts executions."""

    attestation_capability = AttestationCapability.NONE

    def __init__(
        self,
        execution_profile: ExecutionProfile,
        provider_id: ProviderId = FAKE_TIER1_PROVIDER_ID,
    ) -> None:
        self._provider_id = provider_id
        self._profile = execution_profile
        self.calls = 0

    @property
    def provider_id(self) -> ProviderId:
        return self._provider_id

    def execute(self, request: ProviderRequest) -> ProviderResult:
        self.calls += 1
        response = synthetic_response(self._provider_id, request)
        return ProviderResult(
            provider_id=self._provider_id,
            execution_profile_digest=self._profile.digest(),
            response=response,
            input_token_count=synthetic_token_count(request.prompt),
            output_token_count=synthetic_token_count(response),
            attestation=None,
        )


class FakeSigningProvider:
    """Tier 2 fake. Attests to the request, its own profile, its response digest and its token
    counts, and signs that attestation with its provider key. `calls` counts executions."""

    attestation_capability = AttestationCapability.SIGNS_ATTESTATION

    def __init__(
        self,
        execution_profile: ExecutionProfile,
        signing_key: SigningKey,
        provider_id: ProviderId = FAKE_TIER2_PROVIDER_ID,
    ) -> None:
        self._provider_id = provider_id
        self._profile = execution_profile
        self._signing_key = signing_key
        self.calls = 0

    @property
    def provider_id(self) -> ProviderId:
        return self._provider_id

    def execute(self, request: ProviderRequest) -> ProviderResult:
        self.calls += 1
        response = synthetic_response(self._provider_id, request)
        body = ProviderAttestationBody(
            purchase_request_digest=request.purchase_request.digest(),
            execution_profile_digest=self._profile.digest(),
            response_digest=payload_digest(response),
            input_token_count=synthetic_token_count(request.prompt),
            output_token_count=synthetic_token_count(response),
        )
        return ProviderResult(
            provider_id=self._provider_id,
            execution_profile_digest=body.execution_profile_digest,
            response=response,
            input_token_count=body.input_token_count,
            output_token_count=body.output_token_count,
            attestation=ProviderAttestation(body=body, signature=sign(body, self._signing_key)),
        )
