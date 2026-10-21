"""I2a, CR-9: the product API with the IntegrationService embedded, over HTTP, offline.

End-to-end and adversarial checks through FastAPI's in-process TestClient and the real agent
client (`credence.agent.client`): agent sessions, prepare / verify / sign / submit, agent-scoped
reads, response retrieval and acknowledgment, XRPL status. Offline mock providers; agent keys are
generated per test and never leave the agent client; no network, model or XRPL.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import socket
from collections.abc import Iterator, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from i1_world import Clock
from nacl.signing import SigningKey
from xrpl.core.addresscodec import encode_classic_address

from credence.agent.client import AgentClient, ApiRefused, Expectations
from credence.api.app import create_app
from credence.api.marketplace import token_digest
from credence.api.offline_demo import open_marketplace
from credence.api.offline_world import FacilityConfig, OfflineWorld, offline_world
from credence.api.product import ProductReadModel
from credence.core.objects import PurchaseRequest
from credence.core.primitives import FacilityId, KeyId, MicroTrlusd, XrplAccount
from credence.core.signatures import KeyRegistry, key_id_for, sign
from credence.integration.authorization import ResponseAction
from credence.integration.preparation import PREPARATION_TTL_SECONDS
from credence.integration.service import IntegrationService

B_ACCOUNT = XrplAccount(
    encode_classic_address(hashlib.sha256(b"credence-i2a-api:agent-b").digest()[:20])
)
B_FACILITY = "facility-i2a-api-agent-b"
PRESETS = {p["preset_id"]: p["form"] for p in ProductReadModel().procurement_presets()}
GENERAL = PRESETS["scenario-a-general"]
CONTROLLED = PRESETS["scenario-b-controlled"]
PROMPT = b"Synthetic Credence I2a prompt: list three facts about the number 7."


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Refuse every internet socket. The in-process ASGI client opens none; asyncio's own
    AF_UNIX socketpair (its loop wake-up pipe) is allowed."""
    real = socket.socket

    class Guarded(real):  # type: ignore[misc, valid-type]
        def __init__(self, family: int = -1, *args: object, **kwargs: object) -> None:
            if family in (socket.AF_INET, socket.AF_INET6, -1):
                raise AssertionError("network access attempted")
            super().__init__(family, *args, **kwargs)  # type: ignore[arg-type]

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", Guarded)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield


class Agent:
    def __init__(self, agent_id: str, subject: str) -> None:
        self.agent_id = agent_id
        self.subject = subject
        self.key = SigningKey.generate()
        self.key_id = key_id_for(self.key.verify_key)
        self.token = "cred-agent-" + secrets.token_urlsafe(32)

    def entry(self) -> dict[str, str]:
        return {
            "agent_id": self.agent_id,
            "agent_key_id": self.key_id.value,
            "subject": self.subject,
            "token_sha256": token_digest(self.token),
        }


