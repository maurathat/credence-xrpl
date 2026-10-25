"""X1s: QuickNode transport hardening against Salvador's X1r findings Q1-Q4. Offline.

Q1 hostile responses are contained as typed results; Q2 every request has an absolute deadline;
Q3 the endpoint token never reaches tracebacks, frame locals, exception chains or logs; Q4 the
probe CLI reports failures instead of crashing. HTTP cases use real loopback sockets only.
"""

from __future__ import annotations

import copy
import json
import logging
import socket
import ssl
import time
import traceback
from pathlib import Path
from typing import Any

import pytest
from test_xrpl_x1c_remediation import ASSET, REVIEW_AMOUNT, chain, outbox_events
from test_xrpl_x1q_quicknode import (
    _errors_for_redaction,
    _json_fault,
    client,
    expectation,
    observed,
    ok,
    raised,
    transport,
)
from x1a_world import (
    LAST_LEDGER,
    NETWORK_ID,
    OWNER,
    PROVIDER_A,
    TICKET,
    VALIDATED_LEDGER,
    direct_policy_signature,
    researchbot_signature,
    unsigned_payment,
)
from x1q_quicknode import (
    HISTORY,
    HOST,
    SENDER,
    TESTNET_ID,
    TOKEN,
    URL,
    VALIDATED,
    Drip,
    FakeQuickNode,
    LoopbackQuickNode,
    Raw,
    Reply,
    Stall,
    Wiring,
    config,
    endpoint,
    funded_sender,
    issued,
    signed_testnet_payment,
)

from credence.xrpl_adapter import quicknode
from credence.xrpl_adapter.journal import PaymentJournal
from credence.xrpl_adapter.multisign import assemble_multisigned
from credence.xrpl_adapter.preparation import OfflinePaymentPreparer, XrplSignedPaymentInspector
from credence.xrpl_adapter.quicknode import (
    MAX_JSON_DEPTH,
    HostedObservationStatus,
    QuickNodeConfigError,
    QuickNodeError,
    QuickNodeJsonRpcClient,
    QuickNodeRateLimited,
    QuickNodeReconciliationView,
    QuickNodeRefused,
    QuickNodeTimeout,
    QuickNodeUnavailable,
    QuickNodeXRPLTransport,
    assess_hosted_payment,
    parse_json_bounded,
    resolve_with_deadline,
)
from credence.xrpl_adapter.quicknode_probe import (
    EXIT_FAILED,
    EXIT_INCOMPLETE,
    EXIT_NOT_CONFIGURED,
    EXIT_PASS,
    run_probe,
)
from credence.xrpl_adapter.quicknode_probe import main as probe_main
from credence.xrpl_adapter.service import PaymentService
from credence.xrpl_adapter.types import (
    LookupDisposition,
    PaymentRequest,
    SubmissionResponse,
    TransactionState,
    VerificationDisposition,
)

NESTED = b"[" * 200_000 + b"]" * 200_000
EXTREME = "1E+999999999"


def chain_of(error: BaseException) -> list[BaseException]:
    found: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and len(found) < 20:
        found.append(current)
        current = current.__cause__ or current.__context__
    return found


def assert_clean(text: str) -> None:
    assert TOKEN not in text
    assert HOST not in text


# --- Q1: hostile responses are contained ---------------------------------------------------------


@pytest.mark.parametrize("deliver_max", ["9" * 5000, issued("9" * 5000)], ids=["xrp", "issued"])
def test_q1_5000_digit_deliver_max_is_refused(deliver_max: object) -> None:
    node = FakeQuickNode()
    amount = "1500000" if isinstance(deliver_max, str) else None
    _json_fault(node, lambda result: result["tx_json"].update(DeliverMax=deliver_max))
    observation = observed(node, signed_testnet_payment(amount=amount))
    assert (observation.status, observation.code) == (
        HostedObservationStatus.REFUSED,
        "MALFORMED_AMOUNT",
    )
    assert observation.evidence == b"" and observation.delivered is None


