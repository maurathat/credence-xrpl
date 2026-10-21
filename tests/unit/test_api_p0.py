"""Product P0: the offline Credence API adapter (credence/api).

Every route, the Scenario A and B behaviour against the committed market fixture, policy rejection
explanations, every recorded lifecycle outcome, the exposure invariant, receipt re-verification
(and its failure on tampering), and the separation of accounting from XRPL settlement. Offline:
no network.
"""

from __future__ import annotations

import json
import re
import socket
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from credence.api.app import create_app
from credence.api.product import MARKET_CONFIGURATION, ProductReadModel
from credence.integration.journal import OperationState


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "create_connection", refuse)
    yield


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def presets(client: TestClient) -> dict[str, dict[str, Any]]:
    return {p["preset_id"]: p for p in client.get("/v0/procurement/presets").json()}


def evaluate(client: TestClient, **changes: Any) -> dict[str, Any]:
    form = {**presets(client)["scenario-a-general"]["form"], **changes}
    response = client.post("/v0/procurement/evaluate", json=form)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def offers(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {o["provider_id"]: o for o in result["offers"]}


def operations(client: TestClient) -> dict[str, dict[str, Any]]:
    return {o["run_id"]: o for o in client.get("/v0/operations").json()}


# --- health and providers ------------------------------------------------------------------------


def test_health_is_offline_with_no_ledger(client: TestClient) -> None:
    health = client.get("/v0/health").json()
    assert health["mode"] == "offline"
    assert health["live_purchases"] is False
    assert health["ledger"]["connected"] is False and health["ledger"]["network"] is None
    assert health["currency"]["code"] == "tRLUSD"


def test_providers_are_the_configured_nebius_and_local_qwen_offers(client: TestClient) -> None:
    providers = {p["provider_id"]: p for p in client.get("/v0/providers").json()}
    assert set(providers) == {"nebius-token-factory", "local-qwen-provider"}
    nebius, local = providers["nebius-token-factory"], providers["local-qwen-provider"]
    assert nebius["evidence"]["tier"] == 1 and nebius["evidence"]["name"] == "Witnessed"
    assert local["evidence"]["tier"] == 2 and local["evidence"]["name"] == "Bound and signed"
    # Nebius's profile asserts nothing it does not substantiate; catalog metadata stays separate.
    assert "quantization" in nebius["execution_profile"]["not_asserted"]
    assert nebius["execution_profile"]["fields"]["quantization"] is None
    assert nebius["declared_metadata"]["quantization"] == "fp8"
    assert local["execution_profile"]["fields"]["quantization"] == "Q8_0"
    for p in providers.values():
        assert p["quote"]["price_kind"] == "configured_demo"
        assert "Configured demo price" in p["quote"]["price_label"]
        assert p["availability"]["status"] == "not_monitored"
    assert "not Nebius's billed price" in nebius["quote"]["price_label"]


# --- evaluateProcurement -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("preset", "fixture_key"),
    [
        ("scenario-a-general", "scenario_a_general"),
        ("scenario-b-controlled", "scenario_b_controlled"),
    ],
)
def test_preset_dry_runs_reproduce_the_committed_market_selection(
    client: TestClient, preset: str, fixture_key: str
) -> None:
    """Scenario A and B behaviour is preserved: same qualification, codes, costs and choice."""
    expected = json.loads(MARKET_CONFIGURATION.read_text(encoding="ascii"))[fixture_key]
    form = presets(client)[preset]["form"]
    result = client.post("/v0/procurement/evaluate", json=form).json()
    assert result["mode"] == "dry_run" and result["matching_preset"] == preset
    got = [
        (o["provider_id"], o["qualified"], o["reason_code"], o["max_total_cost_micro_trlusd"])
        for o in result["offers"]
    ]
    want = [
        (q["provider_id"], q["qualified"], q["reason_code"], q["max_total_cost_micro_trlusd"])
        for q in expected["qualifications"]
    ]
    assert got == want
    assert result["chosen_provider_id"] == expected["chosen_provider_id"]
    for offer in result["offers"]:
        record = offer["policy_decision"]["object"]
        assert record["decision_stage"] == "policy"
        assert record["engine_version"] == "credence-core-policy-c2"


