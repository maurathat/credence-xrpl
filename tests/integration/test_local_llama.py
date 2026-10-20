"""Inference Checkpoint 2: the real local Llama 3.1 8B Instruct provider through the real gateway
and Core engines. Skipped unless CREDENCE_LOCAL_LLAMA=1 (needs the installed model).

Every inference here is real. Network use is limited to loopback: any other connection fails.
"""

from __future__ import annotations

import socket
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from local_llama_world import (
    MAX_OUTPUT_TOKENS,
    PROMPT,
    LocalWorld,
    build_local_world,
    enabled,
    scenario_for,
)
from nacl.signing import SigningKey

pytestmark = pytest.mark.skipif(not enabled(), reason="set CREDENCE_LOCAL_LLAMA=1 to run")

from inference_world import GATEWAY_KEY, make_purchase  # noqa: E402

from credence.core.primitives import (  # noqa: E402
    DecisionOutcome,
    EvidenceTier,
    ReasonCode,
    ReservationStatus,
)
from credence.core.reservation_ledger import ReservationLedger  # noqa: E402
from credence.core.signatures import KeyRole, sign, verify_signed  # noqa: E402
from credence.inference.gateway import (  # noqa: E402
    DeliveryRejected,
    DeliveryVerified,
    GatewayOutcome,
)
from credence.inference.local_llama import LOCAL_PROVIDER_ID  # noqa: E402
from credence.inference.payload import TokenCountSource, payload_digest  # noqa: E402
from credence.inference.provider import (  # noqa: E402
    AttestationCapability,
    ProviderAttestation,
    ProviderRequest,
    ProviderResult,
)

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


