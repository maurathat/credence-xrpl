"""X1q: read-only QuickNode XRPL Testnet transport, exercised offline against a fake Clio endpoint.

Adversarial cases from the X1q brief are numbered ``test_aNN_*`` (NN is the brief's item number).
No test opens a socket, reaches QuickNode or writes to any ledger.
"""

from __future__ import annotations

import json
import pickle
import re
import ssl
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from test_xrpl_x1c_remediation import OPERATION, chain, outbox_events, trlusd
from x1a_rippled import standard_ledger
from x1a_world import NETWORK_ID, VALIDATED_LEDGER
from x1q_quicknode import (
    CLOSE_TIME,
    HISTORY,
    HOST,
    ISSUER,
    NOW,
    OTHER_ISSUER,
    RECEIVER,
    SENDER,
    SIGNER_A,
    SIGNER_B,
    TESTNET_ID,
    TOKEN,
    URL,
    VALIDATED,
    FakeQuickNode,
    Reply,
    ScriptedHTTPS,
    config,
    endpoint,
    funded_sender,
    issued,
    ledger_hash_for,
    signed_testnet_payment,
)

from credence.xrpl_adapter.journal import PaymentJournal
from credence.xrpl_adapter.preparation import (
    TRLUSD_WIRE_CODE,
    OfflinePaymentPreparer,
    XrplSignedPaymentInspector,
)
from credence.xrpl_adapter.quicknode import (
    API_VERSION,
    OBSERVATION_SCHEMA,
    HostedNetwork,
    HostedObservationStatus,
    HostedPaymentExpectation,
    NetworkIsolationError,
    QuickNodeConfig,
    QuickNodeConfigError,
    QuickNodeEndpoint,
    QuickNodeError,
    QuickNodeJsonRpcClient,
    QuickNodeRateLimited,
    QuickNodeReconciliationView,
    QuickNodeRefused,
    QuickNodeTimeout,
    QuickNodeUnavailable,
    QuickNodeXRPLTransport,
    assess_hosted_payment,
)
from credence.xrpl_adapter.quicknode_probe import main as probe_main
from credence.xrpl_adapter.quicknode_probe import run_probe
from credence.xrpl_adapter.rippled import RippledTransport, RippledTransportConfig, TransportRefused
from credence.xrpl_adapter.service import PaymentResultVerifier, PaymentService
from credence.xrpl_adapter.types import (
    Asset,
    LedgerPayment,
    LookupDisposition,
    LookupUnavailable,
    TransactionState,
    TransactionType,
    VerificationDisposition,
)

READ_METHODS = {"server_info", "server_state", "ledger", "account_info", "account_objects"}
READ_METHODS |= {"account_lines", "tx"}
TESTNET_ASSET = Asset("tRLUSD", TRLUSD_WIRE_CODE, ISSUER.account)


def transport(node: FakeQuickNode | None = None, **changes: Any) -> QuickNodeXRPLTransport:
    return QuickNodeXRPLTransport(node or FakeQuickNode(), config(**changes), clock=lambda: NOW)


def observed(node: FakeQuickNode, blob: bytes, **include: Any) -> Any:
    tx_hash = node.include(blob, **include)
    return transport(node).observe_payment(tx_hash)


def expectation(tx_hash: str, **changes: Any) -> HostedPaymentExpectation:
    fields: dict[str, Any] = {
        "transaction_hash": tx_hash,
        "network_id": TESTNET_ID,
        "account": SENDER.account,
        "destination": RECEIVER.account,
        "asset": TESTNET_ASSET,
        "amount_micro_units": 2_500_000,
    }
    fields.update(changes)
    return HostedPaymentExpectation(**fields)


def client(replies: list[Reply | BaseException], **changes: Any) -> tuple[Any, ScriptedHTTPS, list]:
    sleeps: list[float] = []
    handler = ScriptedHTTPS(replies)
    rpc = QuickNodeJsonRpcClient(config(**changes), sleep=sleeps.append, https_handler=handler)
    return rpc, handler, sleeps


def ok(result: dict[str, Any] | None = None) -> Reply:
    return Reply(body=json.dumps({"result": result or {"status": "success"}}).encode())


def no_secret(text: str) -> None:
    assert TOKEN not in text
    assert HOST not in text


def raised(call: Any) -> QuickNodeError:
    with pytest.raises(QuickNodeError) as caught:
        call()
    return caught.value


# --- configuration and endpoint ------------------------------------------------------------------


def test_environment_requires_an_explicit_network() -> None:
    assert QuickNodeConfig.from_environment({}) is None
    error = raised(lambda: QuickNodeConfig.from_environment({"CREDENCE_QUICKNODE_XRPL_URL": URL}))
    assert error.code == "NETWORK_NOT_CONFIGURED"
    no_secret(str(error))
    loaded = QuickNodeConfig.from_environment(
        {"CREDENCE_QUICKNODE_XRPL_URL": URL, "CREDENCE_QUICKNODE_XRPL_NETWORK": "testnet"}
    )
    assert loaded is not None
    assert (loaded.network, loaded.source_id) == (
        HostedNetwork.TESTNET,
        "quicknode:quicknode:testnet",
    )
    assert loaded.network.network_id == TESTNET_ID


