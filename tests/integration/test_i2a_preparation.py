"""Integration I2a, CR-1, CR-3 and CR-4: agent-signed purchase preparation and agent-scoped reads.

Adversarial and end-to-end checks of `prepare` / `submit_prepared` on the real IntegrationService:
real signatures, registry, Core policy and accounting; offline providers (fake Nebius transport,
fake signing provider standing in for local Qwen); TEST keys (NOT FOR PRODUCTION); no network,
model or XRPL. Provisional: subject to Integration and security review.
"""

from __future__ import annotations

import hashlib
import socket
import threading
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from i1_world import (
    AGENT_B_KEY,
    CONTROLLED,
    CONTROLLED_FACILITY,
    GENERAL,
    GENERAL_FACILITY,
    OFFERS,
    Clock,
    SimulatedCrash,
    acknowledge,
    count,
    crash_at,
    market_world,
    open_service,
    registry_with_agent_b,
    retrieve,
)
from inference_world import AGENT_KEY, make_test_key, prompt
from market_world import LOCAL_QUOTE, NEBIUS_QUOTE, NEBIUS_SERVICE, MarketWorld
from xrpl.core.addresscodec import encode_classic_address

from credence.core.billing import max_total_cost_value
from credence.core.objects import Facility, ProcurementClass, PurchaseRequest, SignedObject
from credence.core.primitives import (
    EvidenceTier,
    FacilityId,
    MicroTrlusd,
    ProviderId,
    Sha256Digest,
    XrplAccount,
)
from credence.core.signatures import KeyRole, KeyStatus, RegisteredKey, key_id_for, sign
from credence.inference.local_llama import LOCAL_QWEN_PROVIDER_ID
from credence.inference.nebius import NEBIUS_PROVIDER_ID
from credence.inference.payload import payload_digest
from credence.integration.journal import OperationState as S
from credence.integration.preparation import PREPARATION_TTL_SECONDS, Preparation
from credence.integration.service import (
    IntegrationService,
    Market,
    Offer,
    PurchaseRefused,
    PurchaseReport,
    Refusal,
    purchase_requests,
)

