"""Verifier: every check, exact precedence, Tier 1/Tier 2 signature facts, bindings, chain, replay."""

import itertools
from dataclasses import replace

import pytest
from c2_world import (
    OTHER_DIGEST,
    PROVIDER_A,
    VERIFIER_BREAKERS,
    VerifierWorld,
    tier1_world,
)

from credence.core import verifier
from credence.core.primitives import (
    VERIFIER_REASON_CODES,
    DecisionStage,
    EvidenceTier,
    MicroTrlusd,
    Nonce,
    ReasonCode,
    Sha256Digest,
)
from credence.core.verifier import ProviderSignatureFact, VerifierInputError, verify

FACT = ProviderSignatureFact


def decide(world: VerifierWorld) -> ReasonCode | None:
    return verify(world.wired()).record.reason_code


def test_controls_accept() -> None:
    assert verify(VerifierWorld().wired()).accepted
    assert verify(tier1_world().wired()).accepted


@pytest.mark.parametrize("code", VERIFIER_REASON_CODES, ids=lambda c: c.value)
def test_each_check_alone(code: ReasonCode) -> None:
    assert decide(VERIFIER_BREAKERS[code](VerifierWorld())) is code


@pytest.mark.parametrize(
    ("first", "second"), list(itertools.pairwise(VERIFIER_REASON_CODES)), ids=lambda c: c.value
)
def test_adjacent_precedence(first: ReasonCode, second: ReasonCode) -> None:
    world = VERIFIER_BREAKERS[second](VERIFIER_BREAKERS[first](VerifierWorld()))
    assert decide(world) is first


def test_first_failure_wins_for_every_combination() -> None:
    for size in range(1, len(VERIFIER_REASON_CODES) + 1):
        for subset in itertools.combinations(VERIFIER_REASON_CODES, size):
            world = VerifierWorld()
            for code in reversed(subset):
                world = VERIFIER_BREAKERS[code](world)
            assert decide(world) is subset[0], subset


# --- signature facts -----------------------------------------------------------------------


@pytest.mark.parametrize("fact", [FACT.INVALID, FACT.NOT_PRESENT])
def test_tier2_requires_a_valid_provider_signature(fact: ProviderSignatureFact) -> None:
    assert decide(replace(VerifierWorld(), provider_signature=fact)) is (
        ReasonCode.RECEIPT_SIG_INVALID
    )


def test_tier1_requires_only_the_gateway_signature() -> None:
    assert decide(replace(tier1_world(), gateway_signature_valid=False)) is (
        ReasonCode.RECEIPT_SIG_INVALID
    )


@pytest.mark.parametrize("fact", [FACT.VALID, FACT.INVALID])
def test_tier1_with_a_provider_signature_fact_is_an_input_error(
    fact: ProviderSignatureFact,
) -> None:
    with pytest.raises(VerifierInputError):
        verify(replace(tier1_world(), provider_signature=fact).wired())


def test_tier1_with_an_attestation_is_an_input_error() -> None:
    with pytest.raises(VerifierInputError):
        verify(
            replace(
                tier1_world(), attestation=VerifierWorld().attestation, wire_attestation=False
            ).wired()
        )


def test_tier2_without_its_attestation_is_an_input_error() -> None:
    with pytest.raises(VerifierInputError):
        verify(replace(VerifierWorld(), attestation=None).wired())


# --- bindings ------------------------------------------------------------------------------


def _receipt(**changes: object) -> VerifierWorld:
    world = VerifierWorld()
    return replace(world, receipt=replace(world.receipt, **changes))


def _attested(**changes: object) -> VerifierWorld:
    world = VerifierWorld()
    assert world.attestation is not None
    return replace(world, attestation=replace(world.attestation, **changes))


