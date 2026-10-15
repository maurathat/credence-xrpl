"""Inference Checkpoint 1: the gateway over the real Core policy, ledger, signatures and verifier.

Every outcome below comes from the Core engines; the tests only assert what Core returned.
"""

from __future__ import annotations

import dataclasses
import socket
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from inference_world import (
    ATTACKER_KEY,
    CONTROLLED_TIER2,
    FACILITY_TIER1,
    GATEWAY_KEY,
    GENERAL_TIER1,
    PROFILE_TIER1,
    PROFILE_TIER2,
    TIER1,
    TIER1_PROVIDER_KEY,
    TIER2,
    TIER2_PROVIDER_KEY,
    make_gateway,
    make_purchase,
    prompt,
    registry,
    synthetic_digest,
    tier1_provider,
    tier2_provider,
    with_facility,
)

from credence.core.billing import total_cost
from credence.core.objects import ProviderAttestationBody
from credence.core.primitives import (
    DecisionOutcome,
    DecisionStage,
    EvidenceTier,
    ProviderId,
    ReasonCode,
    ReservationStatus,
)
from credence.core.reservation_ledger import ReservationLedger
from credence.core.signatures import KeyRole, sign, verify_signed
from credence.core.verifier import ProviderSignatureFact
from credence.inference import gateway as gateway_module
from credence.inference.fake_providers import (
    FAKE_TIER1_PROVIDER_ID,
    FAKE_TIER2_PROVIDER_ID,
    FakeSigningProvider,
    synthetic_token_count,
)
from credence.inference.gateway import (
    DeliveryRejected,
    DeliveryVerified,
    GatewayOutcome,
    GatewayRefusal,
    PolicyRejected,
    ProviderFailed,
    ProviderFailureKind,
    Refused,
)
from credence.inference.payload import TokenCountSource, payload_digest
from credence.inference.provider import (
    AttestationCapability,
    InferenceProvider,
    ProviderAttestation,
    ProviderExecutionError,
    ProviderRequest,
    ProviderResult,
    ProviderUnavailableError,
)
from credence.inference.receipt_state import InMemorySettledReceipts, ReceiptChain

# --- helpers -----------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Any socket use fails the test: Checkpoint 1 makes no network calls. Also makes the two
    spare database directories some tests use for a second, independent gateway."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    yield


class Tampering:
    """Wraps a fake provider and rewrites its result: a misbehaving provider. Counts calls."""

    def __init__(
        self, inner: InferenceProvider, tamper: Callable[[ProviderResult], ProviderResult]
    ) -> None:
        self._inner = inner
        self._tamper = tamper
        self.calls = 0

    @property
    def provider_id(self) -> ProviderId:
        return self._inner.provider_id

    @property
    def attestation_capability(self) -> AttestationCapability:
        return self._inner.attestation_capability

    def execute(self, request: ProviderRequest) -> ProviderResult:
        self.calls += 1
        return self._tamper(self._inner.execute(request))


class Raising:
    """A provider whose execution raises `error`."""

    def __init__(self, provider_id: ProviderId, error: Exception) -> None:
        self.provider_id = provider_id
        self.attestation_capability = AttestationCapability.NONE
        self._error = error
        self.calls = 0

    def execute(self, request: ProviderRequest) -> ProviderResult:
        self.calls += 1
        raise self._error


def ledger_at(tmp_path: Path) -> ReservationLedger:
    return ReservationLedger(tmp_path / "reservations.sqlite3")


def decision_record_count(tmp_path: Path) -> int:
    with sqlite3.connect(tmp_path / "reservations.sqlite3") as conn:
        return int(conn.execute("SELECT count(*) FROM decision_records").fetchone()[0])


def verified(outcome: GatewayOutcome) -> DeliveryVerified:
    assert isinstance(outcome, DeliveryVerified), outcome
    return outcome


def rejected_by_verifier(outcome: GatewayOutcome) -> ReasonCode:
    assert isinstance(outcome, DeliveryRejected), outcome
    assert outcome.decision.record.decision_stage is DecisionStage.VERIFIER
    assert outcome.decision.record.reason_code is not None
    return outcome.decision.record.reason_code