class Demo:
    def __init__(self, tmp_path: Path, *, nebius_reply: Mapping[str, object] | None = None):
        self.db = tmp_path / "i2a-api.sqlite3"
        self.clock = Clock()
        self.nebius_reply = nebius_reply
        base = offline_world()
        general = base.facilities["facility-market-general"]
        self.b_config = FacilityConfig(
            replace(
                general.facility, facility_id=FacilityId(B_FACILITY), borrower_account=B_ACCOUNT
            ),
            general.procurement_classes,
            "Agent B's general facility",
        )
        borrower = general.facility.borrower_account.value
        self.a = Agent("agent-a", borrower)
        self.b = Agent("agent-b", B_ACCOUNT.value)
        self.agents = [self.a.entry(), self.b.entry()]
        self.open()

    def open(self) -> None:
        self.world: OfflineWorld = offline_world(
            nebius_reply=self.nebius_reply, extra_facilities=(self.b_config,)
        )
        self.registry: KeyRegistry = self.world.registry(
            {KeyId(e["agent_key_id"]): e["subject"] for e in self.agents}
        )
        self.service: IntegrationService
        self.service, marketplace, _ = open_marketplace(
            self.db, self.agents, world=self.world, clock=self.clock, registry=self.registry
        )
        self.http = TestClient(create_app(marketplace=marketplace))

    def restart(self) -> None:
        self.service.close()
        self.open()

    def transport(self) -> Any:
        def call(
            method: str, path: str, body: Mapping[str, Any] | None, headers: Mapping[str, str]
        ) -> tuple[int, bytes]:
            r = self.http.request(method, path, json=body, headers=dict(headers))
            return r.status_code, r.content

        return call

    def client(self, agent: Agent) -> AgentClient:
        return AgentClient(self.transport(), agent.token, agent.key, clock=self.clock.peek)

    def get(self, agent: Agent | None, path: str) -> Any:
        headers = {} if agent is None else {"authorization": f"Bearer {agent.token}"}
        return self.http.get(path, headers=headers)

    def post(self, agent: Agent | None, path: str, body: Any) -> Any:
        headers = {} if agent is None else {"authorization": f"Bearer {agent.token}"}
        return self.http.post(path, json=body, headers=headers)

    def provider_calls(self) -> int:
        return len(self.world.nebius_transport.posts) + self.world.local.calls


def expect(agent: Agent, demo: Demo, **kwargs: Any) -> Expectations:
    facilities = demo.client(agent).session()["facility_ids"]
    return Expectations(facility_ids=frozenset(facilities), **kwargs)


def error(response: Any) -> tuple[int, str]:
    return response.status_code, response.json()["error"]


# --- health and authentication --------------------------------------------------------------


def test_health_reports_the_embedded_service_with_offline_mocks(tmp_path: Path) -> None:
    demo = Demo(tmp_path)
    integration = demo.http.get("/v0/health").json()["integration"]
    assert integration == {
        "integrated_service": True,
        "providers": "offline_mock",
        "providers_note": integration["providers_note"],
        "live_provider_execution": False,
        "buyer_price_cap": "preview_only",
        "xrpl": "not_connected",
    }
    assert "no provider call" in integration["providers_note"]


def test_without_an_embedded_service_live_routes_are_unavailable() -> None:
    http = TestClient(create_app())
    assert http.get("/v0/health").json()["integration"] == {"integrated_service": False}
    r = http.get("/v0/live/session", headers={"authorization": "Bearer x"})
    assert error(r) == (503, "INTEGRATED_SERVICE_NOT_RUNNING")


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/v0/live/session"),
        ("GET", "/v0/live/facilities"),
        ("GET", "/v0/live/operations"),
        ("GET", "/v0/live/operations/op-" + "0" * 32),
        ("GET", "/v0/live/purchases/preparations/prep-" + "0" * 32),
        ("GET", "/v0/live/receipts/sha256:" + "0" * 64),
    ],
)
def test_live_routes_require_a_valid_session(tmp_path: Path, method: str, path: str) -> None:
    demo = Demo(tmp_path)
    assert error(demo.http.request(method, path)) == (401, "AUTHENTICATION_REQUIRED")
    bad = demo.http.request(method, path, headers={"authorization": "Bearer not-a-session"})
    assert error(bad) == (401, "INVALID_SESSION")
    # A token is never read from the query string.
    query = demo.http.request(method, f"{path}?token={demo.a.token}")
    assert error(query) == (401, "AUTHENTICATION_REQUIRED")


def test_the_session_names_only_the_agents_own_facilities(tmp_path: Path) -> None:
    demo = Demo(tmp_path)
    a = demo.client(demo.a).session()
    assert a["agent_key_id"] == demo.a.key_id.value
    assert set(a["facility_ids"]) == {"facility-market-general", "facility-market-controlled"}
    assert demo.client(demo.b).session()["facility_ids"] == [B_FACILITY]
    facilities = demo.get(demo.b, "/v0/live/facilities").json()
    assert [f["facility_id"] for f in facilities] == [B_FACILITY]
    assert facilities[0]["exposure"]["drawn_micro_trlusd"] == 0