@pytest.mark.parametrize(
    "world",
    [
        _receipt(purchase_request_digest=OTHER_DIGEST),
        _receipt(execution_profile_digest=OTHER_DIGEST),
        _receipt(provider_id=PROVIDER_A),
        _receipt(nonce=Nonce("0" * 32)),
        _attested(purchase_request_digest=OTHER_DIGEST),
        _attested(execution_profile_digest=OTHER_DIGEST),
        _attested(response_digest=OTHER_DIGEST),
        _attested(input_token_count=1),
        _attested(output_token_count=2),
        replace(
            VerifierWorld(),
            wire_attestation=False,
            receipt=replace(VerifierWorld().receipt, provider_attestation_digest=OTHER_DIGEST),
        ),
    ],
    ids=[
        "request",
        "profile",
        "provider",
        "nonce",
        "att-request",
        "att-profile",
        "att-response",
        "att-input",
        "att-output",
        "att-digest",
    ],
)
def test_binding_mismatch(world: VerifierWorld) -> None:
    assert decide(world) is ReasonCode.BINDING_MISMATCH


def test_tier_downgrade_against_the_approved_service_class_is_a_binding_mismatch() -> None:
    world = tier1_world()
    tier2_service = replace(world.service_class, evidence_tier=EvidenceTier.BOUND_AND_SIGNED)
    assert decide(replace(world, service_class=tier2_service)) is ReasonCode.BINDING_MISMATCH


# --- tokens, billing, chain, replay --------------------------------------------------------


def test_token_cap_equality_is_allowed() -> None:
    world = VerifierWorld()
    count = world.quote.max_output_tokens
    assert world.attestation is not None
    at_cap = replace(
        world,
        receipt=replace(
            world.receipt, output_token_count=count, price_micro_trlusd=MicroTrlusd(186 + 1229)
        ),
        attestation=replace(world.attestation, output_token_count=count),
    )
    assert decide(at_cap) is None  # ceil(2048 * 600000 / 1e6) = 1229


def test_billed_amount_must_equal_the_formula_exactly() -> None:
    world = VerifierWorld()
    under = replace(world.receipt, price_micro_trlusd=MicroTrlusd(186))
    assert decide(replace(world, receipt=under)) is ReasonCode.BILLED_AMOUNT_MISMATCH


def test_chain_genesis_and_mismatch() -> None:
    first = tier1_world()  # receipt.previous_receipt_digest is None, expected None
    assert decide(first) is None
    assert decide(replace(first, expected_previous_receipt_digest=OTHER_DIGEST)) is (
        ReasonCode.CHAIN_BROKEN
    )
    chained = VerifierWorld()
    assert decide(replace(chained, expected_previous_receipt_digest=None)) is (
        ReasonCode.CHAIN_BROKEN
    )


# --- decision records ----------------------------------------------------------------------


def test_decision_record_shape() -> None:
    inp = VerifierWorld().wired()
    record = verify(inp).record
    assert record.decision_stage is DecisionStage.VERIFIER
    assert record.engine_version == verifier.ENGINE_VERSION
    assert record.decided_at == inp.evaluated_at
    assert inp.provider_attestation is not None
    assert record.input_digests == (
        inp.receipt.digest(),
        inp.request.digest(),
        inp.quote.digest(),
        inp.service_class.digest(),
        inp.execution_profile.digest(),
        inp.delivered_response_digest,
        inp.provider_attestation.digest(),
    )
    assert record.authorized_transaction_digest is None
    assert record.authorized_transaction_digest_scheme is None
    assert len(verify(tier1_world().wired()).record.input_digests) == 6


def test_inconsistent_inputs_raise() -> None:
    inp = VerifierWorld().wired()
    with pytest.raises(VerifierInputError):
        verify(replace(inp, quote=replace(inp.quote, service_class_digest=OTHER_DIGEST)))
    with pytest.raises(VerifierInputError):
        verify(replace(inp, receipt_already_settled=0))  # type: ignore[arg-type]
    with pytest.raises(VerifierInputError):
        verify(replace(inp, delivered_response_digest=OTHER_DIGEST.value))  # type: ignore[arg-type]
    assert isinstance(OTHER_DIGEST, Sha256Digest)