A = key_id_for(AGENT_KEY.verify_key)
B = key_id_for(AGENT_B_KEY.verify_key)
# A third registered agent (TEST key, NOT FOR PRODUCTION) with its own borrower and facility.
AGENT_C_KEY = make_test_key("i2a-agent-c")
C = key_id_for(AGENT_C_KEY.verify_key)
C_ACCOUNT = XrplAccount(
    encode_classic_address(hashlib.sha256(b"credence-i2a:agent-c").digest()[:20])
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield


def market(
    facility: Facility = GENERAL_FACILITY,
    procurement: ProcurementClass = GENERAL,
    offers: tuple[Offer, ...] = OFFERS,
) -> Market:
    return Market(facility, (procurement,), offers)


class Env:
    def __init__(self, tmp_path: Path, *, world: MarketWorld | None = None, **kwargs: object):
        self.db = tmp_path / "i2a.sqlite3"
        self.world = world or market_world()
        self.clock = Clock()
        self.registry = registry_with_agent_b(self.world)
        self.register = kwargs.pop("register", (GENERAL_FACILITY, CONTROLLED_FACILITY))
        self.service = self.open(register=self.register, **kwargs)

    def open(self, **kwargs: object) -> IntegrationService:
        return open_service(
            self.db,
            self.world,
            clock=self.clock,
            registry=self.registry,
            **kwargs,  # type: ignore[arg-type]
        )

    def restart(self) -> IntegrationService:
        self.service.close()
        self.service = self.open()
        return self.service

    def provider_calls(self) -> int:
        return len(self.world.nebius_transport.posts) + self.world.local.calls

    def rows(self, table: str) -> int:
        return count(self.db, f"SELECT COUNT(*) FROM {table}")


def prepared(env: Env, m: Market | None = None, index: int = 1, **kwargs: object) -> Preparation:
    result = env.service.prepare(
        m or market(),
        agent_key_id=kwargs.pop("agent", A),  # type: ignore[arg-type]
        prompt_digest=payload_digest(prompt(index)),
        **kwargs,  # type: ignore[arg-type]
    )
    assert isinstance(result, Preparation), result
    return result


def submit(
    env: Env,
    prep: Preparation,
    *,
    m: Market | None = None,
    signature: SignedObject | None = None,
    body: bytes | None = None,
    agent: object = A,
) -> PurchaseReport | PurchaseRefused:
    return env.service.submit_prepared(
        m or market(),
        preparation_id=prep.preparation_id,
        signature=signature or sign(prep.request, AGENT_KEY),
        prompt=prompt(1) if body is None else body,
        agent_key_id=agent,  # type: ignore[arg-type]
    )


def refused(result: object, refusal: Refusal) -> None:
    assert isinstance(result, PurchaseRefused), result
    assert result.refusal is refusal, result


def nothing_happened(env: Env) -> None:
    assert env.rows("integration_operations") == 0
    assert env.rows("reservations") == 0
    assert env.provider_calls() == 0


# --- prepare --------------------------------------------------------------------------------


def test_prepare_selects_under_core_policy_and_builds_the_purchase_request(tmp_path: Path) -> None:
    env = Env(tmp_path)
    prep = prepared(env)
    assert prep.request.provider_id == NEBIUS_PROVIDER_ID  # Scenario A: both qualify, cheaper
    assert prep.offer_index == 0
    assert prep.expires_at == prep.created_at.plus_seconds(PREPARATION_TTL_SECONDS)
    assert prep.request.expires_at == prep.expires_at
    assert prep.request.max_total_cost_micro_trlusd.value == max_total_cost_value(NEBIUS_QUOTE)
    # Exactly the request the shared builder (used by `purchase`) makes for this key and offer.
    rebuilt = purchase_requests(
        prep.idempotency_key,
        GENERAL_FACILITY.facility_id,
        OFFERS,
        payload_digest(prompt(1)),
        prep.expires_at,
    )
    assert rebuilt[0] == prep.request
    assert prep.agent_key_id == A
    nothing_happened(env)
    assert env.rows("integration_preparations") == 1

    b = prepared(env, market(CONTROLLED_FACILITY, CONTROLLED))
    assert b.request.provider_id == LOCAL_QWEN_PROVIDER_ID  # Scenario B: Nebius not allowed
    assert b.offer_index == 1
    nothing_happened(env)


def test_prepare_refuses_unauthorized_and_revoked_agents(tmp_path: Path) -> None:
    env = Env(tmp_path)
    digest = payload_digest(prompt(1))
    # Agent B speaks for another borrower: not authorized on facility A's facility.
    refused(
        env.service.prepare(market(), agent_key_id=B, prompt_digest=digest),
        Refusal.AGENT_NOT_AUTHORIZED,
    )
    gateway_key = next(k for k in env.registry._keys.values() if k.role.value == "gateway")
    refused(
        env.service.prepare(market(), agent_key_id=gateway_key.key_id, prompt_digest=digest),
        Refusal.AGENT_NOT_AUTHORIZED,
    )
    env.registry.revoke(A)
    refused(
        env.service.prepare(market(), agent_key_id=A, prompt_digest=digest),
        Refusal.AGENT_REVOKED,
    )
    assert env.rows("integration_preparations") == 0
    nothing_happened(env)


def test_prepare_refuses_when_no_offer_qualifies(tmp_path: Path) -> None:
    nebius_only = replace(
        CONTROLLED_FACILITY,
        facility_id=FacilityId("facility-i2a-nebius-only"),
        allowed_providers=(NEBIUS_PROVIDER_ID,),
    )
    env = Env(tmp_path, register=(nebius_only,))
    result = env.service.prepare(
        market(nebius_only, CONTROLLED), agent_key_id=A, prompt_digest=payload_digest(prompt(1))
    )
    refused(result, Refusal.NO_QUALIFYING_OFFER)
    assert isinstance(result, PurchaseRefused)
    assert result.detail == "PROFILE_NOT_ALLOWED,PROVIDER_NOT_ALLOWED"
    assert env.rows("integration_preparations") == 0


# --- end to end -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("facility", "procurement", "provider"),
    [
        (GENERAL_FACILITY, GENERAL, NEBIUS_PROVIDER_ID),
        (CONTROLLED_FACILITY, CONTROLLED, LOCAL_QWEN_PROVIDER_ID),
    ],
    ids=["scenario-a", "scenario-b"],
)
def test_prepare_sign_submit_settles_and_the_agent_retrieves_and_acknowledges(
    tmp_path: Path, facility: Facility, procurement: ProcurementClass, provider: ProviderId
) -> None:
    env = Env(tmp_path)
    m = market(facility, procurement)
    prep = prepared(env, m)
    report = submit(env, prep, m=m)
    assert isinstance(report, PurchaseReport)
    assert report.state is S.SETTLED
    assert report.provider_id == provider.value
    assert report.operation_id == prep.operation_id
    assert env.provider_calls() == 1

    op = env.service.journal_entry(report.operation_id)
    assert op.request() == prep.request
    assert op.agent_key_id == A.value
    receipt = op.receipt()
    assert receipt is not None
    assert receipt.purchase_request_digest == prep.request_digest
    settled = env.service.accounting.accounting_settlement(receipt.digest())
    assert settled is not None
    assert settled.reservation_id.value == op.reservation_id
    exposure = env.service.accounting.exposure(facility.facility_id, env.clock.peek())
    assert exposure.drawn == settled.actual_amount
    assert exposure.held == MicroTrlusd(0)
    assert env.service.accounting.chain_head(facility.facility_id) == receipt.digest()
    assert env.service.operation_for_receipt(receipt.digest()).operation_id == op.operation_id

    assert retrieve(env.service, op.operation_id) == op.response
    acknowledge(env.service, op.operation_id)
    assert env.service.operation(op.operation_id).state is S.ACKNOWLEDGED

    events = [e.event for e in env.service.lifecycle_events(op.operation_id)]
    for expected in (
        "intent_journaled",
        "policy_decision",
        "reserved",
        "delivery_attempt_recorded",
        "execution_claimed",
        "provider_started",
        "provider_completed",
        "accounting_settled",
        "retrieved",
        "acknowledged",
        "latest_transition",
    ):
        assert expected in events
    assert "delivered" not in events  # not persisted with a time: never inferred
    times = [e.at.value for e in env.service.lifecycle_events(op.operation_id)]
    assert times == sorted(times)