def test_scenario_b_explains_why_nebius_is_rejected(client: TestClient) -> None:
    form = presets(client)["scenario-b-controlled"]["form"]
    result = client.post("/v0/procurement/evaluate", json=form).json()
    nebius = offers(result)["nebius-token-factory"]
    assert nebius["reason_code"] == "PROFILE_NOT_ALLOWED"
    assert any("quantization" in detail for detail in nebius["reason_details"])
    assert result["competition"] == "1 qualifying provider"


def test_rejections_carry_core_codes_and_plain_reasons(client: TestClient) -> None:
    tier = offers(evaluate(client, minimum_evidence_tier=2))["nebius-token-factory"]
    assert tier["reason_code"] == "TIER_TOO_LOW"
    assert "Tier 2 (Bound and signed) is required" in tier["reason_details"][0]

    price = offers(evaluate(client, max_total_cost_micro_trlusd=100))
    assert price["nebius-token-factory"]["qualified"] is True
    assert price["local-qwen-provider"]["reason_code"] == "PRICE_ABOVE_MAX"

    limit = evaluate(client, facility_limit_micro_trlusd=200, facility_drawn_micro_trlusd=150)
    assert {o["reason_code"] for o in limit["offers"]} == {"OVER_LIMIT"}
    assert limit["chosen_provider_id"] is None and limit["competition"] == "No qualifying provider"

    provider = offers(evaluate(client, allowed_providers=["local-qwen-provider"]))
    assert provider["nebius-token-factory"]["reason_code"] == "PROVIDER_NOT_ALLOWED"
    assert provider["nebius-token-factory"]["reason"]


def test_price_equal_to_the_quote_maximum_is_accepted(client: TestClient) -> None:
    """Core allows equality at PRICE_ABOVE_MAX; the adapter does not second-guess it."""
    result = evaluate(client, max_total_cost_micro_trlusd=86)
    assert offers(result)["nebius-token-factory"]["qualified"] is True


def test_dry_run_returns_no_key_or_signature_material(client: TestClient) -> None:
    text = json.dumps(evaluate(client))
    assert "ed25519:" not in text and "signature" not in text


def test_invalid_or_unexpected_input_is_refused(client: TestClient) -> None:
    form = presets(client)["scenario-a-general"]["form"]
    bad_country = client.post(
        "/v0/procurement/evaluate", json={**form, "allowed_jurisdictions": ["zz"]}
    )
    assert bad_country.status_code == 422
    assert bad_country.json()["error"] == "INVALID_PROCUREMENT"
    # No field for keys or secrets exists; an extra member is refused, not ignored.
    smuggled = client.post("/v0/procurement/evaluate", json={**form, "agent_private_key": "x"})
    assert smuggled.status_code == 422
    floats = client.post(
        "/v0/procurement/evaluate", json={**form, "facility_limit_micro_trlusd": 1.5}
    )
    assert floats.status_code == 422
    none_allowed = client.post("/v0/procurement/evaluate", json={**form, "allowed_providers": []})
    assert none_allowed.status_code == 422


# --- submitPurchase ------------------------------------------------------------------------------


def test_presets_replay_their_recorded_offline_runs_and_nothing_else_is_submitted(
    client: TestClient,
) -> None:
    for preset in presets(client).values():
        result = client.post("/v0/purchases", json=preset["form"]).json()
        assert result["mode"] == "offline_replay"
        assert result["operation_id"] == preset["recorded_operation_id"]
        assert "No purchase was made" in result["detail"]
    custom = {**presets(client)["scenario-a-general"]["form"], "minimum_evidence_tier": 2}
    result = client.post("/v0/purchases", json=custom).json()
    assert result == {
        "mode": "not_available",
        "code": "LIVE_PURCHASE_NOT_CONNECTED",
        "detail": result["detail"],
        "operation_id": None,
    }


# --- getOperation --------------------------------------------------------------------------------