@pytest.mark.parametrize("value", [EXTREME, "1E-999999999", "1e96", "12345678901234567", "1" * 49])
def test_q1_extreme_delivered_value_is_refused(value: str) -> None:
    node = FakeQuickNode()
    _json_fault(node, lambda result: result["meta"].update(delivered_amount=issued(value)))
    observation = observed(node, signed_testnet_payment())
    assert (observation.status, observation.code) == (
        HostedObservationStatus.REFUSED,
        "MALFORMED_AMOUNT",
    )


def test_q1_extreme_values_elsewhere_are_refused() -> None:
    node = FakeQuickNode()
    funded_sender(node)
    node.lines[SENDER.account][0]["balance"] = EXTREME
    reader = transport(node)
    identity = reader.validated_ledger_identity()
    assert raised(lambda: reader.account_lines(SENDER.account, identity)).code == (
        "MALFORMED_RESPONSE"
    )
    for field, value in (("seq", "9" * 5000), ("seq", 10**30)):
        node = FakeQuickNode()
        real = node._server_info

        def info(
            request: dict[str, Any], field: str = field, value: object = value, real: Any = real
        ) -> Any:
            document = real(request)
            document["result"]["info"]["validated_ledger"][field] = value
            return document

        node.faults["server_info"] = info
        assert raised(transport(node).validated_ledger_identity).code in (
            "MALFORMED_RESPONSE",
            "NUMBER_OUT_OF_RANGE",
        )
    node = FakeQuickNode(complete_ledgers="1-" + "9" * 5000)
    assert raised(transport(node).validated_ledger_identity).code == "MALFORMED_COMPLETE_LEDGERS"


@pytest.mark.parametrize("method", ["server_info", "ledger", "tx"])
def test_q1_excessively_nested_json_is_refused_before_parsing(method: str) -> None:
    node = FakeQuickNode()
    tx_hash = node.include(signed_testnet_payment())
    node.faults[method] = lambda request: NESTED
    started = time.monotonic()
    observation = transport(node).observe_payment(tx_hash)
    assert (observation.status, observation.code) == (
        HostedObservationStatus.REFUSED,
        "RESPONSE_TOO_DEEP",
    )
    assert time.monotonic() - started < 1.0
    if method == "server_info":
        error = raised(transport(node).validated_ledger_identity)
        assert error.code == "RESPONSE_TOO_DEEP"
        assert error.__cause__ is None and error.__context__ is None


def test_q1_nesting_number_and_size_limits() -> None:
    at_limit = b"[" * MAX_JSON_DEPTH + b"]" * MAX_JSON_DEPTH
    assert parse_json_bounded(at_limit) is not None
    over = b"[" * (MAX_JSON_DEPTH + 1) + b"]" * (MAX_JSON_DEPTH + 1)
    strings = json.dumps({"a": "[" * 70 + '\\"' + "{" * 70}).encode()
    assert parse_json_bounded(strings) == {"a": "[" * 70 + '\\"' + "{" * 70}
    cases = {
        over: "RESPONSE_TOO_DEEP",
        b"[}": "MALFORMED_RESPONSE",
        b'{"n": 1' + b"0" * 30 + b"}": "NUMBER_OUT_OF_RANGE",
        b'{"n": 1e999}': "NUMBER_OUT_OF_RANGE",
        b'{"n": NaN}': "NUMBER_OUT_OF_RANGE",
        b'{"n": -Infinity}': "NUMBER_OUT_OF_RANGE",
        b"\xff\xfe": "MALFORMED_RESPONSE",
        b"": "MALFORMED_RESPONSE",
    }
    for raw, code in cases.items():
        with pytest.raises(QuickNodeRefused) as caught:
            parse_json_bounded(raw)
        assert caught.value.code == code
        assert caught.value.__context__ is None
    with pytest.raises(QuickNodeRefused, match="RESPONSE_TOO_LARGE"):
        parse_json_bounded(b" " * (quicknode.MAX_RESPONSE_BYTES + 1))


def test_q1_oversized_blobs_and_error_tokens_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(quicknode, "MAX_BLOB_HEX_CHARS", 64)
    observation = observed(FakeQuickNode(), signed_testnet_payment())
    assert observation.code == "MALFORMED_RESPONSE"
    node = FakeQuickNode()
    tx_hash = node.include(signed_testnet_payment())
    node.faults["tx"] = lambda request: {"result": {"status": "error", "error": {"x": "y" * 999}}}
    assert transport(node).observe_payment(tx_hash).code == "RPC_malformedError"


