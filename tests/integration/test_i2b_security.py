"""I2b: the accepted security baseline (ed3b4f9) exercised through the I2a HTTP and signing paths.

Every refused or unauthorized request is checked against one invariant: no provider call, no
consumed purchase or authorization nonce, no journal entry or reservation, and no change to any
facility's limit, drawn or held amount. Offline mock providers; agent keys are generated per test;
no network, model or XRPL.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from i1_world import SimulatedCrash, crash_at
from test_i2a_api import (
    B_FACILITY,
    CONTROLLED,
    GENERAL,
    PROMPT,
    Agent,
    Demo,
    error,
    expect,
    no_network,  # noqa: F401
)

from credence.agent.client import ApiRefused
from credence.api.app import create_app
from credence.api.marketplace import AgentDirectory, AgentPrincipal, Marketplace
from credence.api.offline_demo import open_marketplace
from credence.api.offline_world import GATEWAY_TEST_KEY, FacilityConfig, offline_world
from credence.core.objects import PurchaseRequest
from credence.core.primitives import EvidenceTier, FacilityId, KeyId, MicroTrlusd, Timestamp
from credence.core.settlement_coordinator import SettlementRefused
from credence.core.signatures import sign
from credence.integration.authorization import ResponseAction
from credence.integration.revocation import (
    DurableKeyRegistry,
    RevocationNotPersisted,
    revoke_key,
)
from credence.integration.service import IntegrationService, Offer

GENERAL_ID = "facility-market-general"
CONTROLLED_ID = "facility-market-controlled"
FACILITIES = (GENERAL_ID, CONTROLLED_ID, B_FACILITY)


def snapshot(demo: Demo) -> tuple[Any, ...]:
    """Everything a refused request must leave unchanged."""
    now = demo.clock.peek()
    exposures = tuple(
        (e.limit.value, e.drawn.value, e.held.value)
        for e in (demo.service.accounting.exposure(FacilityId(f), now) for f in FACILITIES)
    )
    with sqlite3.connect(f"file:{demo.service.database}?mode=ro", uri=True) as conn:
        counts = tuple(
            int(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
            for t in (
                "used_nonces",
                "integration_authorization_nonces",
                "integration_operations",
                "reservations",
                "delivery_attempts",
                "accounting_settlements",
            )
        )
    return (demo.provider_calls(), exposures, counts)


def prepared_and_signed(
    demo: Demo, agent: Agent, form: dict[str, Any] = GENERAL
) -> tuple[Any, ...]:
    client = demo.client(agent)
    prepared = client.prepare(form, PROMPT)
    signature = client.sign_preparation(prepared, PROMPT, expect(agent, demo))
    return client, prepared, signature


def submit_body(prepared: dict[str, Any], signature: dict[str, Any], prompt: bytes = PROMPT) -> Any:
    return {
        "preparation_id": prepared["preparation_id"],
        "prompt": prompt.decode(),
        "signature": signature,
    }


# --- agent isolation ------------------------------------------------------------------------


def test_an_agent_cannot_read_another_agents_operations_credit_or_receipts(
    tmp_path: Path,
) -> None:
    demo = Demo(tmp_path)
    report = demo.client(demo.a).purchase(GENERAL, PROMPT, expect(demo.a, demo))
    op, digest = report["operation_id"], report["receipt_digest"]
    before = snapshot(demo)
    assert demo.get(demo.b, "/v0/live/operations").json()["operations"] == []
    for path in (
        f"/v0/live/operations/{op}",
        f"/v0/live/operations/{op}/exposure",
        f"/v0/live/operations/{op}/payment",
    ):
        assert error(demo.get(demo.b, path)) == (404, "UNKNOWN_OPERATION")
    assert error(demo.get(demo.b, f"/v0/live/receipts/{digest}")) == (404, "UNKNOWN_RECEIPT")
    b_credit = demo.get(demo.b, "/v0/live/facilities").json()
    assert [f["facility_id"] for f in b_credit] == [B_FACILITY]
    assert b_credit[0]["exposure"]["drawn_micro_trlusd"] == 0
    a_credit = {f["facility_id"] for f in demo.get(demo.a, "/v0/live/facilities").json()}
    assert a_credit == {GENERAL_ID, CONTROLLED_ID}
    with pytest.raises(ApiRefused) as refused:
        demo.client(demo.b).prepare(GENERAL, PROMPT, facility_id=GENERAL_ID)
    assert (refused.value.status, refused.value.code) == (404, "UNKNOWN_FACILITY")
    assert snapshot(demo) == before


# --- bearer tokens --------------------------------------------------------------------------


def test_forged_malformed_and_expired_tokens_are_refused(tmp_path: Path) -> None:
    demo = Demo(tmp_path)
    expired = Agent("agent-expired", demo.a.subject, token_expires_at="2026-10-24T14:00:00Z")
    demo.agents.append(expired.entry())
    demo.service.close()
    demo.open()
    before = snapshot(demo)
    forged = demo.a.token[:-1] + ("A" if demo.a.token[-1] != "A" else "B")
    for header, code in (
        ({"authorization": f"Bearer {forged}"}, "INVALID_SESSION"),
        ({"authorization": "Bearer "}, "INVALID_SESSION"),
        ({"authorization": f"Basic {demo.a.token}"}, "AUTHENTICATION_REQUIRED"),
        ({"authorization": demo.a.token}, "AUTHENTICATION_REQUIRED"),
        ({"x-credence-token": demo.a.token}, "AUTHENTICATION_REQUIRED"),
        ({"authorization": f"Bearer {expired.token}"}, "SESSION_EXPIRED"),
    ):
        for path in ("/v0/live/session", "/v0/live/operations", "/v0/live/facilities"):
            assert error(demo.http.get(path, headers=header)) == (401, code)
    assert snapshot(demo) == before


def test_a_token_expiring_between_prepare_and_submit_cannot_submit(tmp_path: Path) -> None:
    demo = Demo(tmp_path)
    short = Agent("agent-short", demo.a.subject, token_expires_at="2026-10-24T15:01:00Z")
    short.key, short.key_id = demo.a.key, demo.a.key_id  # the same agent key, a short session
    demo.agents.append(short.entry())
    demo.agents = [e for e in demo.agents if e["token_sha256"] != demo.a.entry()["token_sha256"]]
    demo.service.close()
    demo.open()
    client, prepared, signature = prepared_and_signed(demo, short)
    before = snapshot(demo)
    demo.clock.advance(120)
    r = demo.post(short, "/v0/live/purchases/submit", submit_body(prepared, signature))
    assert error(r) == (401, "SESSION_EXPIRED")
    assert snapshot(demo) == before


# --- idempotency across agents --------------------------------------------------------------


def test_the_same_idempotency_key_for_two_agents_never_collides(tmp_path: Path) -> None:
    demo = Demo(tmp_path)
    key = "shared-job-key-0001"
    a, b = demo.client(demo.a), demo.client(demo.b)
    a_report = a.purchase(GENERAL, PROMPT, expect(demo.a, demo), idempotency_key=key)
    b_report = b.purchase(GENERAL, PROMPT, expect(demo.b, demo), idempotency_key=key)
    assert a_report["operation_id"] != b_report["operation_id"]
    assert a_report["state"] == b_report["state"] == "settled"
    assert demo.provider_calls() == 2
    for client, own in ((a, a_report), (b, b_report)):
        with pytest.raises(ApiRefused) as refused:
            client.prepare(GENERAL, PROMPT, idempotency_key=key)
        assert (refused.value.status, refused.value.code) == (409, "ALREADY_SUBMITTED")
        assert refused.value.detail == own["operation_id"]  # never the other agent's
    assert demo.provider_calls() == 2


# --- tampering ------------------------------------------------------------------------------


def test_prepared_request_and_signature_tampering_changes_nothing(tmp_path: Path) -> None:
    demo = Demo(tmp_path)
    client, prepared, signature = prepared_and_signed(demo, demo.a)
    request = PurchaseRequest.from_canonical(prepared["request"]["object"])
    before = snapshot(demo)
    other = "sha256:" + "ab" * 32
    cases: list[tuple[dict[str, Any], int, str]] = [
        ({**signature, "object_digest": other}, 422, "REQUEST_MISMATCH"),
        ({**signature, "key_id": demo.b.key_id.value}, 403, "WRONG_AGENT"),
        ({**signature, "signature": "00" * 64}, 403, "BAD_SIGNATURE"),
        ({**signature, "schema_version": "2"}, 422, "MALFORMED_SIGNATURE"),
        ({**signature, "object_type": "credence.purchase_request"}, 422, "MALFORMED_SIGNATURE"),
    ]
    for change in (
        {"provider_id": type(request.provider_id)("local-qwen-provider")},
        {"max_total_cost_micro_trlusd": MicroTrlusd(request.max_total_cost_micro_trlusd.value + 1)},
        {"facility_id": FacilityId(CONTROLLED_ID)},
        {"nonce": type(request.nonce)("0" * 32)},
        {"expires_at": request.expires_at.plus_seconds(60)},
    ):
        tampered = replace(request, **change)  # type: ignore[arg-type]
        cases.append((sign(tampered, demo.a.key).to_canonical(), 422, "REQUEST_MISMATCH"))
    for bad, status, code in cases:
        r = demo.post(demo.a, "/v0/live/purchases/submit", submit_body(prepared, bad))
        assert error(r) == (status, code), bad
        assert snapshot(demo) == before
    assert client.submit(prepared["preparation_id"], PROMPT, signature)["state"] == "settled"


# --- revocation -----------------------------------------------------------------------------


def test_revocation_after_preparation_before_submission(tmp_path: Path) -> None:
    demo = Demo(tmp_path)
    _, prepared, signature = prepared_and_signed(demo, demo.a)
    before = snapshot(demo)
    demo.registry.revoke(demo.a.key_id)  # the bound, durable workflow while the service is open
    body = submit_body(prepared, signature)
    assert error(demo.post(demo.a, "/v0/live/purchases/submit", body)) == (403, "AGENT_REVOKED")
    assert snapshot(demo) == before
    demo.restart()  # a fresh registry lists the key as current; the revocation persists
    assert error(demo.post(demo.a, "/v0/live/purchases/submit", body)) == (403, "AGENT_REVOKED")
    assert error(demo.get(demo.a, "/v0/live/session")) == (403, "AGENT_REVOKED")
    # Agent B is unaffected.
    assert demo.client(demo.b).purchase(GENERAL, PROMPT, expect(demo.b, demo))["state"] == (
        "settled"
    )


def test_closed_service_and_offline_administrative_revocation(tmp_path: Path) -> None:
    demo = Demo(tmp_path)
    _, prepared, signature = prepared_and_signed(demo, demo.a)
    demo.service.close()
    # Revoking through the previously bound registry after close never reports false success.
    with pytest.raises(RevocationNotPersisted):
        demo.registry.revoke(demo.a.key_id)
    demo.open()  # nothing was persisted: the key is still current
    assert demo.get(demo.a, "/v0/live/session").status_code == 200
    demo.service.close()
    # Emergency revocation with no service running: the trusted offline operation.
    record = revoke_key(demo.db, demo.a.key_id, "i2b-test-operator")
    assert record.key_id == demo.a.key_id
    demo.open()  # a fresh registry declares the key current; the persisted revocation wins
    before = snapshot(demo)
    body = submit_body(prepared, signature)
    assert error(demo.post(demo.a, "/v0/live/purchases/submit", body)) == (403, "AGENT_REVOKED")
    assert snapshot(demo) == before
    assert demo.get(demo.b, "/v0/live/session").status_code == 200


def test_lookup_does_no_database_io_on_the_http_purchase_and_response_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    demo = Demo(tmp_path)
    inside = threading.local()
    lookups: list[int] = []
    io_inside_lookup: list[str] = []
    real_lookup = DurableKeyRegistry.lookup
    real_connect = sqlite3.connect

    def lookup(self: DurableKeyRegistry, key_id: KeyId) -> Any:
        inside.active = True
        lookups.append(1)
        try:
            return real_lookup(self, key_id)
        finally:
            inside.active = False

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        if getattr(inside, "active", False):
            io_inside_lookup.append(str(args[0]))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(DurableKeyRegistry, "lookup", lookup)
    monkeypatch.setattr(sqlite3, "connect", connect)
    client = demo.client(demo.a)
    report = client.purchase(CONTROLLED, PROMPT, expect(demo.a, demo))
    assert report["state"] == "settled"
    client.retrieve(report["operation_id"])
    client.acknowledge(report["operation_id"])
    demo.get(demo.a, f"/v0/live/receipts/{report['receipt_digest']}")
    assert len(lookups) > 10
    assert io_inside_lookup == []


# --- changed market or terms between prepare and submit -------------------------------------


class ReconfiguredDemo(Demo):
    """Restart the same database with the operator's configuration changed."""

    def reopen_with(self, change: Any) -> None:
        self.service.close()
        world = offline_world(nebius_reply=self.nebius_reply, extra_facilities=(self.b_config,))
        change(world)
        self.world = world
        self.registry = world.registry(
            {KeyId(e["agent_key_id"]): e["subject"] for e in self.agents}
        )
        self.service, marketplace, _ = open_marketplace(
            self.db, self.agents, world=world, clock=self.clock, registry=self.registry
        )
        self.http = TestClient(create_app(marketplace=marketplace))


