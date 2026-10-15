"""C2 attack suite: the build plan's cases owned by the policy engine, verifier and reservation
ledger, each run through the real code. Plus reason-code coverage and stage ownership.

Every case returns the decision records the real engines produced; nothing here is a canned answer.
"""

import itertools
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from c2_world import (
    CONTROLLED_TIER2,
    GENERAL_TIER1,
    MAX_COST,
    NOW,
    OTHER_DIGEST,
    POLICY_BREAKERS,
    VERIFIER_BREAKERS,
    PolicyWorld,
    VerifierWorld,
    provider_a_policy_world,
    tier1_world,
    world_for_chain,
)

from credence.core.objects import DecisionRecord
from credence.core.policy_engine import evaluate
from credence.core.primitives import (
    POLICY_REASON_CODES,
    VERIFIER_REASON_CODES,
    DecisionStage,
    EvidenceTier,
    ReasonCode,
)
from credence.core.reservation_ledger import ReservationLedger
from credence.core.verifier import ProviderSignatureFact, verify

# Codes that C1A typing makes impossible to produce, with the reason. Nothing else may be missing.
UNREACHABLE_UNDER_C1A = {
    ReasonCode.PURPOSE_MISMATCH: "Purpose has exactly one value, so request and facility agree",
}


def policy(world: PolicyWorld) -> list[DecisionRecord]:
    return [evaluate(world.wired()).record]


def verified(world: VerifierWorld) -> list[DecisionRecord]:
    return [verify(world.wired()).record]


def race_to_limit() -> list[DecisionRecord]:
    path = Path(tempfile.mkdtemp()) / "race.sqlite3"
    ledger = ReservationLedger(path)
    base = PolicyWorld()
    limit = base.facility.drawn_micro_trlusd.value + 2 * MAX_COST - 1
    facility = replace(
        base.facility, limit_micro_trlusd=type(base.facility.limit_micro_trlusd)(limit)
    )
    worlds = [
        replace(
            base,
            facility=facility,
            request=replace(base.request, nonce=type(base.request.nonce)(f"{i:032x}")),
        )
        for i in (1, 2)
    ]
    barrier = threading.Barrier(2)
    records: list[DecisionRecord] = []
    lock = threading.Lock()

    def run(world: PolicyWorld) -> None:
        barrier.wait()
        record = ledger.reserve(world.context(), NOW).decision.record
        with lock:
            records.append(record)

    threads = [threading.Thread(target=run, args=(w,)) for w in worlds]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return records


def deleted_receipt() -> list[DecisionRecord]:
    """Three receipts chained on one facility; the verifier sees #3 after #2 was deleted."""
    first, second, third = world_for_chain(3)
    records = [verify(first.wired()).record, verify(second.wired()).record]
    after_deletion = replace(third, expected_previous_receipt_digest=first.wired().receipt.digest())
    records.append(verify(after_deletion.wired()).record)
    return records


def reordered_receipts() -> list[DecisionRecord]:
    first, second, _ = world_for_chain(3)
    out_of_order = replace(second, expected_previous_receipt_digest=None)  # #2 presented first
    return [verify(out_of_order.wired()).record]


def _policy_with(code: ReasonCode) -> Callable[[], list[DecisionRecord]]:
    return lambda: policy(POLICY_BREAKERS[code](PolicyWorld()))


def _verifier_with(code: ReasonCode) -> Callable[[], list[DecisionRecord]]:
    return lambda: verified(VERIFIER_BREAKERS[code](VerifierWorld()))


def _profile(**changes: object) -> Callable[[], list[DecisionRecord]]:
    return lambda: policy(replace(PolicyWorld(), profile=replace(PolicyWorld().profile, **changes)))


@dataclass(frozen=True)
class Attack:
    name: str
    stopped_by: str
    run: Callable[[], list[DecisionRecord]]
    expected: tuple[ReasonCode | None, ...]