def rejected_by_policy(outcome: GatewayOutcome) -> ReasonCode:
    assert isinstance(outcome, PolicyRejected), outcome
    assert outcome.decision.record.decision_stage is DecisionStage.POLICY
    assert outcome.decision.record.reason_code is not None
    return outcome.decision.record.reason_code


def resign_attestation(
    result: ProviderResult, body: ProviderAttestationBody, key: object = TIER2_PROVIDER_KEY
) -> ProviderResult:
    assert result.attestation is not None
    return replace(result, attestation=ProviderAttestation(body, sign(body, key)))  # type: ignore[arg-type]


# --- Tier 1 end to end -------------------------------------------------------------------------


def test_tier1_end_to_end(tmp_path: Path) -> None:
    provider = tier1_provider()
    gateway = make_gateway(tmp_path, provider)
    purchase = make_purchase(TIER1)

    outcome = verified(gateway.purchase(purchase))
    evidence = outcome.evidence

    assert provider.calls == 1
    assert outcome.decision.record.outcome is DecisionOutcome.ACCEPT
    assert outcome.policy_decision.record.outcome is DecisionOutcome.ACCEPT
    assert (
        outcome.policy_decision.reservation_amount
        == outcome.reservation.maximum_amount_micro_trlusd
    )
    assert evidence.receipt.evidence_tier is EvidenceTier.WITNESSED
    assert evidence.provider_attestation is None
    assert evidence.provider_signature is None
    assert evidence.receipt.provider_attestation_digest is None
    gateway_check = verify_signed(
        evidence.receipt, evidence.gateway_signature, registry(), role=KeyRole.GATEWAY, subject=None
    )
    assert gateway_check.accepted
    # Provider A-style: precision and quantization NOT ASSERTED, and the C2A general class
    # imposes no constraint on them, so the policy engine accepted the purchase.
    assert PROFILE_TIER1.precision is None and PROFILE_TIER1.quantization is None
    assert GENERAL_TIER1.allowed_precisions is None and GENERAL_TIER1.allowed_quantizations is None
    assert evidence.receipt.response_digest == payload_digest(outcome.response)
    assert evidence.token_count_source is TokenCountSource.PROVIDER_REPORTED
    held = ledger_at(tmp_path).get(outcome.reservation.reservation_id)
    assert held.status is ReservationStatus.HELD  # no settlement in Checkpoint 1


def test_tier1_signature_facts(tmp_path: Path) -> None:
    from credence.core.signatures import verifier_signature_facts

    outcome = verified(make_gateway(tmp_path, tier1_provider()).purchase(make_purchase(TIER1)))
    evidence = outcome.evidence
    facts = verifier_signature_facts(
        evidence.receipt, evidence.gateway_signature, None, None, registry()
    )
    assert facts.gateway_signature_valid
    assert facts.provider_signature is ProviderSignatureFact.NOT_PRESENT


# --- Tier 2 end to end -------------------------------------------------------------------------


def test_tier2_end_to_end(tmp_path: Path) -> None:
    provider = tier2_provider()
    gateway = make_gateway(tmp_path, provider)

    outcome = verified(gateway.purchase(make_purchase(TIER2)))
    evidence = outcome.evidence
    receipt = evidence.receipt
    attestation = evidence.provider_attestation

    assert provider.calls == 1
    assert PROFILE_TIER2.precision in (CONTROLLED_TIER2.allowed_precisions or ())
    assert PROFILE_TIER2.quantization in (CONTROLLED_TIER2.allowed_quantizations or ())
    assert receipt.evidence_tier is EvidenceTier.BOUND_AND_SIGNED
    assert attestation is not None and evidence.provider_signature is not None
    assert receipt.provider_attestation_digest == attestation.digest()
    provider_check = verify_signed(
        attestation,
        evidence.provider_signature,
        registry(),
        role=KeyRole.PROVIDER,
        subject=FAKE_TIER2_PROVIDER_ID.value,
    )
    assert provider_check.accepted
    gateway_check = verify_signed(
        receipt, evidence.gateway_signature, registry(), role=KeyRole.GATEWAY, subject=None
    )
    assert gateway_check.accepted
    assert attestation.purchase_request_digest == receipt.purchase_request_digest
    assert attestation.purchase_request_digest == evidence.request.digest()
    assert attestation.execution_profile_digest == receipt.execution_profile_digest
    assert attestation.execution_profile_digest == PROFILE_TIER2.digest()
    assert attestation.response_digest == receipt.response_digest
    assert attestation.response_digest == payload_digest(outcome.response)
    assert attestation.input_token_count == receipt.input_token_count
    assert attestation.output_token_count == receipt.output_token_count