def test_recorded_runs_cover_every_required_outcome(client: TestClient) -> None:
    runs = operations(client)
    classes = {o["outcome_class"] for o in runs.values()}
    assert {"accounting_settled", "rejected", "released", "unknown", "disputed"} <= classes
    assert any(o["interrupted"] for o in runs.values())
    assert runs["scenario-a-settled"]["state"] == "acknowledged"
    assert runs["scenario-b-settled"]["provider_id"] == "local-qwen-provider"
    assert runs["rejected-no-qualifying-offer"]["state"] == "no_qualifying_offer"
    assert runs["disputed-chain-broken"]["reason_code"] == "CHAIN_BROKEN"
    assert runs["late-delivery"]["state"] == "late"
    assert runs["interrupted-after-attempt"]["detail"] == "interrupted"
    # Pending states appear in the recorded steps of the settled run.
    detail = client.get(f"/v0/operations/{runs['scenario-a-settled']['operation_id']}").json()
    seen = [s["journal_state"] for s in detail["steps"]]
    assert {"intent", "executing", "delivered"} <= set(seen)


def test_lifecycle_uses_the_journal_states_with_accounting_settled_naming(
    client: TestClient,
) -> None:
    runs = operations(client)
    settled = client.get(f"/v0/operations/{runs['scenario-a-settled']['operation_id']}").json()
    assert [(s["state"], s["label"]) for s in settled["lifecycle"]] == [
        ("intent", "intent"),
        ("reserved", "reserved"),
        ("executing", "executing"),
        ("delivered", "delivered"),
        ("settled", "accounting_settled"),
        ("retrieved", "retrieved"),
        ("acknowledged", "acknowledged"),
    ]
    assert all(s["reached"] for s in settled["lifecycle"])
    unknown = client.get(
        f"/v0/operations/{runs['unknown-provider-failure']['operation_id']}"
    ).json()
    assert [s["state"] for s in unknown["lifecycle"] if s["reached"]] == [
        "intent",
        "reserved",
        "executing",
    ]
    rejected = runs["rejected-no-qualifying-offer"]["operation_id"]
    assert not any(
        s["reached"] for s in client.get(f"/v0/operations/{rejected}").json()["lifecycle"]
    )


def test_every_state_has_explanatory_text(client: TestClient) -> None:
    from credence.api.explanations import OUTCOME_CLASS, STATE_TEXT

    assert set(STATE_TEXT) == set(OperationState) == set(OUTCOME_CLASS)


def test_unknown_operation_is_404(client: TestClient) -> None:
    for path in ("", "/exposure", "/payment"):
        response = client.get(f"/v0/operations/op-missing{path}")
        assert response.status_code == 404 and response.json()["error"] == "UNKNOWN_OPERATION"


def test_response_retrieval_is_not_connected_and_takes_no_key(client: TestClient) -> None:
    op = operations(client)["scenario-a-settled"]["operation_id"]
    response = client.post(f"/v0/operations/{op}/response")
    assert response.status_code == 501
    assert response.json()["error"] == "RESPONSE_RETRIEVAL_NOT_CONNECTED"


# --- getAccountingExposure -----------------------------------------------------------------------


def test_exposure_invariant_holds_at_every_recorded_step(client: TestClient) -> None:
    for run in operations(client).values():
        exposure = client.get(f"/v0/operations/{run['operation_id']}/exposure").json()
        assert exposure["invariant"] == "limit = drawn + held + headroom"
        for point in exposure["timeline"]:
            assert point["limit_micro_trlusd"] == (
                point["drawn_micro_trlusd"]
                + point["held_micro_trlusd"]
                + point["headroom_micro_trlusd"]
            )


def test_drawn_is_the_settled_receipt_price_and_holds_are_released(client: TestClient) -> None:
    runs = operations(client)
    for run_id in ("scenario-a-settled", "scenario-b-settled"):
        op = runs[run_id]["operation_id"]
        exposure = client.get(f"/v0/operations/{op}/exposure").json()
        receipt = client.get(f"/v0/receipts/{runs[run_id]['receipt_digest']}").json()
        assert exposure["current"]["drawn_micro_trlusd"] == receipt["price_micro_trlusd"]
        assert exposure["current"]["held_micro_trlusd"] == 0
        assert max(p["held_micro_trlusd"] for p in exposure["timeline"]) > 0
    unknown = runs["unknown-provider-failure"]["operation_id"]
    current = client.get(f"/v0/operations/{unknown}/exposure").json()["current"]
    assert current["drawn_micro_trlusd"] == 0 and current["held_micro_trlusd"] > 0