# --- end to end -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("form", "provider", "tier"),
    [(GENERAL, "nebius-token-factory", 1), (CONTROLLED, "local-qwen-provider", 2)],
    ids=["scenario-a", "scenario-b"],
)
def test_scenario_end_to_end_over_http(
    tmp_path: Path, form: dict[str, Any], provider: str, tier: int
) -> None:
    demo = Demo(tmp_path)
    agent = demo.client(demo.a)
    prepared = agent.prepare(form, PROMPT)
    assert prepared["provider_id"] == provider
    assert prepared["price"]["price_kind"] == "configured_demo"
    assert prepared["evidence"]["tier"] == tier
    assert demo.provider_calls() == 0  # preparing calls nothing
    signature = agent.sign_preparation(prepared, PROMPT, expect(demo.a, demo))
    report = agent.submit(prepared["preparation_id"], PROMPT, signature)
    assert report["state"] == "settled"
    assert report["outcome_class"] == "accounting_settled"
    assert report["provider_id"] == provider
    assert demo.provider_calls() == 1
    op_id = report["operation_id"]

    op = demo.get(demo.a, f"/v0/live/operations/{op_id}").json()
    assert op["source"] == "integrated_offline_service"
    assert [s["reached"] for s in op["lifecycle"]] == [True] * 5 + [False, False]
    assert op["response"]["available_to_agent"] is True
    assert op["objects"]["purchase_request"] == prepared["request"]["object"]
    assert "delivered" not in {e["event"] for e in op["events"]}

    exposure = demo.get(demo.a, f"/v0/live/operations/{op_id}/exposure").json()
    current = exposure["exposure"]
    assert current["drawn_micro_trlusd"] > 0 and current["held_micro_trlusd"] == 0
    assert current["limit_micro_trlusd"] == (
        current["drawn_micro_trlusd"]
        + current["held_micro_trlusd"]
        + current["headroom_micro_trlusd"]
    )

    receipt = demo.get(demo.a, f"/v0/live/receipts/{report['receipt_digest']}").json()
    assert {c["result"] for c in receipt["checks"]} <= {"pass", "not_applicable"}
    assert receipt["accounting"]["settled"] is True
    assert receipt["accounting"]["actual_amount_micro_trlusd"] == current["drawn_micro_trlusd"]
    assert receipt["chain"]["is_chain_head"] is True
    assert receipt["verifier"]["decision"]["outcome"] == "accept"
    assert receipt["evidence"]["tier"] == tier

    payment = demo.get(demo.a, f"/v0/live/operations/{op_id}/payment").json()
    assert payment["xrpl"]["state"] == "not_submitted"
    assert payment["xrpl"]["transaction_hash"] is None
    assert payment["xrpl"]["validated_ledger_index"] is None
    assert payment["xrpl"]["source"] == "no_xrpl_adapter"

    response = agent.retrieve(op_id)
    assert b"Synthetic" in response
    # The response is never in any read.
    for path in (f"/v0/live/operations/{op_id}", f"/v0/live/receipts/{report['receipt_digest']}"):
        assert response not in demo.get(demo.a, path).content
    agent.acknowledge(op_id)
    after = demo.get(demo.a, f"/v0/live/operations/{op_id}").json()
    assert after["state"] == "acknowledged"
    assert after["response"]["retained"] is False
    assert [s["reached"] for s in after["lifecycle"]] == [True] * 7
    with pytest.raises(ApiRefused) as refused:
        agent.retrieve(op_id)
    assert (refused.value.status, refused.value.code) == (409, "RESPONSE_UNAVAILABLE")