def test_runs_are_deterministic(tmp_path: Path) -> None:
    first = verified(make_gateway(tmp_path / "a", tier2_provider()).purchase(make_purchase(TIER2)))
    second = verified(make_gateway(tmp_path / "b", tier2_provider()).purchase(make_purchase(TIER2)))
    assert first.evidence == second.evidence
    assert first.decision == second.decision


# --- plaintext boundary and token accounting -----------------------------------------------------


@pytest.mark.parametrize("scenario,provider", [(TIER1, tier1_provider), (TIER2, tier2_provider)])
def test_no_plaintext_in_canonical_evidence(
    tmp_path: Path, scenario: object, provider: Callable[[], InferenceProvider]
) -> None:
    purchase = make_purchase(scenario)  # type: ignore[arg-type]
    outcome = verified(make_gateway(tmp_path, provider()).purchase(purchase))
    evidence = outcome.evidence
    canonical = [
        evidence.request.canonical_bytes(),
        evidence.receipt.canonical_bytes(),
        evidence.gateway_signature.canonical_bytes(),
        outcome.decision.record.canonical_bytes(),
    ]
    if evidence.provider_attestation is not None:
        canonical.append(evidence.provider_attestation.canonical_bytes())
    for data in canonical:
        assert purchase.prompt not in data
        assert outcome.response not in data
    assert evidence.request.prompt_digest == payload_digest(purchase.prompt)


def test_token_counts_are_the_provider_reported_counts(tmp_path: Path) -> None:
    outcome = verified(make_gateway(tmp_path, tier1_provider()).purchase(make_purchase(TIER1)))
    receipt = outcome.evidence.receipt
    assert receipt.input_token_count == synthetic_token_count(prompt(1))
    assert receipt.output_token_count == synthetic_token_count(outcome.response)
    assert outcome.evidence.token_count_source is TokenCountSource.PROVIDER_REPORTED


def test_provider_receives_only_immutable_purchase_objects() -> None:
    fields = {f.name for f in dataclasses.fields(ProviderRequest)}
    assert fields == {"purchase_request", "execution_profile", "quote", "prompt"}
    assert ProviderRequest.__dataclass_params__.frozen  # type: ignore[attr-defined]


# --- prompt refusals (before Core is asked) -------------------------------------------------------


def test_prompt_that_does_not_match_the_signed_digest_is_refused(tmp_path: Path) -> None:
    provider = tier1_provider()
    gateway = make_gateway(tmp_path, provider)
    purchase = make_purchase(TIER1, sent_prompt=b"Synthetic prompt swapped after signing.")

    outcome = gateway.purchase(purchase)

    assert isinstance(outcome, Refused)
    assert outcome.refusal is GatewayRefusal.PROMPT_DIGEST_MISMATCH
    assert provider.calls == 0
    assert not ledger_at(tmp_path).nonce_used(purchase.request.facility_id, purchase.request.nonce)


def test_prompt_that_is_not_utf8_is_refused(tmp_path: Path) -> None:
    provider = tier1_provider()
    outcome = make_gateway(tmp_path, provider).purchase(
        replace(make_purchase(TIER1), prompt=b"\xff\xfe synthetic")
    )
    assert isinstance(outcome, Refused)
    assert outcome.refusal is GatewayRefusal.PROMPT_NOT_UTF8
    assert provider.calls == 0


# --- policy authority: a rejection never reaches the provider ---------------------------------------