# --- getReceiptEvidence --------------------------------------------------------------------------


def test_every_recorded_receipt_re_verifies(client: TestClient) -> None:
    receipts = client.get("/v0/receipts").json()
    sources = {r["source"] for r in receipts}
    assert sources == {"recorded_live_observation", "recorded_offline_run"}
    for listed in receipts:
        evidence = client.get(f"/v0/receipts/{listed['receipt_digest']}").json()
        results = {c["check"]: c["result"] for c in evidence["checks"]}
        assert "fail" not in results.values(), results
        assert results["receipt_digest"] == "pass" and results["gateway_signature"] == "pass"
        if evidence["evidence"]["tier"] == 2:
            assert results["provider_signature"] == "pass"
            assert results["attestation_binding"] == "pass"
        else:
            assert results["provider_signature"] == "not_applicable"
            assert evidence["provider_attestation"] is None
        ladder = {t["tier"]: t for t in evidence["evidence_ladder"]}
        assert ladder[3]["implemented"] is False and ladder[3]["present"] is False


def test_settled_offline_receipt_is_the_chain_head(client: TestClient) -> None:
    run = operations(client)["scenario-a-settled"]
    evidence = client.get(f"/v0/receipts/{run['receipt_digest']}").json()
    assert evidence["chain"]["accounting_settled"] is True
    assert evidence["chain"]["is_chain_head"] is True
    assert evidence["chain"]["first_on_facility"] is True


def test_tampered_receipt_fails_re_verification() -> None:
    model = ProductReadModel()
    run = next(r for r in model._offline["runs"] if r["run_id"] == "scenario-a-settled")
    digest = run["operation"]["receipt_digest"]
    run["operation"]["objects"]["receipt"]["price_micro_trlusd"] += 1
    results = {c["check"]: c["result"] for c in model.get_receipt_evidence(digest)["checks"]}
    assert results["receipt_digest"] == "fail"
    assert results["gateway_signature"] == "fail"


# --- getPaymentStatus and XRPL fixtures ----------------------------------------------------------


def test_payment_status_is_never_confirmed(client: TestClient) -> None:
    for run in operations(client).values():
        status = client.get(f"/v0/operations/{run['operation_id']}/payment").json()
        assert status["xrpl"]["state"] == "not_submitted"
        assert status["xrpl"]["transaction_hash"] is None
        assert "not an XRPL payment" in status["accounting"]["note"]


def test_xrpl_states_are_labelled_fixtures_with_no_ledger_data(client: TestClient) -> None:
    fixtures = client.get("/v0/xrpl/settlement-fixtures").json()
    assert (
        fixtures["kind"] == "ui-state-fixtures"
        and "UI STATE FIXTURES" in fixtures["fixture_notice"]
    )
    assert [s["state"] for s in fixtures["states"]] == [
        "not_submitted",
        "pending_confirmation",
        "validated_success",
        "validated_failure",
        "expired_not_included",
        "unknown_reconciliation_required",
    ]
    for state in fixtures["states"]:
        for field in ("transaction_hash", "ledger_index", "last_ledger_sequence", "engine_result"):
            assert state[field] is None


# --- claims discipline over every response -------------------------------------------------------


def test_no_response_names_the_real_token_or_claims_mainnet(client: TestClient) -> None:
    runs = operations(client)
    paths = [
        "/v0/health",
        "/v0/providers",
        "/v0/procurement/presets",
        "/v0/operations",
        "/v0/receipts",
        "/v0/xrpl/settlement-fixtures",
    ]
    for run in runs.values():
        op = run["operation_id"]
        paths += [f"/v0/operations/{op}", f"/v0/operations/{op}/exposure"]
    bodies = [json.dumps(client.get(p).json()) for p in paths]
    bodies.append(json.dumps(evaluate(client)))
    for body in bodies:
        assert not re.search(r"(?<![tT])RLUSD", body)
        assert "mainnet" not in body.lower()
        assert "trustless" not in body.lower()
