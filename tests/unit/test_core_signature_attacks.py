"""Cryptographic attack cases end to end: real Ed25519 signatures -> signature facts -> the real
C2 policy engine and verifier. Complements, and does not replace, the pure-engine attack suite."""

from collections.abc import Callable
from dataclasses import dataclass, replace

import pytest
from c2_world import OTHER_DIGEST, PolicyWorld, VerifierWorld, tier1_world
from c3a_keys import AGENT, AGENT_REVOKED, ATTACKER, GATEWAY, PROVIDER_A, PROVIDER_B, registry
from nacl.signing import SigningKey

from credence.core.objects import ProviderAttestationBody, PurchaseRequest, SignedObject
from credence.core.policy_engine import PolicyInput, evaluate
from credence.core.primitives import MicroTrlusd, ReasonCode
from credence.core.signatures import policy_signature_facts, sign, verifier_signature_facts
from credence.core.verifier import VerifierInput, verify

R = ReasonCode


# --- policy side ---------------------------------------------------------------------------


def run_policy(
    signer: SigningKey, tamper: Callable[[PurchaseRequest], PurchaseRequest] = lambda r: r
) -> ReasonCode | None:
    """Sign the approved request, optionally alter it afterwards, derive facts, run the engine."""
    base = PolicyWorld().wired()
    signed = sign(base.request, signer)
    presented = tamper(base.request)
    facts = policy_signature_facts(presented, signed, base.facility, registry())
    inp: PolicyInput = replace(
        PolicyWorld(request=presented).wired(),
        request_signature_valid=facts.request_signature_valid,
        working_key_current=facts.working_key_current,
    )
    return evaluate(inp).record.reason_code


def test_valid_signed_purchase_is_accepted() -> None:
    assert run_policy(AGENT) is None


def test_forged_request_signature() -> None:
    assert run_policy(ATTACKER) is R.SIG_INVALID


def test_request_signed_by_revoked_key() -> None:
    assert run_policy(AGENT_REVOKED) is R.KEY_REVOKED


@pytest.mark.parametrize(
    "tamper",
    [
        lambda r: replace(r, max_total_cost_micro_trlusd=MicroTrlusd(10**9)),
        lambda r: replace(r, prompt_digest=OTHER_DIGEST),
        lambda r: replace(r, expires_at=r.expires_at.plus_seconds(3600)),
    ],
    ids=["raised-max-cost", "swapped-prompt", "extended-expiry"],
)
def test_request_body_modified_after_signing(
    tamper: Callable[[PurchaseRequest], PurchaseRequest],
) -> None:
    assert run_policy(AGENT, tamper) is R.SIG_INVALID


# --- verifier side -------------------------------------------------------------------------


@dataclass(frozen=True)
class Delivery:
    """A delivery as presented to the verifier, with how each part was signed."""

    inp: VerifierInput
    gateway_signed: SignedObject | None
    attestation: ProviderAttestationBody | None
    provider_signed: SignedObject | None


def honest_tier2() -> Delivery:
    inp = VerifierWorld().wired()
    assert inp.provider_attestation is not None
    return Delivery(
        inp=inp,
        gateway_signed=sign(inp.receipt, GATEWAY),
        attestation=inp.provider_attestation,
        provider_signed=sign(inp.provider_attestation, PROVIDER_B),
    )


def run_verifier(delivery: Delivery) -> ReasonCode | None:
    facts = verifier_signature_facts(
        delivery.inp.receipt,
        delivery.gateway_signed,
        delivery.attestation,
        delivery.provider_signed,
        registry(),
    )
    inp = replace(
        delivery.inp,
        provider_attestation=delivery.attestation,
        gateway_signature_valid=facts.gateway_signature_valid,
        provider_signature=facts.provider_signature,
    )
    return verify(inp).record.reason_code


def test_valid_tier2_delivery_is_accepted() -> None:
    assert run_verifier(honest_tier2()) is None


def test_valid_tier1_delivery_is_accepted() -> None:
    inp = tier1_world().wired()
    delivery = Delivery(inp, sign(inp.receipt, GATEWAY), None, None)
    assert run_verifier(delivery) is None


def test_tier2_without_provider_signature_is_rejected() -> None:
    delivery = replace(honest_tier2(), provider_signed=None)
    assert run_verifier(delivery) is R.RECEIPT_SIG_INVALID


def test_forged_gateway_receipt() -> None:
    honest = honest_tier2()
    assert run_verifier(replace(honest, gateway_signed=sign(honest.inp.receipt, ATTACKER))) is (
        R.RECEIPT_SIG_INVALID
    )


def test_receipt_modified_after_gateway_signing() -> None:
    honest = honest_tier2()
    inflated = replace(
        honest.inp.receipt,
        price_micro_trlusd=honest.inp.receipt.price_micro_trlusd + MicroTrlusd(1_000_000),
    )
    delivery = replace(honest, inp=replace(honest.inp, receipt=inflated))
    assert run_verifier(delivery) is R.RECEIPT_SIG_INVALID


def test_forged_provider_attestation() -> None:
    honest = honest_tier2()
    assert honest.attestation is not None
    forged = replace(honest, provider_signed=sign(honest.attestation, ATTACKER))
    assert run_verifier(forged) is R.RECEIPT_SIG_INVALID


def test_provider_attestation_signed_by_wrong_provider() -> None:
    honest = honest_tier2()
    assert honest.attestation is not None
    wrong = replace(honest, provider_signed=sign(honest.attestation, PROVIDER_A))
    assert run_verifier(wrong) is R.RECEIPT_SIG_INVALID


@pytest.mark.parametrize(
    "change",
    [
        {"response_digest": OTHER_DIGEST},
        {"input_token_count": 4096},
        {"output_token_count": 2048},
    ],
    ids=["response-swapped", "input-tokens-changed", "output-tokens-changed"],
)
def test_attestation_altered_after_provider_signing(change: dict[str, object]) -> None:
    """The attacker rewrites the attestation and the receipt consistently, and even gets the
    gateway to sign the altered receipt; the provider's signature still exposes it."""
    honest = honest_tier2()
    assert honest.attestation is not None
    altered_attestation = replace(honest.attestation, **change)
    altered_receipt = replace(
        honest.inp.receipt,
        provider_attestation_digest=altered_attestation.digest(),
        **change,
    )
    delivery = Delivery(
        inp=replace(honest.inp, receipt=altered_receipt, provider_attestation=altered_attestation),
        gateway_signed=sign(altered_receipt, GATEWAY),
        attestation=altered_attestation,
        provider_signed=honest.provider_signed,
    )
    assert run_verifier(delivery) is R.RECEIPT_SIG_INVALID


def test_attestation_signature_replayed_as_gateway_signature() -> None:
    honest = honest_tier2()
    replayed = replace(honest, gateway_signed=honest.provider_signed)
    assert run_verifier(replayed) is R.RECEIPT_SIG_INVALID


def test_valid_signatures_do_not_override_c2_decisions() -> None:
    """Crypto says who signed; C2 still decides. A correctly signed over-billed receipt fails."""
    honest = honest_tier2()
    over = replace(
        honest.inp.receipt,
        price_micro_trlusd=honest.inp.receipt.price_micro_trlusd + MicroTrlusd(1),
    )
    delivery = replace(
        honest, inp=replace(honest.inp, receipt=over), gateway_signed=sign(over, GATEWAY)
    )
    assert run_verifier(delivery) is R.BILLED_AMOUNT_MISMATCH