@pytest.mark.parametrize(
    ("network", "code"),
    [
        ("mainnet", "MAINNET_NOT_AUTHORIZED"),
        ("0", "MAINNET_NOT_AUTHORIZED"),
        ("21337", "LOCAL_NETWORK_NOT_HOSTED"),
        ("standalone", "LOCAL_NETWORK_NOT_HOSTED"),
        ("local", "LOCAL_NETWORK_NOT_HOSTED"),
        ("devnet", "UNSUPPORTED_NETWORK"),
        ("1", "UNSUPPORTED_NETWORK"),
    ],
)
def test_only_testnet_may_be_configured(network: str, code: str) -> None:
    environment = {"CREDENCE_QUICKNODE_XRPL_URL": URL, "CREDENCE_QUICKNODE_XRPL_NETWORK": network}
    assert raised(lambda: QuickNodeConfig.from_environment(environment)).code == code


@pytest.mark.parametrize(
    ("url", "code"),
    [
        (f"http://{HOST}/{TOKEN}/", "ENDPOINT_NOT_HTTPS"),
        (f"https://user:pw@{HOST}/{TOKEN}/", "ENDPOINT_HAS_USERINFO"),
        (f"https://{HOST}:8443/{TOKEN}/", "ENDPOINT_PORT_NOT_ALLOWED"),
        (f"https://{HOST}/{TOKEN}/?next=x", "ENDPOINT_HAS_QUERY"),
        (f"https://{HOST}/{TOKEN}/#frag", "ENDPOINT_HAS_QUERY"),
        (f"https://s.altnet.rippletest.net/{TOKEN}/", "ENDPOINT_NOT_QUICKNODE"),
        (f"https://{HOST}.attacker.example/{TOKEN}/", "ENDPOINT_NOT_QUICKNODE"),
        (f"https://quiknode.pro/{TOKEN}/", "ENDPOINT_NOT_QUICKNODE"),
        ("https://127.0.0.1:5005/", "ENDPOINT_NOT_QUICKNODE"),
        ("https://localhost/", "ENDPOINT_NOT_QUICKNODE"),
        (f"https://words.xrp-mainnet.quiknode.pro/{TOKEN}/", "ENDPOINT_NETWORK_MISMATCH"),
        (f"https://words.xrp-devnet.quiknode.pro/{TOKEN}/", "ENDPOINT_NETWORK_MISMATCH"),
        (f" {URL}", "ENDPOINT_MALFORMED"),
        (URL.replace("/x1q", "/x 1q"), "ENDPOINT_MALFORMED"),
        ("", "ENDPOINT_MALFORMED"),
    ],
    ids=lambda value: value if value.startswith("ENDPOINT_") else "",  # never print the URL
)
def test_endpoint_rules(url: str, code: str) -> None:
    error = raised(lambda: QuickNodeEndpoint(url, HostedNetwork.TESTNET))
    assert isinstance(error, QuickNodeConfigError)
    assert error.code == code
    no_secret(str(error))


def test_config_bounds_and_no_node_key_pin() -> None:
    for changes in (
        {"timeout_seconds": 0},
        {"timeout_seconds": 31},
        {"max_attempts": 0},
        {"max_attempts": 6},
        {"label": "Has Spaces"},
        {"max_validated_ledger_age_seconds": 0},
    ):
        with pytest.raises(QuickNodeConfigError):
            config(**changes)
    # Hosted trust policy: there is deliberately no node-key or build pin to configure.
    fields = set(QuickNodeConfig.__dataclass_fields__)
    assert not {name for name in fields if "node" in name or "build" in name}
    # The local rippled transport keeps its pins unchanged.
    assert {"expected_build_version", "expected_node_public_key"} <= set(
        RippledTransportConfig.__dataclass_fields__
    )


# --- read paths ------------------------------------------------------------------------------------


def test_validated_ledger_identity_from_clio() -> None:
    node = FakeQuickNode()
    identity = transport(node).validated_ledger_identity()
    assert (identity.network_id, identity.ledger_index) == (TESTNET_ID, VALIDATED)
    assert identity.ledger_hash == ledger_hash_for(VALIDATED)
    assert (identity.source_kind, identity.server_version, identity.rippled_version) == (
        "clio",
        "2.7.0",
        "3.1.0",
    )
    assert identity.complete_ledgers == HISTORY
    assert identity.close_time == CLOSE_TIME
    assert [item.method for item in identity.raw] == ["server_info", "ledger"]
    assert all(params["api_version"] == API_VERSION for _, params in node.calls)


def test_account_signer_list_objects_and_lines_are_pinned() -> None:
    node = FakeQuickNode(page_size=1)
    funded_sender(node)
    reader = transport(node)
    identity = reader.validated_ledger_identity()
    state = reader.account_state(SENDER.account, identity)
    assert (state.exists, state.sequence, state.balance_drops, state.owner_count) == (
        True,
        9,
        90_000_000,
        2,
    )
    assert state.signer_list is not None and state.signer_list.quorum == 2
    assert {entry.account for entry in state.signer_list.entries} == {
        SIGNER_A.account,
        SIGNER_B.account,
    }
    objects = reader.account_objects(SENDER.account, identity)
    assert [item["LedgerEntryType"] for item in objects] == ["SignerList", "RippleState"]
    assert len(reader.account_objects(SENDER.account, identity, "signer_list")) == 1
    (line,) = reader.account_lines(SENDER.account, identity)
    assert (line.peer, line.currency, line.balance, line.no_ripple) == (
        ISSUER.account,
        TRLUSD_WIRE_CODE,
        "10.5",
        True,
    )
    pinned = [p for m, p in node.calls if m.startswith("account_")]
    assert pinned and all(p["ledger_hash"] == identity.ledger_hash for p in pinned)
    assert not reader.account_state(RECEIVER.account, identity).exists