@pytest.mark.parametrize(
    "data",
    [
        b"HTTP/1.1 OK-but-not-a-status\r\n\r\n",
        b"SSH-2.0-OpenSSH_9.6\r\n",
        b"HTTP/1.1 200 OK\r\nX-Long: " + b"a" * 70_000 + b"\r\n\r\n",
        b"HTTP/1.1 200 OK\r\n" + b"X-H: v\r\n" * 150 + b"\r\n",
    ],
    ids=["bad-status", "not-http", "long-header", "too-many-headers"],
)
def test_q1_malformed_http_response_is_typed_and_not_retried(data: bytes) -> None:
    rpc, server, sleeps = client([Raw(data), ok()])
    error = raised(lambda: rpc.call("server_info", {}))
    assert (type(error), error.code) == (QuickNodeRefused, "MALFORMED_HTTP_RESPONSE")
    assert (len(server.requests), sleeps) == (1, [])
    assert error.__cause__ is None and error.__context__ is None


def test_q1_premature_close_and_oversized_bodies(monkeypatch: pytest.MonkeyPatch) -> None:
    short = Raw(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n0123456789")
    rpc, _, _ = client([short], max_attempts=1)
    error = raised(lambda: rpc.call("server_info", {}))
    assert (type(error), error.code) == (QuickNodeUnavailable, "CONNECTION_CLOSED")
    declared = Raw(b"HTTP/1.1 200 OK\r\nContent-Length: 999999999999\r\n\r\n{}")
    rpc, _, _ = client([declared])
    assert raised(lambda: rpc.call("server_info", {})).code == "RESPONSE_TOO_LARGE"
    monkeypatch.setattr(quicknode, "MAX_RESPONSE_BYTES", 1000)
    streamed = Raw(b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n" + b" " * 5000)
    rpc, _, _ = client([streamed])
    assert raised(lambda: rpc.call("server_info", {})).code == "RESPONSE_TOO_LARGE"


def test_q1_unanticipated_exceptions_are_typed_and_unchained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(*args: object) -> object:
        raise ZeroDivisionError("an edge nobody bounded")

    node = FakeQuickNode()
    tx_hash = node.include(signed_testnet_payment())
    monkeypatch.setattr(quicknode, "_delivered", explode)
    observation = transport(node).observe_payment(tx_hash)
    assert (observation.status, observation.code) == (
        HostedObservationStatus.REFUSED,
        "RESPONSE_PROCESSING_FAILED",
    )
    monkeypatch.setattr(quicknode, "_ranges", explode)
    error = raised(transport(FakeQuickNode()).validated_ledger_identity)
    assert error.code == "RESPONSE_PROCESSING_FAILED"
    assert error.__cause__ is None and error.__context__ is None


def test_q1_malformed_observation_has_no_settlement_authority() -> None:
    node = FakeQuickNode()
    _json_fault(node, lambda result: result["meta"].update(delivered_amount=issued(EXTREME)))
    observation = observed(node, signed_testnet_payment())
    assessment = assess_hosted_payment(observation, expectation(observation.transaction_hash))
    assert (assessment.disposition, assessment.settlement_authority) == (
        VerificationDisposition.UNRESOLVED,
        False,
    )
    view = QuickNodeReconciliationView(transport(node), journal_network_id=TESTNET_ID)
    result = view.lookup(observation.transaction_hash)
    assert (result.disposition, result.payment) == (LookupDisposition.UNAVAILABLE, None)


def _second_pending_entry(flow: Any) -> bytes:
    transaction = unsigned_payment(
        amount_micro_units=REVIEW_AMOUNT, sequence=None, ticket_sequence=TICKET
    )
    signed = assemble_multisigned(
        transaction, [researchbot_signature(transaction), direct_policy_signature(transaction)]
    )
    flow.service.prepare_and_persist(
        PaymentRequest(
            operation_id="operation-x1s-second",
            payment_authorization_id="authorization-x1s-second",
            account=OWNER.account,
            destination=PROVIDER_A.account,
            asset=ASSET,
            amount_micro_units=REVIEW_AMOUNT,
            fee_drops=30,
            network_id=NETWORK_ID,
            last_ledger_sequence=LAST_LEDGER,
            earliest_ledger_sequence=VALIDATED_LEDGER,
            ticket_sequence=TICKET,
        ),
        signed,
    )
    flow.offline.queue_submission(SubmissionResponse(True, "tesSUCCESS", "queued offline"))
    assert flow.service.submit("operation-x1s-second").state is (
        TransactionState.PENDING_CONFIRMATION
    )
    return signed


def test_q1_one_hostile_observation_does_not_block_other_entries(tmp_path: Path) -> None:
    flow = chain(tmp_path)
    second = _second_pending_entry(flow)
    node = FakeQuickNode()
    first_hash = node.include(flow.signed)
    node.include(second)
    real = node._tx
    node.faults["tx"] = lambda request: (
        NESTED if request["transaction"] == first_hash else real(request)
    )
    view = QuickNodeReconciliationView(transport(node), journal_network_id=TESTNET_ID)
    restarted = PaymentService(
        journal=PaymentJournal(flow.database),
        preparer=OfflinePaymentPreparer(maximum_fee_drops=100),
        inspector=XrplSignedPaymentInspector(),
        transport=flow.offline,
        lookup=view,
        ledger_observer=view,
    )
    records = {record.operation_id: record for record in restarted.reconcile_after_restart()}
    assert {key: (r.state, r.last_provisional_status) for key, r in records.items()} == {
        "operation-x1c": (TransactionState.UNKNOWN, "QUICKNODE_RESPONSE_TOO_DEEP"),
        "operation-x1s-second": (
            TransactionState.UNKNOWN,
            "QUICKNODE_TRANSACTION_NETWORK_MISMATCH",
        ),
    }
    assert outbox_events(flow.database) == 0


# --- Q2: absolute request deadlines --------------------------------------------------------------


def test_q2_dripping_body_hits_the_absolute_deadline() -> None:
    rpc, server, _ = client([Drip()], timeout_seconds=1.0, max_attempts=1)
    started = time.monotonic()
    error = raised(lambda: rpc.call("server_info", {}))
    elapsed = time.monotonic() - started
    assert (type(error), error.code) == (QuickNodeTimeout, "TIMEOUT")
    assert 0.9 <= elapsed < 1.6
    for _ in range(40):  # the server sees the connection closed shortly after the deadline
        if server.closed:
            break
        time.sleep(0.05)
    assert server.closed and server.closed[0] < elapsed + 1.0


def test_q2_dripping_headers_hit_the_absolute_deadline() -> None:
    rpc, _, _ = client([Drip(drip_headers=True)], timeout_seconds=0.6, max_attempts=1)
    started = time.monotonic()
    assert raised(lambda: rpc.call("server_info", {})).code == "TIMEOUT"
    assert time.monotonic() - started < 1.2


def test_q2_response_completing_just_inside_the_deadline_succeeds() -> None:
    body = b'{"result":{"status":"success"}}'
    rpc, _, _ = client([Drip(body=body, interval=0.02)], timeout_seconds=1.5, max_attempts=1)
    started = time.monotonic()
    assert rpc.call("server_info", {}) == body
    assert 0.5 <= time.monotonic() - started < 1.5


def test_q2_retry_exhaustion_is_bounded_by_the_total_budget() -> None:
    rpc, server, _ = client(
        [Stall()] * 5, timeout_seconds=0.4, max_attempts=5, total_budget_seconds=1.0
    )
    started = time.monotonic()
    error = raised(lambda: rpc.call("server_info", {}))
    elapsed = time.monotonic() - started
    assert (type(error), error.code) == (QuickNodeTimeout, "TIMEOUT")
    assert elapsed < 1.6
    assert len(server.requests) <= 3


def test_q2_backoff_never_sleeps_past_the_budget() -> None:
    rpc, server, sleeps = client(
        [Reply(status=429, headers={"Retry-After": "5"}), ok()],
        timeout_seconds=1.0,
        total_budget_seconds=2.0,
    )
    error = raised(lambda: rpc.call("server_info", {}))
    assert isinstance(error, QuickNodeRateLimited)
    assert (sleeps, len(server.requests)) == ([], 1)


def test_q2_dns_resolution_is_bounded_for_the_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    def slow(*args: object, **kwargs: object) -> object:
        time.sleep(2.0)
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 9))]

    monkeypatch.setattr(quicknode.socket, "getaddrinfo", slow)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        resolve_with_deadline(HOST, 443, 0.2)
    assert time.monotonic() - started < 0.6
    rpc = QuickNodeJsonRpcClient(config(timeout_seconds=0.3, max_attempts=1))
    started = time.monotonic()
    assert raised(lambda: rpc.call("server_info", {})).code == "TIMEOUT"
    assert time.monotonic() - started < 0.8