@pytest.fixture(autouse=True)
def loopback_only(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    real = socket.create_connection

    def guarded(address: tuple[str, int], *args: object, **kwargs: object) -> socket.socket:
        if address[0] not in LOOPBACK:
            raise AssertionError(f"non-loopback connection attempted: {address[0]}")
        return real(address, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(socket, "create_connection", guarded)
    yield


@pytest.fixture(scope="module")
def world() -> LocalWorld:
    return build_local_world()


class Tampering:
    """Wraps the real provider and rewrites its real result after it has signed."""

    def __init__(self, inner: object, tamper: Callable[[ProviderResult], ProviderResult]) -> None:
        self._inner = inner
        self._tamper = tamper
        self.attestation_capability = AttestationCapability.SIGNS_ATTESTATION
        self.provider_id = LOCAL_PROVIDER_ID

    def execute(self, request: ProviderRequest) -> ProviderResult:
        return self._tamper(self._inner.execute(request))  # type: ignore[attr-defined]


def run(world: LocalWorld, tmp_path: Path, provider: object | None = None) -> GatewayOutcome:
    gateway = world.gateway(tmp_path, provider)  # type: ignore[arg-type]
    return gateway.purchase(make_purchase(world.scenario, request_prompt=PROMPT))


def verifier_reason(outcome: GatewayOutcome) -> ReasonCode:
    assert isinstance(outcome, DeliveryRejected), outcome
    assert outcome.decision.record.reason_code is not None
    return outcome.decision.record.reason_code


def body_altered(**changes: object) -> Callable[[ProviderResult], ProviderResult]:
    """Alter the signed attestation body after signing; the old signature is kept."""

    def tamper(result: ProviderResult) -> ProviderResult:
        assert result.attestation is not None
        body = replace(result.attestation.body, **changes)  # type: ignore[arg-type]
        return replace(result, attestation=replace(result.attestation, body=body))

    return tamper


# --- smoke and full flow ------------------------------------------------------------------------------


def test_binding_describes_the_installed_artifact(world: LocalWorld) -> None:
    profile = world.bound.profile
    assert profile.model_artifact_digest == world.bound.installed.model_digest
    assert profile.chat_template_digest == world.bound.installed.template_digest
    assert profile.tokenizer_digest is None
    assert world.bound.file_type == 1
    assert world.bound.tensor_types <= {"F16", "F32"}
    assert (profile.precision, profile.quantization) == ("F16", "none")


def test_real_inference_smoke(world: LocalWorld) -> None:
    purchase = make_purchase(world.scenario, request_prompt=PROMPT)
    result = world.provider.execute(
        ProviderRequest(purchase.request, purchase.execution_profile, purchase.quote, PROMPT)
    )
    assert result.response.decode("utf-8")
    assert result.input_token_count is not None and result.input_token_count > 0
    assert result.output_token_count is not None
    assert 0 < result.output_token_count <= MAX_OUTPUT_TOKENS
    assert result.attestation is not None and result.attestation.signature is not None
    check = verify_signed(
        result.attestation.body,
        result.attestation.signature,
        world.registry(),
        role=KeyRole.PROVIDER,
        subject=LOCAL_PROVIDER_ID.value,
    )
    assert check.accepted


def test_full_tier2_gateway_flow(world: LocalWorld, tmp_path: Path) -> None:
    outcome = run(world, tmp_path)
    assert isinstance(outcome, DeliveryVerified), outcome
    evidence = outcome.evidence
    assert outcome.decision.record.outcome is DecisionOutcome.ACCEPT
    assert evidence.receipt.evidence_tier is EvidenceTier.BOUND_AND_SIGNED
    assert evidence.receipt.execution_profile_digest == world.bound.profile.digest()
    assert evidence.receipt.response_digest == payload_digest(outcome.response)
    assert evidence.token_count_source is TokenCountSource.PROVIDER_REPORTED
    held = ReservationLedger(tmp_path / "reservations.sqlite3").get(
        outcome.reservation.reservation_id
    )
    assert held.status is ReservationStatus.HELD  # no settlement in this checkpoint


# --- profile and artifact identity ----------------------------------------------------------------------


def test_served_profile_mismatch(world: LocalWorld, tmp_path: Path) -> None:
    other = replace(world.bound.profile, runtime="ollama 0.0.0-other").digest()
    outcome = run(
        world,
        tmp_path,
        Tampering(world.provider, lambda r: replace(r, execution_profile_digest=other)),
    )
    assert verifier_reason(outcome) is ReasonCode.BINDING_MISMATCH


def test_changed_artifact_digest_changes_identity_and_rejects(
    world: LocalWorld, tmp_path: Path
) -> None:
    real_profile = world.bound.profile
    assert real_profile.model_artifact_digest is not None
    flipped = "0" if real_profile.model_artifact_digest.hex[0] != "0" else "1"
    from credence.core.primitives import Sha256Digest

    other_artifact = Sha256Digest.from_hex(flipped + real_profile.model_artifact_digest.hex[1:])
    approved = replace(real_profile, model_artifact_digest=other_artifact)
    assert approved.digest() != real_profile.digest()

    # A purchase approved for the other artifact: the real provider's evidence does not bind.
    approved_scenario = scenario_for(approved)
    gateway = world.gateway(tmp_path)
    outcome = gateway.purchase(make_purchase(approved_scenario, request_prompt=PROMPT))
    assert verifier_reason(outcome) is ReasonCode.BINDING_MISMATCH

    # Evidence produced under the real profile, re-checked against the newly approved chain.
    real = run(world, tmp_path / "a")
    assert isinstance(real, DeliveryVerified)
    swapped = replace(
        real.evidence,
        quote=approved_scenario.quote,
        service_class=approved_scenario.service_class,
        execution_profile=approved,
    )
    checker = world.gateway(tmp_path / "b")
    assert checker.verify_delivery(swapped, real.response).record.reason_code is (
        ReasonCode.BINDING_MISMATCH
    )


@pytest.fixture(autouse=True)
def _dirs(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()


# --- attestation attacks on the real flow ----------------------------------------------------------------


def _strip(result: ProviderResult) -> ProviderResult:
    assert result.attestation is not None
    return replace(result, attestation=replace(result.attestation, signature=None))


def _wrong_key(result: ProviderResult) -> ProviderResult:
    assert result.attestation is not None
    body = result.attestation.body
    return replace(result, attestation=ProviderAttestation(body, sign(body, SigningKey.generate())))


ATTACKS: dict[str, Callable[[ProviderResult], ProviderResult]] = {
    "missing-signature": _strip,
    "wrong-provider-key": _wrong_key,
    "response-digest-altered-after-signing": body_altered(
        response_digest=payload_digest(b"Synthetic other response.")
    ),
    "input-count-altered-after-signing": lambda r: body_altered(
        input_token_count=(r.input_token_count or 0) + 1
    )(r),
    "output-count-altered-after-signing": lambda r: body_altered(
        output_token_count=(r.output_token_count or 0) + 1
    )(r),
    "profile-digest-altered-after-signing": lambda r: body_altered(
        execution_profile_digest=replace(r.attestation.body, input_token_count=0).digest()  # type: ignore[union-attr]
    )(r),
}


@pytest.mark.parametrize("name", sorted(ATTACKS))
def test_attestation_attack(world: LocalWorld, tmp_path: Path, name: str) -> None:
    outcome = run(world, tmp_path, Tampering(world.provider, ATTACKS[name]))
    assert verifier_reason(outcome) is ReasonCode.RECEIPT_SIG_INVALID


@pytest.mark.parametrize(
    "field", ["input_token_count", "output_token_count"], ids=["input", "output"]
)
def test_reported_count_differs_from_signed_count(
    world: LocalWorld, tmp_path: Path, field: str
) -> None:
    def tamper(result: ProviderResult) -> ProviderResult:
        return replace(result, **{field: (getattr(result, field) or 0) + 1})

    outcome = run(world, tmp_path, Tampering(world.provider, tamper))
    assert verifier_reason(outcome) is ReasonCode.BINDING_MISMATCH


def test_response_changed_after_signing(world: LocalWorld, tmp_path: Path) -> None:
    outcome = run(
        world,
        tmp_path,
        Tampering(world.provider, lambda r: replace(r, response=r.response + b" (altered)")),
    )
    assert verifier_reason(outcome) is ReasonCode.BINDING_MISMATCH


# --- gateway and provider signatures are not interchangeable ---------------------------------------------


def test_signature_roles_do_not_substitute(world: LocalWorld, tmp_path: Path) -> None:
    real = run(world, tmp_path)
    assert isinstance(real, DeliveryVerified)
    evidence = real.evidence
    assert evidence.provider_attestation is not None and evidence.provider_signature is not None
    checker = world.gateway(tmp_path / "a")

    def reason(**changes: object) -> ReasonCode | None:
        decision = checker.verify_delivery(replace(evidence, **changes), real.response)  # type: ignore[arg-type]
        return decision.record.reason_code

    assert reason() is None  # control: the untouched evidence verifies
    # The provider's attestation signature in the gateway's place, and the reverse:
    assert reason(gateway_signature=evidence.provider_signature) is ReasonCode.RECEIPT_SIG_INVALID
    assert reason(provider_signature=evidence.gateway_signature) is ReasonCode.RECEIPT_SIG_INVALID
    # The provider key signing the receipt, and the gateway key signing the attestation:
    by_provider = sign(evidence.receipt, world.provider_key)
    assert reason(gateway_signature=by_provider) is ReasonCode.RECEIPT_SIG_INVALID
    by_gateway = sign(evidence.provider_attestation, GATEWAY_KEY)
    assert reason(provider_signature=by_gateway) is ReasonCode.RECEIPT_SIG_INVALID