R = ReasonCode
ATTACKS = [
    Attack("valid policy purchase", "control", lambda: policy(PolicyWorld()), (None,)),
    Attack("valid Tier 1 receipt", "control", lambda: verified(tier1_world()), (None,)),
    Attack("valid Tier 2 receipt", "control", lambda: verified(VerifierWorld()), (None,)),
    Attack("invalid request signature", "policy", _policy_with(R.SIG_INVALID), (R.SIG_INVALID,)),
    Attack("revoked working key", "policy", _policy_with(R.KEY_REVOKED), (R.KEY_REVOKED,)),
    Attack(
        "inactive facility", "policy", _policy_with(R.FACILITY_NOT_ACTIVE), (R.FACILITY_NOT_ACTIVE,)
    ),
    Attack(
        "expired facility",
        "policy",
        lambda: policy(
            replace(
                PolicyWorld(),
                evaluated_at=PolicyWorld().facility.maturity_at.plus_seconds(1),
                request=replace(
                    PolicyWorld().request,
                    expires_at=PolicyWorld().facility.maturity_at.plus_seconds(60),
                ),
                quote=replace(
                    PolicyWorld().quote,
                    valid_until=PolicyWorld().facility.maturity_at.plus_seconds(60),
                ),
            )
        ),
        (R.FACILITY_EXPIRED,),
    ),
    Attack("expired request", "policy", _policy_with(R.FACILITY_EXPIRED), (R.FACILITY_EXPIRED,)),
    Attack("expired quote", "policy", _policy_with(R.QUOTE_EXPIRED), (R.QUOTE_EXPIRED,)),
    Attack(
        "wrong provider", "policy", _policy_with(R.PROVIDER_NOT_ALLOWED), (R.PROVIDER_NOT_ALLOWED,)
    ),
    Attack(
        "wrong artifact/profile",
        "policy",
        _policy_with(R.PROFILE_NOT_ALLOWED),
        (R.PROFILE_NOT_ALLOWED,),
    ),
    Attack("wrong precision", "policy", _profile(precision="FP8"), (R.PROFILE_NOT_ALLOWED,)),
    Attack(
        "wrong quantization", "policy", _profile(quantization="Q4_K_M"), (R.PROFILE_NOT_ALLOWED,)
    ),
    Attack("precision not asserted", "policy", _profile(precision=None), (R.PROFILE_NOT_ALLOWED,)),
    Attack(
        "Provider A-style profile, general Tier 1 class",
        "control",
        lambda: policy(provider_a_policy_world((GENERAL_TIER1,))),
        (None,),
    ),
    Attack(
        "Provider A-style profile, controlled Tier 2 class",
        "policy",
        lambda: policy(provider_a_policy_world((CONTROLLED_TIER2,))),
        (R.PROFILE_NOT_ALLOWED,),
    ),
    Attack(
        "unconstrained precision does not admit another model family",
        "policy",
        lambda: policy(
            replace(
                provider_a_policy_world((GENERAL_TIER1,)),
                service_class=replace(
                    provider_a_policy_world((GENERAL_TIER1,)).service_class,
                    model_family="other-model-family",
                ),
            )
        ),
        (R.PROFILE_NOT_ALLOWED,),
    ),
    Attack(
        "unconstrained precision does not lower the tier floor",
        "policy",
        lambda: policy(
            provider_a_policy_world(
                (replace(GENERAL_TIER1, minimum_evidence_tier=EvidenceTier.BOUND_AND_SIGNED),)
            )
        ),
        (R.TIER_TOO_LOW,),
    ),
    Attack("tier too low", "policy", _policy_with(R.TIER_TOO_LOW), (R.TIER_TOO_LOW,)),
    Attack("price above max", "policy", _policy_with(R.PRICE_ABOVE_MAX), (R.PRICE_ABOVE_MAX,)),
    Attack("overspend", "policy", _policy_with(R.OVER_LIMIT), (R.OVER_LIMIT,)),
    Attack("reused nonce", "policy", _policy_with(R.NONCE_REUSED), (R.NONCE_REUSED,)),
    Attack("race to the limit", "reservation ledger", race_to_limit, (None, R.OVER_LIMIT)),
    Attack(
        "bad gateway signature",
        "verifier",
        _verifier_with(R.RECEIPT_SIG_INVALID),
        (R.RECEIPT_SIG_INVALID,),
    ),
    Attack(
        "bad provider signature",
        "verifier",
        lambda: verified(
            replace(VerifierWorld(), provider_signature=ProviderSignatureFact.INVALID)
        ),
        (R.RECEIPT_SIG_INVALID,),
    ),
    Attack(
        "swapped request",
        "verifier",
        lambda: verified(
            replace(
                VerifierWorld(),
                receipt=replace(VerifierWorld().receipt, purchase_request_digest=OTHER_DIGEST),
            )
        ),
        (R.BINDING_MISMATCH,),
    ),
    Attack(
        "swapped profile",
        "verifier",
        lambda: verified(
            replace(
                VerifierWorld(),
                receipt=replace(VerifierWorld().receipt, execution_profile_digest=OTHER_DIGEST),
            )
        ),
        (R.BINDING_MISMATCH,),
    ),
    Attack(
        "swapped response",
        "verifier",
        _verifier_with(R.RESPONSE_DIGEST_MISMATCH),
        (R.RESPONSE_DIGEST_MISMATCH,),
    ),
    Attack(
        "over-billed tokens",
        "verifier",
        _verifier_with(R.TOKEN_COUNT_EXCEEDS_QUOTE),
        (R.TOKEN_COUNT_EXCEEDS_QUOTE,),
    ),
    Attack(
        "over-billed amount",
        "verifier",
        _verifier_with(R.BILLED_AMOUNT_MISMATCH),
        (R.BILLED_AMOUNT_MISMATCH,),
    ),
    Attack("deleted receipt", "verifier", deleted_receipt, (None, None, R.CHAIN_BROKEN)),
    Attack("reordered receipts", "verifier", reordered_receipts, (R.CHAIN_BROKEN,)),
    Attack(
        "replayed receipt", "verifier", _verifier_with(R.REPLAYED_RECEIPT), (R.REPLAYED_RECEIPT,)
    ),
]