def test_receipt_chain_and_accounting_stay_consistent_across_purchases(tmp_path: Path) -> None:
    env = Env(tmp_path)
    digests: list[Sha256Digest] = []
    total = 0
    for index in (1, 2, 3):
        prep = prepared(env, index=index)
        report = submit(env, prep, body=prompt(index))
        assert isinstance(report, PurchaseReport) and report.state is S.SETTLED
        receipt = env.service.journal_entry(report.operation_id).receipt()
        assert receipt is not None
        assert receipt.previous_receipt_digest == (digests[-1] if digests else None)
        settled = env.service.accounting.accounting_settlement(receipt.digest())
        assert settled is not None
        total += settled.actual_amount.value
        digests.append(receipt.digest())
    exposure = env.service.accounting.exposure(GENERAL_FACILITY.facility_id, env.clock.peek())
    assert exposure.drawn.value == total
    assert env.service.accounting.chain_head(GENERAL_FACILITY.facility_id) == digests[-1]


# --- tampering and changes after preparation ------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        {"provider_id": LOCAL_QWEN_PROVIDER_ID, "quote_id": LOCAL_QUOTE.quote_id},
        {"max_total_cost_micro_trlusd": MicroTrlusd(1)},
        {"prompt_digest": payload_digest(b"another workload")},
        {"facility_id": CONTROLLED_FACILITY.facility_id},
    ],
    ids=["provider", "price", "workload", "facility"],
)
def test_a_signature_over_a_changed_request_is_refused(
    tmp_path: Path, change: dict[str, object]
) -> None:
    env = Env(tmp_path)
    prep = prepared(env)
    tampered = replace(prep.request, **change)  # type: ignore[arg-type]
    assert isinstance(tampered, PurchaseRequest)
    refused(submit(env, prep, signature=sign(tampered, AGENT_KEY)), Refusal.REQUEST_MISMATCH)
    nothing_happened(env)
    # The untouched preparation still works afterwards.
    assert isinstance(submit(env, prep), PurchaseReport)


def test_a_changed_prompt_after_signing_is_refused(tmp_path: Path) -> None:
    env = Env(tmp_path)
    prep = prepared(env)
    refused(submit(env, prep, body=prompt(2)), Refusal.PROMPT_MISMATCH)
    refused(submit(env, prep, body=b"\xff\xfe"), Refusal.PROMPT_NOT_UTF8)
    nothing_happened(env)


def test_changed_offers_or_evidence_tier_stale_the_preparation(tmp_path: Path) -> None:
    env = Env(tmp_path)
    prep = prepared(env)
    cheaper = replace(NEBIUS_QUOTE, input_price_per_million_micro_trlusd=MicroTrlusd(1))
    lower_tier = replace(NEBIUS_SERVICE, evidence_tier=EvidenceTier.REQUESTED)
    for offers in (
        (Offer(cheaper, OFFERS[0].service_class, OFFERS[0].execution_profile), OFFERS[1]),
        (Offer(NEBIUS_QUOTE, lower_tier, OFFERS[0].execution_profile), OFFERS[1]),
        (OFFERS[1], OFFERS[0]),  # reordered
        (OFFERS[0],),  # an offer withdrawn
    ):
        refused(submit(env, prep, m=market(offers=offers)), Refusal.PREPARATION_STALE)
    changed_terms = replace(GENERAL_FACILITY, spread_bps=GENERAL_FACILITY.spread_bps + 1)
    refused(submit(env, prep, m=market(changed_terms)), Refusal.PREPARATION_STALE)
    refused(submit(env, prep, m=market(CONTROLLED_FACILITY, CONTROLLED)), Refusal.PREPARATION_STALE)
    nothing_happened(env)