@pytest.mark.parametrize("signer", [ATTACKER_KEY, None], ids=["wrong-key", "unsigned"])
def test_a_invalid_agent_signature(tmp_path: Path, signer: object) -> None:
    provider = tier1_provider()
    gateway = make_gateway(tmp_path, provider)
    purchase = make_purchase(TIER1, signer=signer)  # type: ignore[arg-type]

    assert rejected_by_policy(gateway.purchase(purchase)) is ReasonCode.SIG_INVALID
    assert provider.calls == 0
    assert (
        ledger_at(tmp_path).held_amount(FACILITY_TIER1.facility_id, TIER1.quote.valid_until).value
        == 0
    )


def test_b_provider_not_allowed(tmp_path: Path) -> None:
    provider = tier1_provider()
    gateway = make_gateway(tmp_path, provider)
    purchase = with_facility(make_purchase(TIER1), allowed_providers=(FAKE_TIER2_PROVIDER_ID,))

    assert rejected_by_policy(gateway.purchase(purchase)) is ReasonCode.PROVIDER_NOT_ALLOWED
    assert provider.calls == 0


def test_c_tier_too_low(tmp_path: Path) -> None:
    provider = tier1_provider()
    gateway = make_gateway(tmp_path, provider)
    purchase = with_facility(
        make_purchase(TIER1), minimum_evidence_tier=EvidenceTier.BOUND_AND_SIGNED
    )

    assert rejected_by_policy(gateway.purchase(purchase)) is ReasonCode.TIER_TOO_LOW
    assert provider.calls == 0


def test_tier1_provider_under_the_controlled_class_is_profile_not_allowed(tmp_path: Path) -> None:
    """NOT ASSERTED precision never satisfies a constrained class (C2A)."""
    provider = tier1_provider()
    purchase = replace(
        with_facility(
            make_purchase(TIER1), allowed_procurement_classes=(CONTROLLED_TIER2.digest(),)
        ),
        procurement_classes=(CONTROLLED_TIER2,),
    )
    outcome = make_gateway(tmp_path, provider).purchase(purchase)
    assert rejected_by_policy(outcome) is ReasonCode.PROFILE_NOT_ALLOWED
    assert provider.calls == 0


def test_reused_nonce_is_rejected_by_policy(tmp_path: Path) -> None:
    provider = tier1_provider()
    gateway = make_gateway(tmp_path, provider)
    verified(gateway.purchase(make_purchase(TIER1)))
    assert rejected_by_policy(gateway.purchase(make_purchase(TIER1))) is ReasonCode.NONCE_REUSED
    assert provider.calls == 1


# --- verifier authority: attacks on the delivered evidence -------------------------------------------


def test_d_tier2_provider_signature_missing(tmp_path: Path) -> None:
    def strip(result: ProviderResult) -> ProviderResult:
        assert result.attestation is not None
        return replace(result, attestation=replace(result.attestation, signature=None))

    outcome = make_gateway(tmp_path, Tampering(tier2_provider(), strip)).purchase(
        make_purchase(TIER2)
    )
    assert rejected_by_verifier(outcome) is ReasonCode.RECEIPT_SIG_INVALID


@pytest.mark.parametrize(
    "forger", [ATTACKER_KEY, TIER1_PROVIDER_KEY], ids=["unregistered-key", "other-provider-key"]
)
def test_e_tier2_provider_signature_forged(tmp_path: Path, forger: object) -> None:
    def forge(result: ProviderResult) -> ProviderResult:
        assert result.attestation is not None
        return resign_attestation(result, result.attestation.body, forger)

    outcome = make_gateway(tmp_path, Tampering(tier2_provider(), forge)).purchase(
        make_purchase(TIER2)
    )
    assert rejected_by_verifier(outcome) is ReasonCode.RECEIPT_SIG_INVALID