@pytest.mark.parametrize("attack", ATTACKS, ids=lambda a: a.name)
def test_attack(attack: Attack) -> None:
    records = attack.run()
    observed = tuple(record.reason_code for record in records)
    if attack.stopped_by == "reservation ledger":
        observed = tuple(sorted(observed, key=lambda c: "" if c is None else c.value))
    assert observed == attack.expected


def test_every_engine_record_stays_inside_its_stage() -> None:
    for attack in ATTACKS:
        for record in attack.run():
            allowed = (
                POLICY_REASON_CODES
                if record.decision_stage is DecisionStage.POLICY
                else VERIFIER_REASON_CODES
            )
            assert record.reason_code is None or record.reason_code in allowed, attack.name


def test_reason_code_coverage_has_no_orphans() -> None:
    produced = {
        record.reason_code for attack in ATTACKS for record in attack.run() if record.reason_code
    }
    owned = set(POLICY_REASON_CODES) | set(VERIFIER_REASON_CODES)
    assert owned == set(ReasonCode)
    assert produced | set(UNREACHABLE_UNDER_C1A) == owned
    assert not produced & set(UNREACHABLE_UNDER_C1A)


def test_stage_partition_is_disjoint_and_ordered() -> None:
    assert not set(POLICY_REASON_CODES) & set(VERIFIER_REASON_CODES)
    assert list(ReasonCode) == [*POLICY_REASON_CODES, *VERIFIER_REASON_CODES]
    assert POLICY_REASON_CODES.index(R.QUOTE_EXPIRED) == (
        POLICY_REASON_CODES.index(R.FACILITY_EXPIRED) + 1
    )
    assert VERIFIER_REASON_CODES.index(R.BILLED_AMOUNT_MISMATCH) == (
        VERIFIER_REASON_CODES.index(R.TOKEN_COUNT_EXCEEDS_QUOTE) + 1
    )
    assert VERIFIER_REASON_CODES.index(R.CHAIN_BROKEN) == (
        VERIFIER_REASON_CODES.index(R.BILLED_AMOUNT_MISMATCH) + 1
    )


def test_engines_cannot_emit_cross_stage_codes() -> None:
    """Every reachable failure combination yields a code from the engine's own stage."""
    for subset in itertools.chain.from_iterable(
        itertools.combinations(POLICY_BREAKERS, n) for n in (1, 2)
    ):
        world = PolicyWorld()
        for code in subset:
            world = POLICY_BREAKERS[code](world)
        record = evaluate(world.wired()).record
        assert record.decision_stage is DecisionStage.POLICY
        assert record.reason_code in POLICY_REASON_CODES
    for subset in itertools.chain.from_iterable(
        itertools.combinations(VERIFIER_BREAKERS, n) for n in (1, 2)
    ):
        vworld = VerifierWorld()
        for code in subset:
            vworld = VERIFIER_BREAKERS[code](vworld)
        record = verify(vworld.wired()).record
        assert record.decision_stage is DecisionStage.VERIFIER
        assert record.reason_code in VERIFIER_REASON_CODES