def test_a_policy_change_after_preparation_refuses_and_never_substitutes(tmp_path: Path) -> None:
    """Exposure moves after preparation so the prepared offer would now be OVER_LIMIT. The
    service refuses; it does not buy the other offer under the old signature."""
    nebius_max = max_total_cost_value(NEBIUS_QUOTE)
    tight = replace(
        GENERAL_FACILITY,
        facility_id=FacilityId("facility-i2a-tight"),
        limit_micro_trlusd=MicroTrlusd(nebius_max + 1),
    )
    env = Env(tmp_path, register=(tight,))
    m = market(tight)
    first = prepared(env, m, index=1)
    second = prepared(env, m, index=2)
    report = submit(env, first, m=m)
    assert isinstance(report, PurchaseReport) and report.state is S.SETTLED
    calls = env.provider_calls()
    result = submit(env, second, m=m, body=prompt(2))
    refused(result, Refusal.PREPARATION_STALE)
    assert env.provider_calls() == calls
    assert env.rows("integration_operations") == 1


def test_an_expired_preparation_is_refused(tmp_path: Path) -> None:
    env = Env(tmp_path)
    prep = prepared(env)
    env.clock.advance(PREPARATION_TTL_SECONDS + 1)
    refused(submit(env, prep), Refusal.PREPARATION_EXPIRED)
    nothing_happened(env)


def test_wrong_agent_signatures_and_callers_are_refused(tmp_path: Path) -> None:
    env = Env(tmp_path)
    prep = prepared(env)
    # Agent B signs A's prepared request.
    refused(submit(env, prep, signature=sign(prep.request, AGENT_B_KEY)), Refusal.WRONG_AGENT)
    # Agent B submits A's preparation with A's genuine signature.
    refused(submit(env, prep, agent=B), Refusal.WRONG_AGENT)
    # A forged signature carrying A's key ID.
    genuine = sign(prep.request, AGENT_KEY)
    forged = replace(genuine, signature=sign(prep.request, AGENT_B_KEY).signature)
    result = submit(env, prep, signature=forged)
    refused(result, Refusal.BAD_SIGNATURE)
    assert isinstance(result, PurchaseRefused) and result.detail == "BAD_SIGNATURE"
    refused(
        env.service.submit_prepared(
            market(),
            preparation_id="prep-" + "0" * 32,
            signature=genuine,
            prompt=prompt(1),
            agent_key_id=A,
        ),
        Refusal.UNKNOWN_PREPARATION,
    )
    nothing_happened(env)


def test_a_revoked_agent_cannot_submit(tmp_path: Path) -> None:
    env = Env(tmp_path)
    prep = prepared(env)
    signature = sign(prep.request, AGENT_KEY)
    env.registry.revoke(A)
    refused(submit(env, prep, signature=signature), Refusal.AGENT_REVOKED)
    env.restart()  # durable: a restart does not restore the key
    refused(submit(env, prep, signature=signature), Refusal.AGENT_REVOKED)
    nothing_happened(env)


# --- idempotency ----------------------------------------------------------------------------


def test_duplicate_submissions_buy_once(tmp_path: Path) -> None:
    env = Env(tmp_path)
    prep = prepared(env)
    first = submit(env, prep)
    again = submit(env, prep)
    assert isinstance(first, PurchaseReport) and first == again
    assert env.provider_calls() == 1
    # The same preparation with a different body or signature is not a retry.
    refused(submit(env, prep, body=prompt(2)), Refusal.PREPARATION_CONSUMED)
    other_nonce_signature = sign(prep.request, AGENT_B_KEY)
    refused(submit(env, prep, signature=other_nonce_signature), Refusal.PREPARATION_CONSUMED)
    refused(
        env.service.prepare(
            market(),
            agent_key_id=A,
            prompt_digest=payload_digest(prompt(1)),
            idempotency_key=prep.idempotency_key,
        ),
        Refusal.ALREADY_SUBMITTED,
    )
    assert env.rows("integration_operations") == 1


def test_concurrent_submissions_buy_once(tmp_path: Path) -> None:
    env = Env(tmp_path)
    prep = prepared(env)
    results: list[object] = []
    barrier = threading.Barrier(4)

    def run() -> None:
        barrier.wait()
        results.append(submit(env, prep))

    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 4
    assert all(isinstance(r, PurchaseReport) and r.state is S.SETTLED for r in results)
    assert len({r.operation_id for r in results if isinstance(r, PurchaseReport)}) == 1
    assert env.provider_calls() == 1
    assert env.rows("reservations") == 1