def _cheaper_nebius(world: Any) -> None:
    nebius, local = world.offers
    quote = replace(nebius.quote, input_price_per_million_micro_trlusd=MicroTrlusd(1))
    world.offers = (Offer(quote, nebius.service_class, nebius.execution_profile), local)


def _lower_tier(world: Any) -> None:
    nebius, local = world.offers
    service = replace(nebius.service_class, evidence_tier=EvidenceTier.REQUESTED)
    world.offers = (Offer(nebius.quote, service, nebius.execution_profile), local)


def _reordered(world: Any) -> None:
    world.offers = tuple(reversed(world.offers))


def _withdrawn_nebius(world: Any) -> None:
    world.offers = world.offers[1:]


def _changed_terms(world: Any) -> None:
    config = world.facilities[GENERAL_ID]
    facility = replace(config.facility, spread_bps=config.facility.spread_bps + 1)
    world.facilities[GENERAL_ID] = FacilityConfig(
        facility, config.procurement_classes, config.title
    )


@pytest.mark.parametrize(
    "change",
    [_cheaper_nebius, _lower_tier, _reordered, _withdrawn_nebius, _changed_terms],
    ids=["price", "evidence-tier", "offer-order", "provider-withdrawn", "facility-terms"],
)
def test_a_changed_market_or_facility_stales_the_preparation(tmp_path: Path, change: Any) -> None:
    demo = ReconfiguredDemo(tmp_path)
    _, prepared, signature = prepared_and_signed(demo, demo.a)
    demo.reopen_with(change)
    before = snapshot(demo)
    r = demo.post(demo.a, "/v0/live/purchases/submit", submit_body(prepared, signature))
    assert error(r) == (409, "PREPARATION_STALE")
    assert snapshot(demo) == before