def test_q2_stalled_tls_handshake_hits_the_deadline() -> None:
    server = LoopbackQuickNode([Stall()])
    wiring = Wiring(server)
    rpc = QuickNodeJsonRpcClient(
        config(timeout_seconds=0.4, max_attempts=1), resolver=wiring.resolver
    )
    started = time.monotonic()
    assert raised(lambda: rpc.call("server_info", {})).code == "TIMEOUT"
    assert time.monotonic() - started < 1.0


def test_q2_default_path_always_negotiates_verified_tls() -> None:
    server = LoopbackQuickNode([Raw(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")])
    wiring = Wiring(server)
    rpc = QuickNodeJsonRpcClient(config(max_attempts=1), resolver=wiring.resolver)
    hostnames: list[str | None] = []
    real_wrap = rpc.tls_context.wrap_socket

    def recording_wrap(sock: socket.socket, **kwargs: Any) -> ssl.SSLSocket:
        hostnames.append(kwargs.get("server_hostname"))
        return real_wrap(sock, **kwargs)

    rpc.tls_context.wrap_socket = recording_wrap  # type: ignore[method-assign]
    assert raised(lambda: rpc.call("server_info", {})).code == "TLS_VERIFICATION_FAILED"
    assert hostnames == [HOST]
    assert [seen.method for seen in server.requests] == ["<not http>"]  # no plaintext request


def test_q2_budget_configuration_bounds() -> None:
    for changes in (
        {"timeout_seconds": 5.0, "total_budget_seconds": 4.0},
        {"total_budget_seconds": 121},
    ):
        with pytest.raises(QuickNodeConfigError, match="BUDGET_OUT_OF_RANGE"):
            config(**changes)


# --- Q3: credential-safe diagnostics -------------------------------------------------------------


def _hostile_errors() -> list[QuickNodeError]:
    errors = _errors_for_redaction()
    rpc, _, _ = client([Drip()], timeout_seconds=0.3, max_attempts=1)
    errors.append(raised(lambda: rpc.call("server_info", {})))
    return errors


def test_q3_tracebacks_with_locals_never_contain_the_token() -> None:
    errors = _hostile_errors()
    assert len(errors) == 9
    for error in errors:
        assert error.__cause__ is None and error.__context__ is None
        plain = "".join(traceback.format_exception(error))
        with_locals = "".join(
            traceback.TracebackException.from_exception(error, capture_locals=True).format()
        )
        assert_clean(plain)
        assert_clean(with_locals)
        assert "_exchange" not in with_locals  # the frame that held the target is never captured


def test_q3_crash_reporter_frame_inspection_finds_no_token() -> None:
    rpc, server, _ = client([Raw(b"HTTP/1.1 OK-but-not-a-status\r\n\r\n")])
    try:
        rpc.call("server_info", {})
    except QuickNodeError as error:
        captured = error
    assert server.requests[0].target == f"/{TOKEN}/"  # the credential really was sent
    inspected = 0
    # Every frame from the catching frame's callee down to the raise (the catcher's own locals,
    # here the fake remote server that recorded what it received, are the caller's business).
    assert captured.__traceback__ is not None
    tb = captured.__traceback__.tb_next
    while tb is not None:
        for name, value in tb.tb_frame.f_locals.items():
            inspected += 1
            assert_clean(f"{name}={value!r}")
            for attribute in getattr(value, "__dict__", {}).values():
                assert_clean(repr(attribute))
        tb = tb.tb_next
    assert inspected > 0


def test_q3_caller_chained_exceptions_stay_clean() -> None:
    rpc, _, _ = client([Reply(status=401)])
    try:
        try:
            rpc.call("server_info", {})
        except QuickNodeError as error:
            raise RuntimeError("caller wraps the failure") from error
    except RuntimeError as wrapped:
        rendered = "".join(
            traceback.TracebackException.from_exception(wrapped, capture_locals=True).format()
        )
    assert "ENDPOINT_AUTH_REJECTED" in rendered
    assert_clean(rendered)


def test_q3_no_request_logging_or_debug_output(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    logging.getLogger().setLevel(logging.DEBUG)
    rpc, _, _ = client([ok(), Reply(status=500)], max_attempts=1)
    rpc.call("server_info", {})
    raised(lambda: rpc.call("server_info", {}))
    captured = capsys.readouterr()
    assert_clean(captured.out + captured.err)
    assert_clean(" ".join(record.getMessage() for record in caplog.records))


def test_q3_transport_refusals_are_unchained_and_evidence_free() -> None:
    node = FakeQuickNode()
    tx_hash = node.include(signed_testnet_payment())
    node.faults["server_info"] = lambda request: b"not json"
    error = raised(transport(node).validated_ledger_identity)
    assert error.code == "MALFORMED_RESPONSE" and error.__context__ is None
    observation = transport(node).observe_payment(tx_hash)
    assert observation.evidence == b""
    for member in chain_of(error):
        assert_clean(repr(member))


def test_q3_endpoint_secret_is_not_reachable_through_generic_introspection() -> None:
    pinned = endpoint()
    with pytest.raises(TypeError):
        vars(pinned)
    with pytest.raises(TypeError):
        copy.copy(pinned)
    assert_clean(repr(pinned) + str(pinned) + repr(config()))


# --- Q4: probe CLI robustness --------------------------------------------------------------------


def _hostile_nodes() -> dict[str, tuple[FakeQuickNode, str]]:
    nodes: dict[str, tuple[FakeQuickNode, str]] = {}
    node = FakeQuickNode()
    _json_fault(node, lambda result: result["meta"].update(delivered_amount=issued(EXTREME)))
    nodes["extreme-exponent"] = (node, node.include(signed_testnet_payment()))
    node = FakeQuickNode()
    _json_fault(node, lambda result: result["tx_json"].update(DeliverMax="9" * 5000))
    nodes["5000-digit"] = (node, node.include(signed_testnet_payment(amount="1500000")))
    node = FakeQuickNode()
    node.faults["server_state"] = lambda request: NESTED
    nodes["nested-server-state"] = (node, node.include(signed_testnet_payment()))
    node = FakeQuickNode()
    node.faults["server_info"] = lambda request: NESTED
    nodes["nested-server-info"] = (node, node.include(signed_testnet_payment()))
    for node, _ in nodes.values():
        funded_sender(node)
    return nodes


@pytest.mark.parametrize("case", list(_hostile_nodes()))
def test_q4_probe_contains_each_hostile_input_and_keeps_partial_results(case: str) -> None:
    node, tx_hash = _hostile_nodes()[case]
    report = run_probe(transport(node), account=SENDER.account, transaction_hash=tx_hash)
    statuses = {name: check["status"] for name, check in report["checks"].items()}
    codes = {name: check.get("code") for name, check in report["checks"].items()}
    assert report["outcome"] == "FAIL"
    assert_clean(json.dumps(report))
    if case in ("extreme-exponent", "5000-digit"):
        assert statuses["validated_ledger_identity"] == "PASS"
        assert statuses["account_info"] == "PASS"
        assert (statuses["transaction_lookup"], codes["transaction_lookup"]) == (
            "FAIL",
            "MALFORMED_AMOUNT",
        )
    elif case == "nested-server-state":
        assert (statuses["node_identity_stability"], codes["node_identity_stability"]) == (
            "FAIL",
            "RESPONSE_TOO_DEEP",
        )
        assert statuses["transaction_lookup"] == "PASS"
    else:
        assert codes["validated_ledger_identity"] == "RESPONSE_TOO_DEEP"
        assert statuses["account_info"] == "NOT_RUN"
        assert codes["transaction_lookup"] == "RESPONSE_TOO_DEEP"


def _set_endpoint(monkeypatch: pytest.MonkeyPatch, network: str = "testnet") -> None:
    monkeypatch.setenv("CREDENCE_QUICKNODE_XRPL_URL", URL)
    monkeypatch.setenv("CREDENCE_QUICKNODE_XRPL_NETWORK", network)


def test_q4_cli_reports_hostile_input_with_exit_1_and_no_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _set_endpoint(monkeypatch)
    node, tx_hash = _hostile_nodes()["extreme-exponent"]
    out = tmp_path / "report.json"
    code = probe_main(
        ["--account", SENDER.account, "--tx", tx_hash, "--out", str(out)],
        transport_factory=lambda chosen: QuickNodeXRPLTransport(node, chosen, clock=lambda: NOW()),
    )
    captured = capsys.readouterr()
    assert code == EXIT_FAILED
    report = json.loads(out.read_text())
    assert report["outcome"] == "FAIL"
    assert report["checks"]["transaction_lookup"]["code"] == "MALFORMED_AMOUNT"
    assert report["checks"]["validated_ledger_identity"]["status"] == "PASS"
    assert "Traceback" not in captured.err + captured.out
    assert_clean(out.read_text() + captured.err + captured.out)


def NOW() -> float:
    from x1q_quicknode import NOW as fixed

    return fixed


def test_q4_cli_against_a_real_malformed_http_endpoint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_endpoint(monkeypatch)
    bad = b"HTTP/1.1 OK-but-not-a-status\r\n\r\n"
    server = LoopbackQuickNode([Raw(bad)] * 20)
    wiring = Wiring(server)

    def factory(chosen: Any) -> QuickNodeXRPLTransport:
        rpc = QuickNodeJsonRpcClient(chosen, resolver=wiring.resolver, connector=wiring.connector)
        return QuickNodeXRPLTransport(rpc, chosen)

    code = probe_main(["--node-samples", "1"], transport_factory=factory)
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert code == EXIT_FAILED
    assert report["checks"]["validated_ledger_identity"]["code"] == "MALFORMED_HTTP_RESPONSE"
    assert "Traceback" not in captured.err
    assert_clean(captured.out + captured.err)


def test_q4_cli_exit_codes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    node = FakeQuickNode()
    funded_sender(node)
    tx_hash = node.include(signed_testnet_payment())

    def factory(chosen: Any) -> QuickNodeXRPLTransport:
        return QuickNodeXRPLTransport(node, chosen, clock=NOW)

    for name in ("CREDENCE_QUICKNODE_XRPL_URL", "CREDENCE_QUICKNODE_XRPL_NETWORK"):
        monkeypatch.delenv(name, raising=False)
    assert probe_main([], transport_factory=factory) == EXIT_NOT_CONFIGURED
    _set_endpoint(monkeypatch, "mainnet")
    assert probe_main([], transport_factory=factory) == EXIT_NOT_CONFIGURED
    _set_endpoint(monkeypatch)
    full = ["--account", SENDER.account, "--tx", tx_hash]
    assert probe_main(full, transport_factory=factory) == EXIT_PASS
    assert probe_main([], transport_factory=factory) == EXIT_INCOMPLETE

    def broken(chosen: Any) -> QuickNodeXRPLTransport:
        raise RecursionError("factory failed")

    assert probe_main(full, transport_factory=broken) == EXIT_FAILED
    captured = capsys.readouterr()
    assert '"code": "PROBE_INTERNAL_ERROR"' in captured.out
    assert "Traceback" not in captured.err
    assert_clean(captured.out + captured.err)


def test_x1s_identity_and_history_regressions_still_hold() -> None:
    node = FakeQuickNode()
    identity = transport(node).validated_ledger_identity()
    assert (identity.network_id, identity.ledger_index, identity.complete_ledgers) == (
        TESTNET_ID,
        VALIDATED,
        HISTORY,
    )
