"""Policy engine: every rule, exact precedence, boundaries, input consistency, decision records."""

import ast
import itertools
from dataclasses import replace
from pathlib import Path

import pytest
from c2_world import (
    CONTROLLED_TIER2,
    GENERAL_TIER1,
    MAX_COST,
    NOW,
    POLICY_BREAKERS,
    PROCUREMENT,
    PolicyWorld,
    headroom,
    provider_a_policy_world,
)

from credence.core import policy_engine
from credence.core.policy_engine import PolicyInputError, evaluate
from credence.core.primitives import (
    MAX_SAFE_INTEGER,
    POLICY_REASON_CODES,
    DecisionOutcome,
    DecisionStage,
    DeliveryMode,
    EvidenceTier,
    MicroTrlusd,
    Purpose,
    ReasonCode,
)

REACHABLE = tuple(code for code in POLICY_REASON_CODES if code in POLICY_BREAKERS)


def decide(world: PolicyWorld) -> ReasonCode | None:
    return evaluate(world.wired()).record.reason_code


def test_control_accepts_and_reserves_the_quoted_maximum() -> None:
    decision = evaluate(PolicyWorld().wired())
    assert decision.accepted
    assert decision.reservation_amount == MicroTrlusd(MAX_COST)
    assert decision.max_total_cost_value == MAX_COST


def test_breakers_cover_every_reachable_policy_code() -> None:
    assert set(REACHABLE) == set(POLICY_REASON_CODES) - {ReasonCode.PURPOSE_MISMATCH}


@pytest.mark.parametrize("code", REACHABLE, ids=lambda c: c.value)
def test_each_rule_alone(code: ReasonCode) -> None:
    decision = evaluate(POLICY_BREAKERS[code](PolicyWorld()).wired())
    assert decision.record.reason_code is code
    assert decision.record.outcome is DecisionOutcome.REJECT
    assert decision.reservation_amount is None


@pytest.mark.parametrize(
    ("first", "second"), list(itertools.pairwise(REACHABLE)), ids=lambda c: c.value
)
def test_adjacent_precedence(first: ReasonCode, second: ReasonCode) -> None:
    world = POLICY_BREAKERS[second](POLICY_BREAKERS[first](PolicyWorld()))
    assert decide(world) is first


def test_first_failure_wins_for_every_combination() -> None:
    """All 2**11 subsets of reachable rules: the earliest failing rule is always the answer."""
    for size in range(1, len(REACHABLE) + 1):
        for subset in itertools.combinations(REACHABLE, size):
            world = PolicyWorld()
            for code in reversed(subset):
                world = POLICY_BREAKERS[code](world)
            assert decide(world) is subset[0], subset


# --- boundaries ----------------------------------------------------------------------------


def test_expiry_equality_is_still_valid() -> None:
    world = PolicyWorld()
    at_maturity = world.facility.maturity_at
    edge = replace(
        world,
        request=replace(world.request, expires_at=at_maturity),
        quote=replace(world.quote, valid_until=at_maturity),
        evaluated_at=at_maturity,
    )
    assert decide(edge) is None
    assert decide(replace(edge, evaluated_at=at_maturity.plus_seconds(1))) is (
        ReasonCode.FACILITY_EXPIRED
    )


def test_request_expiry_boundary() -> None:
    world = PolicyWorld()
    assert decide(replace(world, evaluated_at=world.request.expires_at)) is None
    assert decide(replace(world, evaluated_at=world.request.expires_at.plus_seconds(1))) is (
        ReasonCode.FACILITY_EXPIRED
    )


def test_quote_expiry_boundary_and_its_own_code() -> None:
    world = PolicyWorld(quote=replace(PolicyWorld().quote, valid_until=NOW))
    assert decide(world) is None
    assert decide(replace(world, evaluated_at=NOW.plus_seconds(1))) is ReasonCode.QUOTE_EXPIRED


def test_limit_equality_is_allowed() -> None:
    world = PolicyWorld()
    exact = replace(world, currently_reserved=MicroTrlusd(headroom(world) - MAX_COST))
    assert decide(exact) is None
    over = replace(exact, currently_reserved=MicroTrlusd(headroom(world) - MAX_COST + 1))
    assert decide(over) is ReasonCode.OVER_LIMIT