def test_account_read_from_another_ledger_is_refused() -> None:
    node = FakeQuickNode()
    funded_sender(node)
    reader = transport(node)
    identity = reader.validated_ledger_identity()
    node.faults["account_info"] = lambda request: {
        "result": {
            "account_data": {"Account": SENDER.account, "Sequence": 1, "Balance": "1"},
            "ledger_hash": ledger_hash_for(VALIDATED + 1),
            "ledger_index": VALIDATED + 1,
            "validated": True,
            "status": "success",
        }
    }
    assert raised(lambda: reader.account_state(SENDER.account, identity)).code == (
        "LEDGER_STATE_INCONSISTENT"
    )


def test_more_than_one_signer_list_is_refused() -> None:
    node = FakeQuickNode()
    funded_sender(node)
    node.signer_lists[SENDER.account] *= 2
    reader = transport(node)
    identity = reader.validated_ledger_identity()
    assert raised(lambda: reader.account_state(SENDER.account, identity)).code == (
        "MALFORMED_RESPONSE"
    )


def test_validated_issued_payment_observation() -> None:
    node = FakeQuickNode()
    observation = observed(node, signed_testnet_payment())
    assert observation.status is HostedObservationStatus.VALIDATED
    assert (observation.result, observation.network_id) == ("tesSUCCESS", TESTNET_ID)
    assert observation.ledger_index == VALIDATED - 10
    assert observation.ledger_hash == ledger_hash_for(VALIDATED - 10)
    assert observation.delivered is not None and observation.delivered.value == "2.5"
    assert observation.delivered.micro_units() == 2_500_000
    envelope = json.loads(observation.evidence)
    assert envelope["schema"] == OBSERVATION_SCHEMA
    assert envelope["trust"] == "single-hosted-source"
    assert envelope["validated_identity"]["source_kind"] == "clio"
    assert "node_public_key" not in envelope["validated_identity"]
    assert {method for method, _ in node.calls} <= READ_METHODS


def test_validated_xrp_payment_observation() -> None:
    observation = observed(FakeQuickNode(), signed_testnet_payment(amount="1500000"))
    assert observation.status is HostedObservationStatus.VALIDATED
    assert observation.delivered is not None
    assert (observation.delivered.currency, observation.delivered.value) == ("XRP", "1500000")
    assert observation.delivered.micro_units() is None


def test_validated_ledger_observation_maps_to_the_x0t_type() -> None:
    node = FakeQuickNode(complete_ledgers="4000-4500,4600-5000")
    funded_sender(node)
    observation = transport(node).validated_ledger_observation(SENDER.account)
    assert observation.source_id == "quicknode:quicknode:testnet"
    assert observation.account_sequence == 9
    assert observation.has_trusted_identity()
    assert not observation.proves_non_inclusion(earliest=4400, last=4700)


# --- adversarial: the brief's sixteen cases -------------------------------------------------------


@pytest.mark.parametrize("network_id", [NETWORK_ID, 0, 2])
def test_a01_incorrect_network_latches_the_transport_closed(network_id: int) -> None:
    node = FakeQuickNode(network_id=network_id)
    reader = transport(node)
    assert raised(reader.validated_ledger_identity).code == "NETWORK_MISMATCH"
    node.network_id = TESTNET_ID  # no silent recovery onto the "right" network
    assert raised(reader.validated_ledger_identity).code == "SOURCE_CONTRADICTED"
    tx_hash = node.include(signed_testnet_payment())
    observation = reader.observe_payment(tx_hash)
    assert (observation.status, observation.code) == (
        HostedObservationStatus.REFUSED,
        "SOURCE_CONTRADICTED",
    )


def test_a02_missing_network_identity_is_refused() -> None:
    node = FakeQuickNode(network_id=None)
    reader = transport(node)
    assert raised(reader.validated_ledger_identity).code == "NETWORK_IDENTITY_MISSING"
    assert reader.contradiction is None
    node.network_id = TESTNET_ID
    assert reader.validated_ledger_identity().network_id == TESTNET_ID
    node.backend_network_id = None
    assert raised(reader.sample_node_identity).code == "NETWORK_IDENTITY_MISSING"
    node.faults["server_info"] = lambda request: {
        "result": {"info": {"network_id": "1"}, "status": "success"}
    }
    assert raised(reader.validated_ledger_identity).code == "MALFORMED_RESPONSE"


def test_a03_node_key_changes_are_recorded_not_pinned() -> None:
    node = FakeQuickNode(node_keys=["n9BackendA", "n9BackendB", "n9BackendC"])
    reader = transport(node)
    samples = [reader.sample_node_identity() for _ in range(4)]
    assert [sample.node_public_key for sample in samples] == [
        "n9BackendA",
        "n9BackendB",
        "n9BackendC",
        "n9BackendA",
    ]
    assert all(sample.forwarded for sample in samples)
    assert reader.observed_node_public_keys == ("n9BackendA", "n9BackendB", "n9BackendC")
    assert reader.contradiction is None
    tx_hash = node.include(signed_testnet_payment())
    assert reader.observe_payment(tx_hash).status is HostedObservationStatus.VALIDATED


def test_a03_backend_reporting_another_network_is_a_contradiction() -> None:
    node = FakeQuickNode(backend_network_id=0)
    reader = transport(node)
    assert raised(reader.sample_node_identity).code == "NETWORK_MISMATCH"
    assert reader.contradiction is not None


def test_a03_local_rippled_node_key_pin_is_unchanged() -> None:
    rippled = standard_ledger(node_public_key="n9SomeOtherNode")
    pinned = RippledTransport(
        rippled,
        RippledTransportConfig(
            source_id="local",
            expected_network_id=NETWORK_ID,
            expected_build_version="3.4.0",
            expected_node_public_key="n9TestLocalStandaloneNode",
        ),
    )
    with pytest.raises(TransportRefused) as caught:
        pinned.validated_ledger_identity()
    assert caught.value.code == "NODE_IDENTITY_MISMATCH"