def test_terms_inconsistent_with_trusted_configuration_are_refused(tmp_path: Path) -> None:
    demo = ReconfiguredDemo(tmp_path)
    base = {
        k: v
        for k, v in GENERAL.items()
        if k not in ("prompt", "facility_limit_micro_trlusd", "facility_drawn_micro_trlusd")
    }
    body = {**base, "prompt_digest": "sha256:" + "0" * 64}
    before = snapshot(demo)
    for extra in (
        {"facility_limit_micro_trlusd": 10**12},
        {"facility_drawn_micro_trlusd": 0},
        {"facility": {"limit_micro_trlusd": 10**12}},
        {"facility_terms": {"spread_bps": 0}},
    ):
        r = demo.post(demo.a, "/v0/live/purchases/prepare", {**body, **extra})
        assert r.status_code == 422, extra
    _, prepared, signature = prepared_and_signed(demo, demo.a)
    for extra in ({"facility_limit_micro_trlusd": 1}, {"market": {}}):
        r = demo.post(
            demo.a, "/v0/live/purchases/submit", {**submit_body(prepared, signature), **extra}
        )
        assert r.status_code == 422
    assert snapshot(demo)[0] == before[0]

    # The operator's configured limit no longer matches Core's stored limit.
    def raised_limit(world: Any) -> None:
        config = world.facilities[GENERAL_ID]
        facility = replace(config.facility, limit_micro_trlusd=MicroTrlusd(10**12))
        world.facilities[GENERAL_ID] = FacilityConfig(
            facility, config.procurement_classes, config.title
        )

    demo.reopen_with(raised_limit)
    before = snapshot(demo)
    with pytest.raises(ApiRefused) as refused:
        demo.client(demo.a).prepare(GENERAL, PROMPT)
    assert (refused.value.status, refused.value.code) == (422, "FACILITY_LIMIT_MISMATCH")
    r = demo.post(demo.a, "/v0/live/purchases/submit", submit_body(prepared, signature))
    assert error(r) == (409, "PREPARATION_STALE")
    assert snapshot(demo) == before