def test_f_response_changed_after_attestation(tmp_path: Path) -> None:
    """The provider attested one response and returned another: attestation and receipt disagree."""

    def swap(result: ProviderResult) -> ProviderResult:
        return replace(result, response=b"Synthetic response altered after attestation.")

    outcome = make_gateway(tmp_path, Tampering(tier2_provider(), swap)).purchase(
        make_purchase(TIER2)
    )
    assert rejected_by_verifier(outcome) is ReasonCode.BINDING_MISMATCH


def test_f_delivered_response_differs_from_receipt(tmp_path: Path) -> None:
    outcome = verified(make_gateway(tmp_path, tier2_provider()).purchase(make_purchase(TIER2)))
    replay_gateway = make_gateway(tmp_path / "a", tier2_provider())  # fresh chain: head is None
    decision = replay_gateway.verify_delivery(outcome.evidence, b"Synthetic swapped response.")
    assert decision.record.reason_code is ReasonCode.RESPONSE_DIGEST_MISMATCH


def test_g_token_count_exceeds_quote(tmp_path: Path) -> None:
    def overcount(result: ProviderResult) -> ProviderResult:
        return replace(result, output_token_count=TIER1.quote.max_output_tokens + 1)

    outcome = make_gateway(tmp_path, Tampering(tier1_provider(), overcount)).purchase(
        make_purchase(TIER1)
    )
    assert rejected_by_verifier(outcome) is ReasonCode.TOKEN_COUNT_EXCEEDS_QUOTE


def test_h_billed_amount_altered(tmp_path: Path) -> None:
    outcome = verified(make_gateway(tmp_path, tier1_provider()).purchase(make_purchase(TIER1)))
    evidence = outcome.evidence
    receipt = evidence.receipt
    assert receipt.price_micro_trlusd == total_cost(
        TIER1.quote, receipt.input_token_count, receipt.output_token_count
    )
    assert "price" not in {f.name for f in dataclasses.fields(ProviderResult)}

    altered = replace(
        receipt, price_micro_trlusd=receipt.price_micro_trlusd + receipt.price_micro_trlusd
    )
    checker = make_gateway(tmp_path / "a")  # fresh chain head (None) matches the genesis receipt

    # A gateway-signed receipt with a wrong price (a gateway billing fault):
    resigned = replace(evidence, receipt=altered, gateway_signature=sign(altered, GATEWAY_KEY))
    decision = checker.verify_delivery(resigned, outcome.response)
    assert decision.record.reason_code is ReasonCode.BILLED_AMOUNT_MISMATCH

    # The same alteration without the gateway's signature:
    unsigned = replace(evidence, receipt=altered)
    decision = checker.verify_delivery(unsigned, outcome.response)
    assert decision.record.reason_code is ReasonCode.RECEIPT_SIG_INVALID


def test_i_provider_serves_a_different_profile(tmp_path: Path) -> None:
    other_profile = replace(PROFILE_TIER2, quantization="q4_0")
    provider = FakeSigningProvider(other_profile, TIER2_PROVIDER_KEY)
    outcome = make_gateway(tmp_path, provider).purchase(make_purchase(TIER2))
    assert rejected_by_verifier(outcome) is ReasonCode.BINDING_MISMATCH


def test_i_attestation_bound_to_another_request(tmp_path: Path) -> None:
    def rebind(result: ProviderResult) -> ProviderResult:
        assert result.attestation is not None
        body = replace(
            result.attestation.body, purchase_request_digest=synthetic_digest("another-request")
        )
        return resign_attestation(result, body)

    outcome = make_gateway(tmp_path, Tampering(tier2_provider(), rebind)).purchase(
        make_purchase(TIER2)
    )
    assert rejected_by_verifier(outcome) is ReasonCode.BINDING_MISMATCH


# --- receipt chaining -------------------------------------------------------------------------------


def test_chain_genesis_then_second_receipt(tmp_path: Path) -> None:
    gateway = make_gateway(tmp_path, tier1_provider())
    first = verified(gateway.purchase(make_purchase(TIER1, 1)))
    second = verified(gateway.purchase(make_purchase(TIER1, 2)))

    assert first.evidence.receipt.previous_receipt_digest is None
    assert second.evidence.receipt.previous_receipt_digest == first.evidence.receipt.digest()
    assert gateway.chain.head(FACILITY_TIER1.facility_id) == second.evidence.receipt.digest()