def test_a04_transaction_outside_complete_history_is_refused() -> None:
    node = FakeQuickNode()
    observation = observed(node, signed_testnet_payment(), ledger_index=3_000)
    assert (observation.status, observation.code) == (
        HostedObservationStatus.REFUSED,
        "LEDGER_OUTSIDE_COMPLETE_HISTORY",
    )
    gap = FakeQuickNode(complete_ledgers="4000-4500,4600-5000")
    assert observed(gap, signed_testnet_payment(), ledger_index=4_550).code == (
        "LEDGER_OUTSIDE_COMPLETE_HISTORY"
    )
    assert observed(gap, signed_testnet_payment(sequence=10), ledger_index=4_700).status is (
        HostedObservationStatus.VALIDATED
    )


def test_a04_validated_ledger_outside_history_and_empty_history_are_refused() -> None:
    for history in ("4000-4999", "empty"):
        node = FakeQuickNode(complete_ledgers=history)
        assert raised(transport(node).validated_ledger_identity).code == (
            "VALIDATED_LEDGER_OUTSIDE_COMPLETE_HISTORY"
        )


def test_a04_not_found_never_proves_non_inclusion() -> None:
    node = FakeQuickNode()
    reader = transport(node)
    missing = "AB" * 32
    observation = reader.observe_payment(missing)
    assert observation.status is HostedObservationStatus.NOT_FOUND
    view = QuickNodeReconciliationView(reader, journal_network_id=TESTNET_ID)
    result = view.lookup(missing)
    assert (result.disposition, result.detail) == (
        LookupDisposition.UNAVAILABLE,
        "QUICKNODE_NON_INCLUSION_UNSUPPORTED",
    )
    with pytest.raises(LookupUnavailable):
        view.validated_ledger(SENDER.account)


def _json_fault(node: FakeQuickNode, change: Any) -> None:
    real = node._tx

    def fault(request: dict[str, Any]) -> Any:
        document = real(request)
        if not request["binary"]:
            change(document["result"])
        return document

    node.faults["tx"] = fault


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (lambda r: r["meta"].update(delivered_amount=issued("0.1")), "EVIDENCE_INCONSISTENT"),
        (lambda r: r["meta"].update(TransactionResult="tecPATH_DRY"), "EVIDENCE_INCONSISTENT"),
        (lambda r: r["tx_json"].update(Destination=SENDER.account), "EVIDENCE_INCONSISTENT"),
        (lambda r: r["tx_json"].update(DeliverMax=issued("9")), "EVIDENCE_INCONSISTENT"),
        (lambda r: r.update(ledger_index=VALIDATED - 11), "EVIDENCE_INCONSISTENT"),
        (lambda r: r.update(validated=False), "EVIDENCE_INCONSISTENT"),
        (lambda r: r.update(hash="CD" * 32), "EVIDENCE_INCONSISTENT"),
        (lambda r: r.update(ledger_hash="EF" * 32), "LEDGER_HASH_MISMATCH"),
        (
            lambda r: r["meta"].update(delivered_amount="unavailable"),
            "DELIVERED_AMOUNT_UNAVAILABLE",
        ),
    ],
)
def test_a05_contradictory_binary_and_json_evidence(change: Any, code: str) -> None:
    node = FakeQuickNode()
    _json_fault(node, change)
    observation = observed(node, signed_testnet_payment())
    assert (observation.status, observation.code) == (HostedObservationStatus.REFUSED, code)
    assert observation.evidence == b""


def test_a05_blob_that_does_not_hash_to_the_request_is_refused() -> None:
    node = FakeQuickNode()
    other = signed_testnet_payment(sequence=99)
    tx_hash = node.include(signed_testnet_payment())
    node.faults["tx"] = lambda request: {
        "result": {"hash": request["transaction"], "tx_blob": other.hex(), "validated": True}
    }
    assert transport(node).observe_payment(tx_hash).code == "EVIDENCE_HASH_MISMATCH"


def test_a05_failed_payment_reporting_a_delivery_is_refused() -> None:
    node = FakeQuickNode()
    observation = observed(
        node, signed_testnet_payment(), result="tecPATH_PARTIAL", delivered_binary=issued("2.5")
    )
    assert observation.code == "EVIDENCE_INCONSISTENT"


def test_a06_incorrect_delivered_amount_or_issuer_is_unexpected_delivery() -> None:
    cases = [
        (issued("0.1"), "DELIVERED_AMOUNT_MISMATCH", 100_000),
        (issued("2.5", issuer=OTHER_ISSUER.account), "DELIVERED_ASSET_MISMATCH", None),
        ("2500000", "DELIVERED_ASSET_MISMATCH", None),
        (issued("2.5000005"), "DELIVERED_AMOUNT_MISMATCH", None),
    ]
    for index, (delivered, code, micro) in enumerate(cases):
        node = FakeQuickNode()
        blob = signed_testnet_payment(sequence=20 + index)
        observation = observed(node, blob, delivered_binary=delivered)
        assert observation.status is HostedObservationStatus.VALIDATED
        assessment = assess_hosted_payment(observation, expectation(observation.transaction_hash))
        assert assessment.disposition is VerificationDisposition.VALIDATED_UNEXPECTED_DELIVERY
        assert (assessment.code, assessment.delivered_amount_micro_units) == (code, micro)
        assert assessment.settlement_authority is False