def test_price_equality_is_allowed() -> None:
    world = PolicyWorld()
    exact = replace(
        world, request=replace(world.request, max_total_cost_micro_trlusd=MicroTrlusd(MAX_COST))
    )
    assert decide(exact) is None


def test_unrepresentable_maximum_cost_rejects_on_price_not_an_exception() -> None:
    world = PolicyWorld()
    huge = replace(
        world,
        quote=replace(
            world.quote,
            input_price_per_million_micro_trlusd=MicroTrlusd(MAX_SAFE_INTEGER),
            max_input_tokens=MAX_SAFE_INTEGER,
        ),
    )
    decision = evaluate(huge.wired())
    assert decision.record.reason_code is ReasonCode.PRICE_ABOVE_MAX
    assert decision.max_total_cost_value > MAX_SAFE_INTEGER


# --- profile vs tier -----------------------------------------------------------------------


def test_tier_too_low_is_not_swallowed_by_profile_not_allowed() -> None:
    strict_class = replace(PROCUREMENT, minimum_evidence_tier=EvidenceTier.MEASURED)
    assert decide(PolicyWorld(procurement=(strict_class,))) is ReasonCode.TIER_TOO_LOW


def test_tier_requirement_is_the_stricter_of_facility_and_class() -> None:
    world = PolicyWorld()
    lenient_facility = replace(world.facility, minimum_evidence_tier=EvidenceTier.REQUESTED)
    strict_class = replace(PROCUREMENT, minimum_evidence_tier=EvidenceTier.MEASURED)
    assert decide(replace(world, facility=lenient_facility, procurement=(strict_class,))) is (
        ReasonCode.TIER_TOO_LOW
    )
    tier1 = replace(world.service_class, evidence_tier=EvidenceTier.WITNESSED)
    two = replace(world.facility, minimum_evidence_tier=EvidenceTier.BOUND_AND_SIGNED)
    assert decide(replace(world, service_class=tier1, facility=two)) is ReasonCode.TIER_TOO_LOW
    assert decide(replace(world, service_class=tier1)) is None  # facility and class need Tier 1


def test_any_structurally_matching_class_that_meets_tier_accepts() -> None:
    mismatched = replace(PROCUREMENT, model_family="something-else")
    strict = replace(PROCUREMENT, minimum_evidence_tier=EvidenceTier.MEASURED)
    assert decide(PolicyWorld(procurement=(mismatched, strict, PROCUREMENT))) is None
    assert decide(PolicyWorld(procurement=(mismatched, strict))) is ReasonCode.TIER_TOO_LOW
    assert decide(PolicyWorld(procurement=(mismatched,))) is ReasonCode.PROFILE_NOT_ALLOWED


# --- purpose -------------------------------------------------------------------------------


def test_purpose_mismatch_is_unreachable_under_c1a_typing() -> None:
    """Purpose has one value, so a typed request and facility always agree on `inference`."""
    assert list(Purpose) == [Purpose.INFERENCE]
    world = PolicyWorld()
    assert world.request.purpose is Purpose.INFERENCE
    assert world.facility.purpose is Purpose.INFERENCE
    source = Path(policy_engine.__file__).read_text()
    assert "ReasonCode.PURPOSE_MISMATCH" in source  # the check exists for future vocabularies


# --- input consistency ---------------------------------------------------------------------


def _inconsistent() -> list[tuple[str, object]]:
    base = PolicyWorld().wired()
    return [
        (
            "facility",
            replace(
                base,
                request=replace(
                    base.request, facility_id=type(base.request.facility_id)("facility-other")
                ),
            ),
        ),
        (
            "quote id",
            replace(
                base, quote=replace(base.quote, quote_id=type(base.quote.quote_id)("quote-other"))
            ),
        ),
        (
            "quote provider",
            replace(
                base,
                quote=replace(base.quote, provider_id=type(base.quote.provider_id)("provider-a")),
            ),
        ),
        (
            "service class",
            replace(base, service_class=replace(base.service_class, model_family="x")),
        ),
        (
            "profile",
            replace(base, execution_profile=replace(base.execution_profile, runtime="other")),
        ),
        ("procurement", replace(base, procurement_classes=())),
        ("bool fact", replace(base, nonce_already_used=1)),
        ("reserved", replace(base, currently_reserved=0)),
    ]