def test_j_broken_chain_deleted_receipt(tmp_path: Path) -> None:
    """Three receipts; the facility's chain loses #2, so #3 no longer links to the head."""
    gateway = make_gateway(tmp_path, tier1_provider())
    first, _second, third = (
        verified(gateway.purchase(make_purchase(TIER1, index))) for index in (1, 2, 3)
    )
    lost_second = ReceiptChain()
    lost_second.advance(FACILITY_TIER1.facility_id, None, first.evidence.receipt.digest())
    checker = make_gateway(tmp_path / "a", chain=lost_second)

    decision = checker.verify_delivery(third.evidence, third.response)
    assert decision.record.reason_code is ReasonCode.CHAIN_BROKEN


def test_rejected_delivery_does_not_move_the_chain(tmp_path: Path) -> None:
    def overcount(result: ProviderResult) -> ProviderResult:
        return replace(result, output_token_count=TIER1.quote.max_output_tokens + 1)

    gateway = make_gateway(tmp_path, Tampering(tier1_provider(), overcount))
    rejected_by_verifier(gateway.purchase(make_purchase(TIER1)))
    assert gateway.chain.head(FACILITY_TIER1.facility_id) is None


def test_k1_resubmitted_receipt_is_stopped_by_the_chain(tmp_path: Path) -> None:
    """Resubmitting an accepted, settled receipt: Core checks CHAIN_BROKEN before REPLAYED_RECEIPT,
    and the head has moved to this receipt, so Core returns CHAIN_BROKEN."""
    settled = InMemorySettledReceipts()
    gateway = make_gateway(tmp_path, tier1_provider(), settled=settled)
    outcome = verified(gateway.purchase(make_purchase(TIER1)))
    settled.mark_settled(outcome.evidence.receipt.digest())  # C3B stand-in

    decision = gateway.verify_delivery(outcome.evidence, outcome.response)
    assert decision.record.outcome is DecisionOutcome.REJECT
    assert decision.record.reason_code is ReasonCode.CHAIN_BROKEN


def test_k2_replayed_receipt_settled_but_chain_not_advanced(tmp_path: Path) -> None:
    """The settled-receipt fact says settled while the chain head still precedes the receipt."""
    outcome = verified(make_gateway(tmp_path, tier1_provider()).purchase(make_purchase(TIER1)))
    settled = InMemorySettledReceipts()
    settled.mark_settled(outcome.evidence.receipt.digest())  # C3B stand-in
    checker = make_gateway(tmp_path / "a", settled=settled)  # head still None (genesis)

    decision = checker.verify_delivery(outcome.evidence, outcome.response)
    assert decision.record.reason_code is ReasonCode.REPLAYED_RECEIPT


# --- operational provider failures ------------------------------------------------------------------


def assert_operational_failure(
    tmp_path: Path, outcome: GatewayOutcome, kind: ProviderFailureKind
) -> None:
    assert isinstance(outcome, ProviderFailed), outcome
    assert outcome.kind is kind
    held = ledger_at(tmp_path).get(outcome.reservation.reservation_id)
    assert held.status is ReservationStatus.HELD  # not settled, not released: C3B owns that
    assert decision_record_count(tmp_path) == 1  # only the policy decision; none for the failure
    assert outcome.policy_decision.record.outcome is DecisionOutcome.ACCEPT


@pytest.mark.parametrize(
    "error,kind",
    [
        (ProviderUnavailableError("synthetic outage"), ProviderFailureKind.PROVIDER_UNAVAILABLE),
        (ProviderExecutionError("synthetic failure"), ProviderFailureKind.PROVIDER_EXECUTION_ERROR),
        (RuntimeError("synthetic crash"), ProviderFailureKind.PROVIDER_EXECUTION_ERROR),
    ],
)
def test_provider_raises(tmp_path: Path, error: Exception, kind: ProviderFailureKind) -> None:
    gateway = make_gateway(tmp_path, Raising(FAKE_TIER1_PROVIDER_ID, error))
    outcome = gateway.purchase(make_purchase(TIER1))
    assert_operational_failure(tmp_path, outcome, kind)
    assert gateway.chain.head(FACILITY_TIER1.facility_id) is None