def test_a06_assessment_success_failure_and_intent_contradiction() -> None:
    node = FakeQuickNode()
    good = observed(node, signed_testnet_payment())
    success = assess_hosted_payment(good, expectation(good.transaction_hash))
    assert (success.disposition, success.delivered_amount_micro_units) == (
        VerificationDisposition.VALIDATED_SUCCESS,
        2_500_000,
    )
    assert success.settlement_authority is False
    contradicted = assess_hosted_payment(
        good, expectation(good.transaction_hash, amount_micro_units=2_000_000)
    )
    assert (contradicted.disposition, contradicted.code) == (
        VerificationDisposition.UNRESOLVED,
        "AMOUNT_MISMATCH",
    )
    failed = observed(FakeQuickNode(), signed_testnet_payment(sequence=30), result="tecPATH_DRY")
    assessment = assess_hosted_payment(failed, expectation(failed.transaction_hash))
    assert (assessment.disposition, assessment.code) == (
        VerificationDisposition.VALIDATED_FAILURE,
        "tecPATH_DRY",
    )


def test_a06_partial_payment_without_delivered_amount_is_refused() -> None:
    blob = signed_testnet_payment(flags=0x00020000)
    observation = observed(FakeQuickNode(), blob, delivered_json=issued("2.5"))
    assert observation.code == "DELIVERED_AMOUNT_UNAVAILABLE"
    partial = observed(FakeQuickNode(), blob, delivered_binary=issued("0.5"))
    assert partial.status is HostedObservationStatus.VALIDATED and partial.partial_payment
    assessment = assess_hosted_payment(partial, expectation(partial.transaction_hash))
    assert assessment.code == "DELIVERED_AMOUNT_MISMATCH"


def test_a07_unvalidated_transaction_is_provisional() -> None:
    node = FakeQuickNode()
    observation = observed(node, signed_testnet_payment(), validated=False, ledger_index=None)
    assert (observation.status, observation.code) == (
        HostedObservationStatus.PROVISIONAL,
        "NOT_VALIDATED",
    )
    assert observation.retryable
    assert (observation.delivered, observation.ledger_hash, observation.evidence) == (
        None,
        None,
        b"",
    )
    assessment = assess_hosted_payment(observation, expectation(observation.transaction_hash))
    assert assessment.disposition is VerificationDisposition.PROVISIONAL
    view = QuickNodeReconciliationView(transport(node), journal_network_id=TESTNET_ID)
    assert view.lookup(observation.transaction_hash).detail == "QUICKNODE_PROVISIONAL_UNSUPPORTED"


@pytest.mark.parametrize(
    ("method", "reply", "code"),
    [
        ("server_info", b"not json", "MALFORMED_RESPONSE"),
        ("server_info", b"[1, 2]", "MALFORMED_RESPONSE"),
        ("server_info", b'{"id": 1}', "MALFORMED_RESPONSE"),
        ("server_info", b'{"result": "x"}', "MALFORMED_RESPONSE"),
        ("server_info", b'{"result": {"status": "weird"}}', "MALFORMED_RESPONSE"),
        ("server_info", b'{"error": "noPermission"}', "RPC_noPermission"),
        ("server_info", b'{"result": {"status": "error", "error": "tooBusy"}}', "RPC_tooBusy"),
        ("ledger", b'{"result": {"validated": true, "ledger_index": 5000}}', "MALFORMED_RESPONSE"),
        (
            "tx",
            b'{"result": {"hash": "00", "tx_blob": "zz", "validated": true}}',
            "MALFORMED_RESPONSE",
        ),
        ("tx", b'{"result": {"status": "error", "error": "internal"}}', "RPC_internal"),
    ],
)
def test_a08_malformed_json_rpc_responses(method: str, reply: bytes, code: str) -> None:
    node = FakeQuickNode()
    tx_hash = node.include(signed_testnet_payment())
    node.faults[method] = lambda request: reply
    observation = transport(node).observe_payment(tx_hash)
    assert observation.code == code
    assert observation.status in (
        HostedObservationStatus.REFUSED,
        HostedObservationStatus.UNAVAILABLE,
    )
    assert observation.evidence == b""


def test_a08_malformed_metadata_blob_and_ledger_hash() -> None:
    node = FakeQuickNode()
    real = node._tx

    def bad_meta(request: dict[str, Any]) -> Any:
        document = real(request)
        if request["binary"]:
            document["result"]["meta_blob"] = "FFFF"
        return document

    node.faults["tx"] = bad_meta
    assert observed(node, signed_testnet_payment()).code == "MALFORMED_RESPONSE"
    node = FakeQuickNode()
    node.faults["server_info"] = lambda request: {
        "result": {
            "info": {
                "network_id": TESTNET_ID,
                "clio_version": "2.7.0",
                "complete_ledgers": HISTORY,
                "validated_ledger": {"seq": VALIDATED, "hash": "not-a-hash"},
            },
            "status": "success",
        }
    }
    assert raised(transport(node).validated_ledger_identity).code == "MALFORMED_RESPONSE"


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_a09_redirects_are_never_followed(status: int) -> None:
    rpc, handler, sleeps = client(
        [Reply(status=status, headers={"Location": "https://attacker.example/steal"}), ok()]
    )
    error = raised(lambda: rpc.call("server_info", {}))
    assert (error.code, type(error)) == ("REDIRECT_REFUSED", QuickNodeRefused)
    assert len(handler.requests) == 1  # the redirect target was never requested
    assert sleeps == []  # and the refusal is not retried