def test_the_cli_style_authorization_can_be_posted_by_another_client(tmp_path: Path) -> None:
    """The browser flow: the agent signs an authorization outside the UI; the UI posts it."""
    demo = Demo(tmp_path)
    agent = demo.client(demo.a)
    report = agent.purchase(GENERAL, PROMPT, expect(demo.a, demo))
    op_id = report["operation_id"]
    body = agent.authorization(op_id, ResponseAction.RETRIEVE)
    r = demo.post(demo.a, f"/v0/live/operations/{op_id}/response", body)
    assert r.status_code == 200 and r.headers["content-type"] == "application/octet-stream"
    assert error(demo.post(demo.a, f"/v0/live/operations/{op_id}/response", body)) == (
        403,
        "NONCE_REUSED",
    )
    ack = agent.authorization(op_id, ResponseAction.ACKNOWLEDGE)
    assert demo.post(demo.a, f"/v0/live/operations/{op_id}/response", ack).status_code == 204


# --- refusals at preparation ----------------------------------------------------------------


def test_the_buyer_price_cap_stays_preview_only(tmp_path: Path) -> None:
    demo = Demo(tmp_path)
    with pytest.raises(ApiRefused) as refused:
        demo.client(demo.a).prepare({**GENERAL, "max_total_cost_micro_trlusd": 1_000_000}, PROMPT)
    assert (refused.value.status, refused.value.code) == (422, "PRICE_CAP_PREVIEW_ONLY")


def test_an_unregistered_procurement_cannot_be_purchased(tmp_path: Path) -> None:
    demo = Demo(tmp_path)
    narrowed = {**GENERAL, "allowed_providers": ["local-qwen-provider"]}
    with pytest.raises(ApiRefused) as refused:
        demo.client(demo.a).prepare(narrowed, PROMPT)
    assert (refused.value.status, refused.value.code) == (422, "PROCUREMENT_NOT_REGISTERED")
    # The dry run still evaluates it.
    dry = demo.http.post("/v0/procurement/evaluate", json={**narrowed, "prompt": "x"})
    assert dry.status_code == 200 and dry.json()["chosen_provider_id"] == "local-qwen-provider"


def test_request_bodies_are_strict(tmp_path: Path) -> None:
    demo = Demo(tmp_path)
    body = {
        **{
            k: v
            for k, v in GENERAL.items()
            if k not in ("prompt", "facility_limit_micro_trlusd", "facility_drawn_micro_trlusd")
        },
        "prompt_digest": "sha256:" + "0" * 64,
    }
    assert demo.post(demo.a, "/v0/live/purchases/prepare", body).status_code == 200
    for extra in ({"agent_private_key": "00"}, {"prompt": "plaintext"}):
        assert demo.post(demo.a, "/v0/live/purchases/prepare", {**body, **extra}).status_code == 422
    assert error(
        demo.post(demo.a, "/v0/live/purchases/prepare", {**body, "prompt_digest": "abc"})
    ) == (422, "INVALID_PROMPT_DIGEST")


# --- tampering and wrong agents -------------------------------------------------------------


def test_a_tampered_or_changed_submission_is_refused(tmp_path: Path) -> None:
    demo = Demo(tmp_path)
    agent = demo.client(demo.a)
    prepared = agent.prepare(GENERAL, PROMPT)
    request = PurchaseRequest.from_canonical(prepared["request"]["object"])
    pid = prepared["preparation_id"]
    for change in (
        {"max_total_cost_micro_trlusd": MicroTrlusd(1)},
        {"expires_at": request.expires_at.plus_seconds(3600)},
    ):
        bad = sign(replace(request, **change), demo.a.key).to_canonical()  # type: ignore[arg-type]
        with pytest.raises(ApiRefused) as refused:
            agent.submit(pid, PROMPT, bad)
        assert (refused.value.status, refused.value.code) == (422, "REQUEST_MISMATCH")
    good = sign(request, demo.a.key).to_canonical()
    with pytest.raises(ApiRefused) as refused:
        agent.submit(pid, PROMPT + b" (changed after signing)", good)
    assert refused.value.code == "PROMPT_MISMATCH"
    extra = demo.post(
        demo.a,
        "/v0/live/purchases/submit",
        {"preparation_id": pid, "prompt": PROMPT.decode(), "signature": {**good, "seed": "00"}},
    )
    assert extra.status_code == 422
    assert demo.provider_calls() == 0
    assert agent.submit(pid, PROMPT, good)["state"] == "settled"