@pytest.mark.parametrize(("label", "inp"), _inconsistent(), ids=lambda v: str(v)[:20])
def test_inconsistent_inputs_raise_instead_of_deciding(label: str, inp: object) -> None:
    with pytest.raises(PolicyInputError):
        evaluate(inp)  # type: ignore[arg-type]


# --- decision records ----------------------------------------------------------------------


def test_decision_record_shape() -> None:
    inp = PolicyWorld().wired()
    record = evaluate(inp).record
    assert record.decision_stage is DecisionStage.POLICY
    assert record.engine_version == policy_engine.ENGINE_VERSION
    assert record.decided_at == inp.evaluated_at
    assert record.input_digests == (
        inp.request.digest(),
        inp.facility.digest(),
        inp.quote.digest(),
        inp.service_class.digest(),
        inp.execution_profile.digest(),
        *(pc.digest() for pc in inp.procurement_classes),
    )
    assert record.authorized_transaction_digest is None
    assert record.authorized_transaction_digest_scheme is None


def test_engine_is_deterministic() -> None:
    inp = POLICY_BREAKERS[ReasonCode.OVER_LIMIT](PolicyWorld()).wired()
    first, second = evaluate(inp), evaluate(inp)
    assert first == second
    assert first.record.digest() == second.record.digest()


@pytest.mark.parametrize("module", ["policy_engine", "verifier", "procurement", "billing"])
def test_pure_modules_import_no_clock_storage_or_network(module: str) -> None:
    path = Path(policy_engine.__file__).with_name(f"{module}.py")
    imported = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    forbidden = {"time", "datetime", "sqlite3", "socket", "urllib", "http", "xrpl", "random"}
    assert not imported & forbidden


# --- C2A: Provider A-style profile against general and controlled procurement ----------------


def test_provider_a_style_profile_matches_the_general_tier1_class() -> None:
    world = provider_a_policy_world((GENERAL_TIER1,))
    assert world.profile.precision is None and world.profile.quantization is None
    assert world.profile.model_artifact_digest is None
    assert decide(world) is None


def test_provider_a_style_profile_cannot_match_the_controlled_tier2_class() -> None:
    assert decide(provider_a_policy_world((CONTROLLED_TIER2,))) is ReasonCode.PROFILE_NOT_ALLOWED


def test_qualifying_tier2_local_profile_matches_the_controlled_class() -> None:
    world = PolicyWorld(procurement=(CONTROLLED_TIER2,))
    assert world.profile.precision == "BF16" and world.profile.quantization == "none"
    assert decide(world) is None


def test_facility_with_both_classes_admits_each_provider_through_its_class() -> None:
    both = (GENERAL_TIER1, CONTROLLED_TIER2)
    assert decide(provider_a_policy_world(both)) is None
    assert decide(PolicyWorld(procurement=both)) is None


def test_general_class_still_enforces_tier_and_other_constraints() -> None:
    world = provider_a_policy_world((GENERAL_TIER1,))
    tier2_facility = replace(world.facility, minimum_evidence_tier=EvidenceTier.BOUND_AND_SIGNED)
    assert decide(replace(world, facility=tier2_facility)) is ReasonCode.TIER_TOO_LOW
    other_family = replace(world.service_class, model_family="other-model-family")
    assert decide(replace(world, service_class=other_family)) is ReasonCode.PROFILE_NOT_ALLOWED
    batch = replace(world.service_class, delivery_mode=DeliveryMode.BATCH)
    assert decide(replace(world, service_class=batch)) is ReasonCode.PROFILE_NOT_ALLOWED
    no_jurisdiction = replace(world.service_class, jurisdiction=None)
    assert decide(replace(world, service_class=no_jurisdiction)) is ReasonCode.PROFILE_NOT_ALLOWED
    b_only = replace(world.facility, allowed_providers=(world.facility.allowed_providers[1],))
    assert decide(replace(world, facility=b_only)) is ReasonCode.PROVIDER_NOT_ALLOWED
    class_b_only = replace(GENERAL_TIER1, allowed_providers=(b_only.allowed_providers[0],))
    assert decide(replace(world, procurement=(class_b_only,))) is ReasonCode.PROFILE_NOT_ALLOWED