# --- concurrency, restart and re-execution --------------------------------------------------


def test_concurrent_http_submissions_of_one_preparation_buy_once(tmp_path: Path) -> None:
    demo = Demo(tmp_path)
    _, prepared, signature = prepared_and_signed(demo, demo.a)
    app = demo.http.app
    body = submit_body(prepared, signature)
    results: list[tuple[int, Any]] = []
    barrier = threading.Barrier(6)

    def run() -> None:
        with TestClient(app) as http:
            barrier.wait()
            r = http.post(
                "/v0/live/purchases/submit",
                json=body,
                headers={"authorization": f"Bearer {demo.a.token}"},
            )
            results.append((r.status_code, r.json()))

    threads = [threading.Thread(target=run) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert [status for status, _ in results] == [200] * 6
    assert len({json["operation_id"] for _, json in results}) == 1
    assert {json["state"] for _, json in results} == {"settled"}
    assert demo.provider_calls() == 1
    counts = snapshot(demo)[2]
    assert counts[3] == 1 and counts[5] == 1  # one reservation, one accounting settlement


def test_a_crash_mid_purchase_then_restart_and_identical_submission_never_reexecutes(
    tmp_path: Path,
) -> None:
    demo = Demo(tmp_path)
    _, prepared, signature = prepared_and_signed(demo, demo.a)
    demo.service.close()
    world = offline_world(extra_facilities=(demo.b_config,))
    registry = world.registry({KeyId(e["agent_key_id"]): e["subject"] for e in demo.agents})
    crashing = IntegrationService(
        demo.db,
        registry=registry,
        gateway_key=GATEWAY_TEST_KEY,
        providers=world.providers(),
        clock=demo.clock,
        crash_hook=crash_at("after_attempt_begun"),
    )
    marketplace = Marketplace(
        crashing,
        facilities=world.facilities,
        offers=world.offers,
        directory=AgentDirectory(
            {
                e["token_sha256"]: AgentPrincipal(
                    e["agent_id"], KeyId(e["agent_key_id"]), Timestamp(e["token_expires_at"])
                )
                for e in demo.agents
            }
        ),
        clock=demo.clock,
    )
    with pytest.raises(SimulatedCrash):
        TestClient(create_app(marketplace=marketplace)).post(
            "/v0/live/purchases/submit",
            json=submit_body(prepared, signature),
            headers={"authorization": f"Bearer {demo.a.token}"},
        )
    crashing.close()
    demo.open()  # recovery: Core's attempt is marked unknown, never retried
    r = demo.post(demo.a, "/v0/live/purchases/submit", submit_body(prepared, signature))
    assert r.status_code == 200 and r.json()["state"] == "unknown"
    assert demo.provider_calls() == 0
    assert (
        demo.get(demo.a, f"/v0/live/operations/{r.json()['operation_id']}/payment").json()["xrpl"][
            "state"
        ]
        == "not_submitted"
    )


# --- response retrieval and acknowledgment --------------------------------------------------


def test_unauthorized_retrieval_and_acknowledgment_consume_nothing(tmp_path: Path) -> None:
    demo = Demo(tmp_path)
    a, b = demo.client(demo.a), demo.client(demo.b)
    op = a.purchase(GENERAL, PROMPT, expect(demo.a, demo))["operation_id"]
    b_op = b.purchase(GENERAL, PROMPT, expect(demo.b, demo))["operation_id"]
    path = f"/v0/live/operations/{op}/response"
    good = a.authorization(op, ResponseAction.RETRIEVE)
    before = snapshot(demo)
    expired = a.authorization(op, ResponseAction.RETRIEVE)
    cases: list[tuple[Agent | None, dict[str, Any], tuple[int, str]]] = [
        (None, good, (401, "AUTHENTICATION_REQUIRED")),
        (demo.b, good, (404, "UNKNOWN_OPERATION")),
        (demo.b, b.authorization(op, ResponseAction.RETRIEVE), (404, "UNKNOWN_OPERATION")),
        (demo.a, b.authorization(op, ResponseAction.RETRIEVE), (403, "WRONG_AGENT")),
        (demo.a, a.authorization(b_op, ResponseAction.RETRIEVE), (403, "WRONG_OPERATION")),
        (demo.a, {**good, "signature": "00" * 64}, (403, "BAD_SIGNATURE")),
        (demo.a, {**good, "action": "acknowledge"}, (403, "BAD_SIGNATURE")),
        (demo.a, {**good, "nonce": "xyz"}, (403, "MALFORMED_NONCE")),
        (demo.a, a.authorization(op, ResponseAction.ACKNOWLEDGE), (409, "RESPONSE_UNAVAILABLE")),
    ]
    for agent, body, expected in cases:
        assert error(demo.post(agent, path, body)) == expected, body
        assert snapshot(demo) == before
    demo.clock.advance(200)  # past the 120-second authorization lifetime
    assert error(demo.post(demo.a, path, expired)) == (403, "AUTHORIZATION_EXPIRED")
    assert snapshot(demo)[2] == before[2]
    fresh = a.authorization(op, ResponseAction.RETRIEVE)
    assert demo.post(demo.a, path, fresh).status_code == 200
    assert error(demo.post(demo.a, path, fresh)) == (403, "NONCE_REUSED")
    assert (
        demo.post(demo.a, path, a.authorization(op, ResponseAction.ACKNOWLEDGE)).status_code == 204
    )
    assert error(demo.post(demo.a, path, a.authorization(op, ResponseAction.RETRIEVE))) == (
        409,
        "RESPONSE_UNAVAILABLE",
    )


def test_retrieval_racing_acknowledgment_over_http_is_atomic(tmp_path: Path) -> None:
    for attempt in range(5):
        root = tmp_path / f"race-{attempt}"
        root.mkdir(mode=0o700)  # the trusted-directory rule refuses to create a parent
        demo = Demo(root)
        a = demo.client(demo.a)
        op = a.purchase(GENERAL, PROMPT, expect(demo.a, demo))["operation_id"]
        a.retrieve(op)  # acknowledgment requires a completed retrieval
        path = f"/v0/live/operations/{op}/response"
        bodies = [
            a.authorization(op, ResponseAction.RETRIEVE),
            a.authorization(op, ResponseAction.ACKNOWLEDGE),
        ]
        out: dict[str, int] = {}
        barrier = threading.Barrier(2)

        def run(
            body: dict[str, Any],
            demo: Demo = demo,
            path: str = path,
            out: dict[str, int] = out,
            barrier: threading.Barrier = barrier,
        ) -> None:
            with TestClient(demo.http.app) as http:
                barrier.wait()
                headers = {"authorization": f"Bearer {demo.a.token}"}
                out[body["action"]] = http.post(path, json=body, headers=headers).status_code

        threads = [threading.Thread(target=run, args=(b,)) for b in bodies]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert out["acknowledge"] == 204
        assert out["retrieve"] in (200, 409)
        with sqlite3.connect(f"file:{demo.service.database}?mode=ro", uri=True) as conn:
            state, response = conn.execute(
                "SELECT state, response FROM integration_operations WHERE operation_id = ?", (op,)
            ).fetchone()
            used = conn.execute("SELECT COUNT(*) FROM integration_authorization_nonces").fetchone()
        assert (state, response) == ("acknowledged", None)
        # retrieve, acknowledge, plus the racing retrieve only if it was served
        assert int(used[0]) == 2 + (1 if out["retrieve"] == 200 else 0)
        demo.service.close()


# --- registration history and accounting invariants -----------------------------------------


def test_startup_never_adopts_a_facility_with_unaccounted_history(tmp_path: Path) -> None:
    demo = Demo(tmp_path)
    demo.service.close()
    with sqlite3.connect(demo.db) as conn:  # standalone history for a new facility identity
        conn.execute(
            "INSERT INTO used_nonces (facility_id, nonce, reservation_id) VALUES (?, ?, ?)",
            ("facility-i2b-with-history", "0" * 32, "reservation-standalone"),
        )
    general = offline_world().facilities[GENERAL_ID]
    historic = FacilityConfig(
        replace(general.facility, facility_id=FacilityId("facility-i2b-with-history")),
        general.procurement_classes,
        "history",
    )
    world = offline_world(extra_facilities=(demo.b_config, historic))
    with pytest.raises(SettlementRefused) as refused:
        open_marketplace(demo.db, demo.agents, world=world, clock=demo.clock)
    assert refused.value.reason == "FACILITY_HAS_HISTORY"
    with sqlite3.connect(demo.db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM facility_accounts WHERE facility_id = 'facility-i2b-with-history'"
        ).fetchone() == (0,)
    demo.open()  # the trusted configuration still opens
    assert demo.get(demo.a, "/v0/live/session").status_code == 200


def test_accounting_and_receipt_chains_stay_consistent_across_agents(tmp_path: Path) -> None:
    demo = Demo(tmp_path)
    a, b = demo.client(demo.a), demo.client(demo.b)
    for i in range(3):
        a.purchase(GENERAL, PROMPT + bytes([65 + i]), expect(demo.a, demo))
        a.purchase(CONTROLLED, PROMPT + bytes([65 + i]), expect(demo.a, demo))
        b.purchase(GENERAL, PROMPT + bytes([65 + i]), expect(demo.b, demo))
    with sqlite3.connect(f"file:{demo.service.database}?mode=ro", uri=True) as conn:
        settled = dict(
            conn.execute(
                "SELECT facility_id, SUM(actual_amount_micro_trlusd) FROM accounting_settlements "
                "GROUP BY facility_id"
            ).fetchall()
        )
    for agent, fids in ((demo.a, (GENERAL_ID, CONTROLLED_ID)), (demo.b, (B_FACILITY,))):
        facilities = {f["facility_id"]: f for f in demo.get(agent, "/v0/live/facilities").json()}
        for fid in fids:
            e = facilities[fid]["exposure"]
            assert e["drawn_micro_trlusd"] == settled[fid]
            assert e["held_micro_trlusd"] == 0
            assert e["limit_micro_trlusd"] == (
                e["drawn_micro_trlusd"] + e["held_micro_trlusd"] + e["headroom_micro_trlusd"]
            )
        ops = demo.get(agent, "/v0/live/operations").json()["operations"]
        for fid in fids:
            previous = None
            for op in (o for o in ops if o["facility_id"] == fid):
                receipt = demo.get(agent, f"/v0/live/receipts/{op['receipt_digest']}").json()
                assert receipt["chain"]["previous_receipt_digest"] == previous
                assert {c["result"] for c in receipt["checks"]} <= {"pass", "not_applicable"}
                previous = receipt["receipt_digest"]
            assert receipt["chain"]["is_chain_head"] is True