def test_another_agent_can_neither_see_nor_use_a_preparation_or_operation(tmp_path: Path) -> None:
    demo = Demo(tmp_path)
    a, b = demo.client(demo.a), demo.client(demo.b)
    prepared = a.prepare(GENERAL, PROMPT)
    pid = prepared["preparation_id"]
    assert error(demo.get(demo.b, f"/v0/live/purchases/preparations/{pid}")) == (
        404,
        "UNKNOWN_PREPARATION",
    )
    b_signature = sign(
        PurchaseRequest.from_canonical(prepared["request"]["object"]), demo.b.key
    ).to_canonical()
    with pytest.raises(ApiRefused) as refused:
        b.submit(pid, PROMPT, b_signature)
    assert (refused.value.status, refused.value.code) == (404, "UNKNOWN_PREPARATION")
    with pytest.raises(ApiRefused) as refused:
        a.submit(pid, PROMPT, b_signature)  # A's session, B's signature
    assert (refused.value.status, refused.value.code) == (403, "WRONG_AGENT")

    report = a.submit(pid, PROMPT, a.sign_preparation(prepared, PROMPT, expect(demo.a, demo)))
    op_id, digest = report["operation_id"], report["receipt_digest"]
    for path in (
        f"/v0/live/operations/{op_id}",
        f"/v0/live/operations/{op_id}/exposure",
        f"/v0/live/operations/{op_id}/payment",
    ):
        assert error(demo.get(demo.b, path)) == (404, "UNKNOWN_OPERATION")
    assert error(demo.get(demo.b, f"/v0/live/receipts/{digest}")) == (404, "UNKNOWN_RECEIPT")
    assert demo.get(demo.b, "/v0/live/operations").json()["operations"] == []
    assert error(demo.get(demo.b, f"/v0/live/operations?after={op_id}")) == (
        404,
        "UNKNOWN_OPERATION",
    )
    # B's own valid authorization cannot open A's operation; A's session cannot use B's key.
    b_auth = b.authorization(op_id, ResponseAction.RETRIEVE)
    assert error(demo.post(demo.b, f"/v0/live/operations/{op_id}/response", b_auth)) == (
        404,
        "UNKNOWN_OPERATION",
    )
    assert error(demo.post(demo.a, f"/v0/live/operations/{op_id}/response", b_auth)) == (
        403,
        "WRONG_AGENT",
    )
    # B cannot prepare on A's facility.
    with pytest.raises(ApiRefused) as refused:
        b.prepare(GENERAL, PROMPT, facility_id="facility-market-general")
    assert (refused.value.status, refused.value.code) == (404, "UNKNOWN_FACILITY")


def test_a_revoked_agent_is_refused_everywhere(tmp_path: Path) -> None:
    demo = Demo(tmp_path)
    agent = demo.client(demo.a)
    prepared = agent.prepare(GENERAL, PROMPT)
    signature = agent.sign_preparation(prepared, PROMPT, expect(demo.a, demo))
    demo.registry.revoke(demo.a.key_id)
    for r in (
        demo.get(demo.a, "/v0/live/session"),
        demo.get(demo.a, "/v0/live/operations"),
        demo.post(
            demo.a,
            "/v0/live/purchases/submit",
            {
                "preparation_id": prepared["preparation_id"],
                "prompt": PROMPT.decode(),
                "signature": signature,
            },
        ),
    ):
        assert error(r) == (403, "AGENT_REVOKED")
    demo.restart()
    assert error(demo.get(demo.a, "/v0/live/session")) == (403, "AGENT_REVOKED")
    assert demo.provider_calls() == 0


def test_an_expired_preparation_is_refused(tmp_path: Path) -> None:
    demo = Demo(tmp_path)
    agent = demo.client(demo.a)
    prepared = agent.prepare(GENERAL, PROMPT)
    signature = agent.sign_preparation(prepared, PROMPT, expect(demo.a, demo))
    demo.clock.advance(PREPARATION_TTL_SECONDS + 1)
    with pytest.raises(ApiRefused) as refused:
        agent.submit(prepared["preparation_id"], PROMPT, signature)
    assert (refused.value.status, refused.value.code) == (410, "PREPARATION_EXPIRED")