def test_provider_not_connected(tmp_path: Path) -> None:
    outcome = make_gateway(tmp_path).purchase(make_purchase(TIER1))
    assert_operational_failure(tmp_path, outcome, ProviderFailureKind.PROVIDER_UNAVAILABLE)


MALFORMED: dict[str, Callable[[ProviderResult], object]] = {
    "not-a-result": lambda r: {"response": r.response},
    "response-not-utf8": lambda r: replace(r, response=b"\xc3\x28"),
    "response-not-bytes": lambda r: replace(r, response="synthetic text"),  # type: ignore[arg-type]
    "no-input-count": lambda r: replace(r, input_token_count=None),
    "no-output-count": lambda r: replace(r, output_token_count=None),
    "negative-count": lambda r: replace(r, output_token_count=-1),
    "bool-count": lambda r: replace(r, input_token_count=True),
    "other-provider-id": lambda r: replace(r, provider_id=FAKE_TIER2_PROVIDER_ID),
    "profile-not-digest": lambda r: replace(r, execution_profile_digest="sha256:00"),  # type: ignore[arg-type]
    "tier1-sends-attestation": lambda r: replace(
        r,
        attestation=ProviderAttestation(
            ProviderAttestationBody(
                synthetic_digest("x"), synthetic_digest("y"), synthetic_digest("z"), 1, 1
            ),
            None,
        ),
    ),
}


@pytest.mark.parametrize("name", sorted(MALFORMED))
def test_malformed_tier1_result(tmp_path: Path, name: str) -> None:
    provider = Tampering(tier1_provider(), MALFORMED[name])  # type: ignore[arg-type]
    outcome = make_gateway(tmp_path, provider).purchase(make_purchase(TIER1))
    assert_operational_failure(tmp_path, outcome, ProviderFailureKind.PROVIDER_MALFORMED_RESPONSE)


def test_malformed_tier2_result_without_attestation(tmp_path: Path) -> None:
    provider = Tampering(tier2_provider(), lambda r: replace(r, attestation=None))
    outcome = make_gateway(tmp_path, provider).purchase(make_purchase(TIER2))
    assert_operational_failure(tmp_path, outcome, ProviderFailureKind.PROVIDER_MALFORMED_RESPONSE)


# --- typed outcomes cannot claim what Core did not decide ------------------------------------------


def test_verified_outcome_requires_core_accept(tmp_path: Path) -> None:
    def overcount(result: ProviderResult) -> ProviderResult:
        return replace(result, output_token_count=TIER1.quote.max_output_tokens + 1)

    outcome = make_gateway(tmp_path, Tampering(tier1_provider(), overcount)).purchase(
        make_purchase(TIER1)
    )
    assert isinstance(outcome, DeliveryRejected)
    with pytest.raises(ValueError):
        DeliveryVerified(
            outcome.reservation, outcome.policy_decision, outcome.evidence, outcome.decision, b"x"
        )


def test_policy_rejected_requires_a_rejection(tmp_path: Path) -> None:
    outcome = verified(make_gateway(tmp_path, tier1_provider()).purchase(make_purchase(TIER1)))
    from credence.core.policy_engine import PolicyDecision

    accept = PolicyDecision(
        record=outcome.decision.record, max_total_cost_value=0, reservation_amount=None
    )
    with pytest.raises(ValueError):
        PolicyRejected(accept)


def test_quote_is_not_in_checkpoint_1() -> None:
    with pytest.raises(NotImplementedError):
        gateway_module.quote()


def test_world_is_wired() -> None:
    assert TIER1.quote.service_class_digest == TIER1.service_class.digest()
    assert TIER2.quote.service_class_digest == TIER2.service_class.digest()
    assert TIER1.facility.allowed_procurement_classes == (GENERAL_TIER1.digest(),)
    assert TIER2.facility.allowed_procurement_classes == (CONTROLLED_TIER2.digest(),)