def test_resubmission_after_a_restart_is_idempotent(tmp_path: Path) -> None:
    env = Env(tmp_path)
    prep = prepared(env)
    env.restart()  # the preparation is durable
    first = submit(env, prep)
    assert isinstance(first, PurchaseReport) and first.state is S.SETTLED
    drawn = env.service.accounting.exposure(GENERAL_FACILITY.facility_id, env.clock.peek()).drawn
    env.restart()
    again = submit(env, prep)
    assert again == first
    assert env.provider_calls() == 1
    after = env.service.accounting.exposure(GENERAL_FACILITY.facility_id, env.clock.peek()).drawn
    assert after == drawn


def test_a_crash_mid_purchase_then_resubmission_never_executes_again(tmp_path: Path) -> None:
    env = Env(tmp_path)
    prep = prepared(env)
    env.service.close()
    env.service = env.open(crash_hook=crash_at("after_attempt_begun"))
    with pytest.raises(SimulatedCrash):
        submit(env, prep)
    env.service.close()
    env.service = env.open()  # recovery: the attempt is marked unknown
    again = submit(env, prep)
    assert isinstance(again, PurchaseReport)
    assert again.state is S.UNKNOWN
    assert env.provider_calls() == 0


# --- provider failures ----------------------------------------------------------------------


def test_a_provider_error_after_the_attempt_is_unknown_and_never_retried(tmp_path: Path) -> None:
    env = Env(tmp_path, world=market_world(reply={"id": "x", "object": "chat.completion"}))
    prep = prepared(env)
    report = submit(env, prep)
    assert isinstance(report, PurchaseReport)
    assert report.state is S.UNKNOWN
    assert report.detail == "PROVIDER_EXECUTION_ERROR"
    assert submit(env, prep) == report
    assert len(env.world.nebius_transport.posts) == 1
    events = [e.event for e in env.service.lifecycle_events(report.operation_id)]
    assert "execution_claimed" in events and "accounting_settled" not in events


def test_a_disconnected_provider_is_released_with_audit(tmp_path: Path) -> None:
    world = market_world()
    env = Env(tmp_path, world=world, providers=(world.local,))
    prep = prepared(env)  # Nebius is chosen by policy; this process has no Nebius connection
    report = submit(env, prep)
    assert isinstance(report, PurchaseReport)
    assert report.state is S.RELEASED_UNDELIVERED
    events = [e.event for e in env.service.lifecycle_events(report.operation_id)]
    assert "released_undelivered" in events and "delivery_attempt_recorded" not in events


# --- agent-scoped reads ---------------------------------------------------------------------


def test_operations_are_listed_per_agent_with_pagination(tmp_path: Path) -> None:
    c_facility = replace(
        GENERAL_FACILITY, facility_id=FacilityId("facility-i2a-agent-c"), borrower_account=C_ACCOUNT
    )
    env = Env(tmp_path, register=(GENERAL_FACILITY, c_facility))
    env.registry.register(RegisteredKey(C, KeyRole.AGENT, C_ACCOUNT.value, KeyStatus.CURRENT))
    ids = []
    for index in (1, 2, 3):
        report = submit(env, prepared(env, index=index), body=prompt(index))
        assert isinstance(report, PurchaseReport)
        ids.append(report.operation_id)
    m_c = market(c_facility)
    c_prep = prepared(env, m_c, agent=C)
    c_report = submit(env, c_prep, m=m_c, signature=sign(c_prep.request, AGENT_C_KEY), agent=C)
    assert isinstance(c_report, PurchaseReport) and c_report.state is S.SETTLED
    # Agent A cannot prepare on C's facility.
    refused(
        env.service.prepare(m_c, agent_key_id=A, prompt_digest=payload_digest(prompt(1))),
        Refusal.AGENT_NOT_AUTHORIZED,
    )

    page = env.service.operations_for_agent(A, limit=2)
    assert [o.operation_id for o in page] == ids[:2]
    rest = env.service.operations_for_agent(A, limit=2, after=page[-1].operation_id)
    assert [o.operation_id for o in rest] == ids[2:]
    assert [o.operation_id for o in env.service.operations_for_agent(C, limit=10)] == [
        c_report.operation_id
    ]
    # A cursor from another agent's operation is not accepted.
    with pytest.raises(KeyError):
        env.service.operations_for_agent(A, limit=2, after=c_report.operation_id)