def test_a09_same_host_redirect_is_also_refused() -> None:
    rpc, handler, _ = client([Reply(status=302, headers={"Location": URL + "other"}), ok()])
    assert raised(lambda: rpc.call("server_info", {})).code == "REDIRECT_REFUSED"
    assert len(handler.requests) == 1


def test_a10_endpoint_substitution_is_impossible(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.attacker.example:3128")
    monkeypatch.setenv("https_proxy", "http://proxy.attacker.example:3128")
    rpc, handler, _ = client([ok(), ok()])
    rpc.call("server_info", {"url": "https://attacker.example/"})
    rpc.call("ledger", {"ledger_index": 1})
    assert [request.full_url for request in handler.requests] == [URL, URL]
    assert all(request.get_method() == "POST" for request in handler.requests)
    # Environment proxies are ignored: no proxy handler is installed and no request used one.
    assert not [h for h in rpc._opener.handlers if isinstance(h, urllib.request.ProxyHandler)]
    assert not any(request.has_proxy() for request in handler.requests)
    pinned = endpoint()
    with pytest.raises(AttributeError):
        pinned._url = "https://attacker.example/"  # type: ignore[misc]
    with pytest.raises(TypeError):
        pickle.dumps(pinned)
    with pytest.raises(QuickNodeConfigError, match="NETWORK_NOT_CONFIGURED"):
        QuickNodeConfig(endpoint=pinned, network="testnet")  # type: ignore[arg-type]


def test_a10_tls_is_verified() -> None:
    rpc, _, _ = client([])
    context = rpc.tls_context
    assert context.verify_mode is ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2
    failing, _, sleeps = client([urllib.error.URLError(ssl.SSLCertVerificationError("bad cert"))])
    assert raised(lambda: failing.call("server_info", {})).code == "TLS_VERIFICATION_FAILED"
    assert sleeps == []


def test_a11_request_timeout_is_strict_bounded_and_typed() -> None:
    rpc, handler, sleeps = client(
        [TimeoutError(), urllib.error.URLError(TimeoutError()), TimeoutError()],
        timeout_seconds=2.5,
    )
    error = raised(lambda: rpc.call("server_info", {}))
    assert isinstance(error, QuickNodeTimeout) and error.code == "TIMEOUT"
    assert handler.timeouts == [2.5, 2.5, 2.5]
    assert sleeps == [0.5, 1.0]
    node = FakeQuickNode()
    tx_hash = node.include(signed_testnet_payment())
    node.faults["tx"] = lambda request: QuickNodeTimeout("TIMEOUT", "tx")
    observation = transport(node).observe_payment(tx_hash)
    assert (observation.status, observation.code, observation.retryable) == (
        HostedObservationStatus.UNAVAILABLE,
        "TIMEOUT",
        True,
    )


def test_a12_rate_limiting_honors_retry_after_within_a_cap() -> None:
    rpc, _, sleeps = client([Reply(status=429, headers={"Retry-After": "2"}), ok()])
    assert json.loads(rpc.call("server_info", {}))["result"]["status"] == "success"
    assert sleeps == [2.0]
    capped, _, capped_sleeps = client(
        [Reply(status=429, headers={"Retry-After": "100"}) for _ in range(3)]
    )
    error = raised(lambda: capped.call("server_info", {}))
    assert isinstance(error, QuickNodeRateLimited) and error.code == "RATE_LIMITED"
    assert capped_sleeps == [5.0, 5.0]


def test_a12_server_errors_retry_and_auth_errors_do_not() -> None:
    rpc, _, sleeps = client([Reply(status=503), Reply(status=502), ok()])
    rpc.call("server_info", {})
    assert sleeps == [0.5, 1.0]
    denied, handler, denied_sleeps = client([Reply(status=401), ok()])
    error = raised(lambda: denied.call("server_info", {}))
    assert error.code == "ENDPOINT_AUTH_REJECTED" and not error.retryable
    assert (len(handler.requests), denied_sleeps) == (1, [])
    exhausted, _, _ = client([Reply(status=500)] * 3)
    assert isinstance(raised(lambda: exhausted.call("x", {})), QuickNodeUnavailable)


def test_a13_repeated_lookup_is_consistent() -> None:
    node = FakeQuickNode()
    reader = transport(node)
    tx_hash = node.include(signed_testnet_payment())
    first, second = reader.observe_payment(tx_hash), reader.observe_payment(tx_hash)
    assert first == second
    assert first.status is HostedObservationStatus.VALIDATED


def test_a13_ledger_hash_changing_between_reads_latches_closed() -> None:
    node = FakeQuickNode()
    reader = transport(node)
    tx_hash = node.include(signed_testnet_payment())
    assert reader.observe_payment(tx_hash).status is HostedObservationStatus.VALIDATED
    real = node._ledger

    def rewritten(request: dict[str, Any]) -> Any:
        document = real(request)
        if int(request["ledger_index"]) == VALIDATED - 10:
            document["result"]["ledger_hash"] = "AA" * 32
        return document

    node.faults["ledger"] = rewritten
    assert reader.observe_payment(tx_hash).code == "LEDGER_HASH_CONTRADICTION"
    node.faults.clear()
    assert reader.observe_payment(tx_hash).code == "SOURCE_CONTRADICTED"


def test_a13_transaction_that_disappears_or_changes_latches_closed() -> None:
    node = FakeQuickNode()
    reader = transport(node)
    tx_hash = node.include(signed_testnet_payment())
    reader.observe_payment(tx_hash)
    del node.transactions[tx_hash]
    assert reader.observe_payment(tx_hash).code == "TRANSACTION_OBSERVATION_CONTRADICTION"
    changed = FakeQuickNode()
    reader = transport(changed)
    tx_hash = changed.include(signed_testnet_payment())
    reader.observe_payment(tx_hash)
    changed.transactions[tx_hash].delivered_binary = issued("0.1")
    assert reader.observe_payment(tx_hash).code == "TRANSACTION_OBSERVATION_CONTRADICTION"


def test_a13_stale_and_backwards_validated_ledgers_are_unavailable() -> None:
    node = FakeQuickNode()
    reader = transport(node)
    reader.validated_ledger_identity()
    node.ledger_index = VALIDATED - 1  # a lagging backend behind the load balancer
    error = raised(reader.validated_ledger_identity)
    assert (error.code, error.retryable, reader.contradiction) == (
        "STALE_VALIDATED_LEDGER",
        True,
        None,
    )
    node.ledger_index, node.complete_ledgers = VALIDATED + 1, f"4000-{VALIDATED + 1}"
    assert reader.validated_ledger_identity().ledger_index == VALIDATED + 1
    old = FakeQuickNode(close_time=CLOSE_TIME - 600)
    assert raised(transport(old).validated_ledger_identity).code == "STALE_VALIDATED_LEDGER"
    future = FakeQuickNode(close_time=CLOSE_TIME + 600)
    assert raised(transport(future).validated_ledger_identity).code == "LEDGER_CLOSE_TIME_IN_FUTURE"


def test_a13_transaction_beyond_the_validated_identity_is_retryable_not_accepted() -> None:
    observation = observed(FakeQuickNode(), signed_testnet_payment(), ledger_index=VALIDATED + 3)
    assert (observation.status, observation.code, observation.retryable) == (
        HostedObservationStatus.UNAVAILABLE,
        "LEDGER_BEYOND_VALIDATED",
        True,
    )
    assert (observation.delivered, observation.evidence) == (None, b"")


def _restart(flow: Any, lookup: Any) -> PaymentService:
    return PaymentService(
        journal=PaymentJournal(flow.database),
        preparer=OfflinePaymentPreparer(maximum_fee_drops=100),
        inspector=XrplSignedPaymentInspector(),
        transport=flow.offline,
        lookup=lookup,
        ledger_observer=lookup,
    )


def test_a14_restart_with_an_unresolved_transaction(tmp_path: Path) -> None:
    flow = chain(tmp_path)
    node = FakeQuickNode()
    node.include(flow.signed)  # even if the hosted node reports the same bytes
    # Mis-wired on purpose: the view is declared for Testnet but the journal holds 21337 payments.
    view = QuickNodeReconciliationView(transport(node), journal_network_id=TESTNET_ID)
    hosted = _restart(flow, view)
    (record,) = hosted.reconcile_after_restart()
    assert (record.state, record.last_provisional_status) == (
        TransactionState.UNKNOWN,
        "QUICKNODE_TRANSACTION_NETWORK_MISMATCH",
    )
    assert record.final_evidence is None
    assert outbox_events(flow.database) == 0
    assert hosted.reconcile_after_restart()[0].state is TransactionState.UNKNOWN
    assert len(flow.offline.submitted) == 1  # nothing was resubmitted
    # The authorized local transport later reconciles the same entry normally.
    flow.rippled.include(flow.signed, ledger_index=VALIDATED_LEDGER + 1, delivered=trlusd("2.5"))
    flow.rippled.advance(VALIDATED_LEDGER + 1)
    trusted = RippledTransport(
        flow.rippled,
        RippledTransportConfig(
            source_id="local-rippled",
            expected_network_id=NETWORK_ID,
            expected_build_version="3.4.0",
        ),
    )
    assert _restart(flow, trusted).reconcile(OPERATION).state is TransactionState.VALIDATED_SUCCESS
    assert outbox_events(flow.database) == 1


def test_a14_validated_hosted_evidence_never_finalizes_a_journal(tmp_path: Path) -> None:
    flow = chain(tmp_path)
    node = FakeQuickNode()
    blob = signed_testnet_payment()
    tx_hash = node.include(blob)
    view = QuickNodeReconciliationView(transport(node), journal_network_id=TESTNET_ID)
    result = view.lookup(tx_hash)
    assert (result.disposition, result.detail, result.payment) == (
        LookupDisposition.UNAVAILABLE,
        "QUICKNODE_FINALITY_UNSUPPORTED",
        None,
    )
    assert view.last_observation is not None
    assert view.last_observation.status is HostedObservationStatus.VALIDATED
    hosted = _restart(flow, view)
    assert hosted.reconcile(OPERATION).state is TransactionState.UNKNOWN
    assert outbox_events(flow.database) == 0


def test_a15_testnet_evidence_is_never_local_21337_evidence(tmp_path: Path) -> None:
    reader = transport()
    with pytest.raises(NetworkIsolationError) as caught:
        QuickNodeReconciliationView(reader, journal_network_id=NETWORK_ID)
    assert caught.value.code == "NETWORK_ISOLATION"
    node = FakeQuickNode()
    observation = observed(node, signed_testnet_payment())
    local = assess_hosted_payment(
        observation, expectation(observation.transaction_hash, network_id=NETWORK_ID)
    )
    assert (local.disposition, local.code) == (
        VerificationDisposition.UNRESOLVED,
        "NETWORK_MISMATCH",
    )
    # Hosted evidence offered to the X0t verifier for a 21337 journal entry is not accepted.
    flow = chain(tmp_path)
    record = flow.service.status.status(OPERATION)
    forged = LedgerPayment(
        transaction_hash=record.transaction_hash,
        validated=True,
        result="tesSUCCESS",
        transaction_type=TransactionType.PAYMENT,
        account=record.account,
        destination=record.destination,
        asset=record.asset,
        amount_micro_units=record.amount_micro_units,
        delivered_amount_micro_units=record.amount_micro_units,
        fee_drops=record.fee_drops,
        network_id=record.network_id,
        last_ledger_sequence=record.last_ledger_sequence,
        sequence=record.sequence,
        ticket_sequence=record.ticket_sequence,
        ledger_index=observation.ledger_index,
        ledger_hash=observation.ledger_hash,
        evidence=observation.evidence,
    )
    verdict = PaymentResultVerifier().verify(record, forged)
    assert (verdict.disposition, verdict.result_code) == (
        VerificationDisposition.UNRESOLVED,
        "FINAL_EVIDENCE_INVALID",
    )


def test_a15_transaction_naming_another_network_is_refused() -> None:
    for network_id in (NETWORK_ID, 1):
        observation = observed(FakeQuickNode(), signed_testnet_payment(network_id=network_id))
        assert observation.code == "TRANSACTION_NETWORK_MISMATCH"


def test_a15_transport_cannot_serve_any_x0t_or_signer_seam() -> None:
    reader = transport()
    for name in ("submit", "lookup", "validated_ledger", "ledger_context", "offline_only"):
        assert not hasattr(reader, name)
    with pytest.raises(ValueError, match="offline submission transports only"):
        PaymentService(
            journal=None,  # type: ignore[arg-type]
            preparer=None,  # type: ignore[arg-type]
            inspector=None,  # type: ignore[arg-type]
            transport=reader,  # type: ignore[arg-type]
            lookup=reader,  # type: ignore[arg-type]
            ledger_observer=reader,  # type: ignore[arg-type]
        )


def _errors_for_redaction() -> list[QuickNodeError]:
    errors = []
    for reply in (
        Reply(status=302, headers={"Location": "https://attacker.example/"}),
        Reply(status=401),
        Reply(status=500),
        Reply(status=429),
        TimeoutError(),
        urllib.error.URLError(ssl.SSLCertVerificationError("bad cert")),
        urllib.error.URLError(OSError("nodename nor servname provided")),
    ):
        rpc, _, _ = client([reply], max_attempts=1)
        errors.append(raised(lambda rpc=rpc: rpc.call("server_info", {})))
    return errors


def test_a16_credentials_are_redacted_everywhere() -> None:
    node = FakeQuickNode()
    funded_sender(node)
    reader = transport(node)
    rpc, _, _ = client([])
    for text in (repr(endpoint()), str(endpoint()), repr(config()), repr(rpc), repr(reader)):
        no_secret(text)
        assert endpoint().fingerprint in text
    for error in _errors_for_redaction():
        no_secret(str(error))
        no_secret(repr(error))
        assert error.__cause__ is None and error.__context__ is None
    observation = observed(node, signed_testnet_payment())
    no_secret(observation.evidence.decode())
    for read in reader.recent_reads():
        no_secret(read.params + read.response.decode())


def test_a16_probe_report_is_sanitized_and_pending_without_an_endpoint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    node = FakeQuickNode(node_keys=["n9BackendA", "n9BackendB"])
    funded_sender(node)
    tx_hash = node.include(signed_testnet_payment())
    report = run_probe(
        transport(node), account=SENDER.account, transaction_hash=tx_hash, node_samples=4
    )
    text = json.dumps(report)
    no_secret(text)
    assert {name: check["status"] for name, check in report["checks"].items()} == {
        "validated_ledger_identity": "PASS",
        "node_identity_stability": "PASS",
        "account_reads": "PASS",
        "transaction_lookup": "PASS",
    }
    assert report["checks"]["node_identity_stability"]["distinct_node_public_keys"] == [
        "n9BackendA",
        "n9BackendB",
    ]
    shapes = json.dumps(report["response_shapes"])
    assert SENDER.account not in shapes and ledger_hash_for(VALIDATED) not in shapes
    assert set(report["response_shapes"]) >= {"server_info", "server_state", "tx:binary", "tx:json"}
    skipped = run_probe(transport(FakeQuickNode()), account=None, transaction_hash=None)
    assert skipped["checks"]["transaction_lookup"]["status"] == "NOT_RUN"
    for name in ("CREDENCE_QUICKNODE_XRPL_URL", "CREDENCE_QUICKNODE_XRPL_NETWORK"):
        monkeypatch.delenv(name, raising=False)
    assert probe_main([]) == 2
    assert "PENDING" in capsys.readouterr().err
    monkeypatch.setenv("CREDENCE_QUICKNODE_XRPL_URL", URL)
    monkeypatch.setenv("CREDENCE_QUICKNODE_XRPL_NETWORK", "mainnet")
    assert probe_main([]) == 2
    captured = capsys.readouterr()
    no_secret(captured.err + captured.out)


_ENDPOINT_TOKEN = re.compile(r"quiknode\.pro/[A-Za-z0-9_-]{16,}")


def test_a16_no_quicknode_endpoint_token_is_committed() -> None:
    root = Path(__file__).resolve().parents[2]
    scanned = 0
    for directory in ("credence", "tests", "contracts", "docs", "spike", "infra", "scripts"):
        for path in (root / directory).rglob("*"):
            if path.is_file() and path.suffix in {".py", ".md", ".json", ".toml", ".cfg", ".yml"}:
                scanned += 1
                assert not _ENDPOINT_TOKEN.search(path.read_text(encoding="utf-8", errors="ignore"))
    assert scanned > 100