# --- idempotency, restart, provider failure -------------------------------------------------


def test_duplicate_and_post_restart_submissions_buy_once(tmp_path: Path) -> None:
    demo = Demo(tmp_path)
    agent = demo.client(demo.a)
    prepared = agent.prepare(GENERAL, PROMPT, idempotency_key="research-run-0001")
    signature = agent.sign_preparation(prepared, PROMPT, expect(demo.a, demo))
    first = agent.submit(prepared["preparation_id"], PROMPT, signature)
    assert agent.submit(prepared["preparation_id"], PROMPT, signature) == first
    demo.restart()
    agent = demo.client(demo.a)
    assert agent.submit(prepared["preparation_id"], PROMPT, signature) == first
    assert demo.provider_calls() == 0  # the restarted process never executed it again
    with pytest.raises(ApiRefused) as refused:
        agent.prepare(GENERAL, PROMPT, idempotency_key="research-run-0001")
    assert (refused.value.status, refused.value.code) == (409, "ALREADY_SUBMITTED")
    # B may use the same key text: idempotency keys are namespaced per agent.
    other = demo.client(demo.b).prepare(GENERAL, PROMPT, idempotency_key="research-run-0001")
    assert other["operation_id"] != first["operation_id"]
    listed = demo.get(demo.a, "/v0/live/operations").json()["operations"]
    assert [o["operation_id"] for o in listed] == [first["operation_id"]]


def test_a_provider_error_is_unknown_and_the_response_unavailable(tmp_path: Path) -> None:
    demo = Demo(tmp_path, nebius_reply={"id": "x", "object": "chat.completion"})
    agent = demo.client(demo.a)
    report = agent.purchase(GENERAL, PROMPT, expect(demo.a, demo))
    assert report["state"] == "unknown"
    assert report["outcome_class"] == "unknown"
    op = demo.get(demo.a, f"/v0/live/operations/{report['operation_id']}").json()
    assert op["response"]["available_to_agent"] is False
    with pytest.raises(ApiRefused) as refused:
        agent.retrieve(report["operation_id"])
    assert (refused.value.status, refused.value.code) == (409, "RESPONSE_UNAVAILABLE")
    assert (
        demo.get(demo.a, f"/v0/live/operations/{report['operation_id']}/payment").json()["xrpl"][
            "state"
        ]
        == "not_submitted"
    )


def test_operations_are_paginated(tmp_path: Path) -> None:
    demo = Demo(tmp_path)
    agent = demo.client(demo.a)
    ids = [
        agent.purchase(GENERAL, PROMPT + str(i).encode(), expect(demo.a, demo))["operation_id"]
        for i in range(3)
    ]
    page = demo.get(demo.a, "/v0/live/operations?limit=2").json()
    assert [o["operation_id"] for o in page["operations"]] == ids[:2]
    rest = demo.get(demo.a, f"/v0/live/operations?limit=2&after={page['next_after']}").json()
    assert [o["operation_id"] for o in rest["operations"]] == ids[2:]
    assert rest["next_after"] is None
    assert demo.get(demo.a, "/v0/live/operations?limit=0").status_code == 422


def test_no_response_body_carries_key_material(tmp_path: Path) -> None:
    demo = Demo(tmp_path)
    agent = demo.client(demo.a)
    report = agent.purchase(GENERAL, PROMPT, expect(demo.a, demo))
    seed = bytes(demo.a.key).hex()
    for path in (
        "/v0/live/session",
        "/v0/live/facilities",
        "/v0/live/operations",
        f"/v0/live/operations/{report['operation_id']}",
        f"/v0/live/receipts/{report['receipt_digest']}",
    ):
        body = demo.get(demo.a, path).text
        assert seed not in body and demo.a.token not in body
    assert json.dumps(demo.agents).count(demo.a.token) == 0
