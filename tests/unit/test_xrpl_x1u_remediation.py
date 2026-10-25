"""X1u: Salvador's X1t findings B1 (connection-close framing) and B2 (quadratic JSON pre-check).

B1 cases run real HTTP over loopback sockets (and the operator probe over real loopback TLS).
B2 functional cases are deterministic; the performance gates use bounds far above the measured
linear cost (a quadratic pre-check needed about 31 s for 128 KB) and are kept separate from the
correctness assertions. Measurements for the report live outside the suite.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from test_xrpl_x1q_quicknode import client, ok, raised, transport
from x1q_quicknode import (
    HOST,
    NOW,
    SENDER,
    TOKEN,
    URL,
    FakeQuickNode,
    LoopbackQuickNode,
    Raw,
    Wiring,
    clio_responder,
    config,
    funded_sender,
    loopback_certificate,
    signed_testnet_payment,
)

from credence.xrpl_adapter import quicknode
from credence.xrpl_adapter.quicknode import (
    MAX_JSON_DEPTH,
    MAX_RESPONSE_BYTES,
    HostedObservationStatus,
    QuickNodeJsonRpcClient,
    QuickNodeRefused,
    QuickNodeUnavailable,
    QuickNodeXRPLTransport,
    parse_json_bounded,
)
from credence.xrpl_adapter.quicknode_probe import EXIT_PASS
from credence.xrpl_adapter.quicknode_probe import main as probe_main

BODY = b'{"result":{"status":"success"}}'
LENGTH = len(BODY)
HOSTILE_UNIT = b'\\"'


def hostile(size: int) -> bytes:
    """Salvador's B2 input: an opening quote, then an unterminated run of escaped quotes."""
    return b'"' + HOSTILE_UNIT * ((size - 1) // 2)


# --- B1: connection-close framing ----------------------------------------------------------------

FRAMINGS = {
    "1.1-length-close": b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s",
    "1.0-length": b"HTTP/1.0 200 OK\r\nContent-Length: %d\r\n\r\n%s",
    "1.1-length-keep-alive": b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s",
}


@pytest.mark.parametrize("name", list(FRAMINGS))
def test_b1_length_framed_responses_are_accepted(name: str) -> None:
    rpc, _, _ = client([Raw(FRAMINGS[name] % (LENGTH, BODY))], max_attempts=1)
    assert rpc.call("server_info", {}) == BODY


def test_b1_chunked_response_with_close_is_accepted() -> None:
    data = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
    data += b"%x\r\n%s\r\n0\r\n\r\n" % (LENGTH, BODY)
    rpc, _, _ = client([Raw(data)], max_attempts=1)
    assert rpc.call("server_info", {}) == BODY


def test_b1_http10_body_delimited_by_close_is_accepted() -> None:
    rpc, _, _ = client([Raw(b"HTTP/1.0 200 OK\r\n\r\n" + BODY)], max_attempts=1)
    assert rpc.call("server_info", {}) == BODY


@pytest.mark.parametrize(
    "data",
    [
        b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s"
        % (LENGTH + 10, BODY),
        b"HTTP/1.0 200 OK\r\nContent-Length: %d\r\n\r\n%s" % (LENGTH + 10, BODY),
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n40\r\n" + BODY,
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n%x\r\n%s\r\n" % (LENGTH, BODY),
    ],
    ids=["1.1-short", "1.0-short", "chunk-short", "no-terminating-chunk"],
)
def test_b1_bodies_shorter_than_their_framing_are_refused(data: bytes) -> None:
    rpc, _, _ = client([Raw(data)], max_attempts=1)
    error = raised(lambda: rpc.call("server_info", {}))
    assert (type(error), error.code, error.retryable) == (
        QuickNodeUnavailable,
        "CONNECTION_CLOSED",
        True,
    )


@pytest.mark.parametrize(
    "headers",
    [
        b"Content-Length: %d\r\nTransfer-Encoding: chunked\r\n" % LENGTH,
        b"Content-Length: %d\r\nContent-Length: %d\r\n" % (LENGTH, LENGTH + 1),
        b"Content-Length: 3O\r\n",
        b"Content-Length: -5\r\n",
        b"Transfer-Encoding: gzip, chunked\r\n",
        b"Transfer-Encoding: identity\r\n",
    ],
    ids=["length-and-chunked", "conflicting-lengths", "non-digit", "negative", "gzip", "identity"],
)
def test_b1_contradictory_or_malformed_framing_is_refused(headers: bytes) -> None:
    rpc, server, sleeps = client([Raw(b"HTTP/1.1 200 OK\r\n" + headers + b"\r\n" + BODY), ok()])
    error = raised(lambda: rpc.call("server_info", {}))
    assert (type(error), error.code) == (QuickNodeRefused, "MALFORMED_HTTP_RESPONSE")
    assert (len(server.requests), sleeps) == (1, [])


def test_b1_identical_duplicate_lengths_and_existing_refusals_are_preserved() -> None:
    same = b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nContent-Length: %d\r\n\r\n%s"
    rpc, _, _ = client([Raw(same % (LENGTH, LENGTH, BODY))], max_attempts=1)
    assert rpc.call("server_info", {}) == BODY
    huge = Raw(b"HTTP/1.1 200 OK\r\nContent-Length: 999999999999\r\n\r\n{}")
    rpc, _, _ = client([huge], max_attempts=1)
    assert raised(lambda: rpc.call("server_info", {})).code == "RESPONSE_TOO_LARGE"
    rpc, _, _ = client([Raw(b"HTTP/1.1 OK-but-not-a-status\r\n\r\n")], max_attempts=1)
    assert raised(lambda: rpc.call("server_info", {})).code == "MALFORMED_HTTP_RESPONSE"


class GuardedSocket(socket.socket):
    """Fails the test if anything sets a timeout on a socket that is already closed."""

    timeouts_after_close = 0

    def settimeout(self, value: float | None) -> None:
        if self.fileno() == -1:
            GuardedSocket.timeouts_after_close += 1
        super().settimeout(value)


@pytest.mark.parametrize("name", list(FRAMINGS))
def test_b1_no_socket_operation_after_the_response_closed_the_socket(name: str) -> None:
    server = LoopbackQuickNode([Raw(FRAMINGS[name] % (LENGTH, BODY))])
    wiring = Wiring(server)
    GuardedSocket.timeouts_after_close = 0

    def connector(address: Any, hostname: str, deadline: float, watchdog: Any) -> socket.socket:
        sock = GuardedSocket(socket.AF_INET, socket.SOCK_STREAM)
        watchdog.attach(sock)
        sock.connect(address[1])
        return sock

    rpc = QuickNodeJsonRpcClient(
        config(max_attempts=1), resolver=wiring.resolver, connector=connector
    )
    assert rpc.call("server_info", {}) == BODY
    assert GuardedSocket.timeouts_after_close == 0


def _tls_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: bytes) -> tuple[int, Any]:
    cafile, server_context = loopback_certificate(tmp_path)
    node = FakeQuickNode()
    funded_sender(node)
    tx_hash = node.include(signed_testnet_payment())
    server = LoopbackQuickNode(
        [], default=clio_responder(node, version=version, close=True), tls=server_context
    )
    wiring = Wiring(server)
    monkeypatch.setenv("CREDENCE_QUICKNODE_XRPL_URL", URL)
    monkeypatch.setenv("CREDENCE_QUICKNODE_XRPL_NETWORK", "testnet")

    def factory(chosen: Any) -> QuickNodeXRPLTransport:
        # The real TLS connector: certificate and hostname verification stay on; the test only
        # adds its throwaway CA to the trust store.
        rpc = QuickNodeJsonRpcClient(chosen, resolver=wiring.resolver)
        rpc.tls_context.load_verify_locations(cafile)
        return QuickNodeXRPLTransport(rpc, chosen, clock=lambda: NOW)

    out = tmp_path / "probe.json"
    code = probe_main(
        ["--account", SENDER.account, "--tx", tx_hash, "--node-samples", "2", "--out", str(out)],
        transport_factory=factory,
    )
    assert all(seen.headers.get("host") == HOST for seen in server.requests)
    return code, json.loads(out.read_text())


@pytest.mark.parametrize("version", [b"HTTP/1.1", b"HTTP/1.0"], ids=["1.1-close", "1.0"])
def test_b1_operator_probe_over_real_loopback_tls_with_connection_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: bytes
) -> None:
    code, report = _tls_probe(tmp_path, monkeypatch, version)
    assert code == EXIT_PASS
    assert report["outcome"] == "PASS"
    assert {name: check["status"] for name, check in report["checks"].items()} == {
        "validated_ledger_identity": "PASS",
        "node_identity_stability": "PASS",
        "account_info": "PASS",
        "account_objects": "PASS",
        "account_lines": "PASS",
        "transaction_lookup": "PASS",
    }
    assert "CONNECTION_FAILED" not in json.dumps(report)
    assert TOKEN not in json.dumps(report)


def test_b1_tls_verification_is_still_enforced_on_the_probe_path(tmp_path: Path) -> None:
    _, server_context = loopback_certificate(tmp_path)
    server = LoopbackQuickNode([], default=clio_responder(FakeQuickNode()), tls=server_context)
    rpc = QuickNodeJsonRpcClient(config(max_attempts=1), resolver=Wiring(server).resolver)
    assert raised(lambda: rpc.call("server_info", {})).code == "TLS_VERIFICATION_FAILED"


# --- B2: linear JSON pre-check -------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        b'{"a": "quote \\" and [brackets] {braces} inside"}',
        b'{"a": "\\\\\\\\"}',
        b'["\\\\\\"]"]',
        b"[" * MAX_JSON_DEPTH + b'"\\""' + b"]" * MAX_JSON_DEPTH,
        b'{"a":' * (MAX_JSON_DEPTH - 1) + b"[1]" + b"}" * (MAX_JSON_DEPTH - 1),
        b'"\\u0022 is a quote escape"',
    ],
    ids=[
        "quotes-and-brackets-in-strings",
        "escaped-backslashes-then-quote",
        "escaped-backslash-escaped-quote",
        "escaped-quote-at-depth-limit",
        "mixed-depth-limit",
        "unicode-escape",
    ],
)
def test_b2_valid_json_is_accepted(raw: bytes) -> None:
    assert parse_json_bounded(raw) == json.loads(raw)


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b'{"a": "abc', "MALFORMED_RESPONSE"),
        (b'"\\', "MALFORMED_RESPONSE"),
        (b'"\\\\\\"', "MALFORMED_RESPONSE"),
        (hostile(1024), "MALFORMED_RESPONSE"),
        (b"[" * (MAX_JSON_DEPTH + 1) + b"]" * (MAX_JSON_DEPTH + 1), "RESPONSE_TOO_DEEP"),
        (b"[" * (MAX_JSON_DEPTH + 1) + b'"\\"]', "RESPONSE_TOO_DEEP"),
        (b"[}", "MALFORMED_RESPONSE"),
        (b"[1]]", "MALFORMED_RESPONSE"),
        (b'{"a": [1}', "MALFORMED_RESPONSE"),
        (b'{"a": "\xff\xfe"}', "MALFORMED_RESPONSE"),
        (b"[01]", "MALFORMED_RESPONSE"),
        (b"[1.]", "MALFORMED_RESPONSE"),
        (b"[-]", "MALFORMED_RESPONSE"),
        (b"[1e]", "MALFORMED_RESPONSE"),
        (b"[1e999999]", "NUMBER_OUT_OF_RANGE"),
        (b"[" + b"9" * 30 + b"]", "NUMBER_OUT_OF_RANGE"),
        (b"[NaN]", "NUMBER_OUT_OF_RANGE"),
        (b"\\[1]", "MALFORMED_RESPONSE"),
    ],
)
def test_b2_malformed_json_is_a_typed_refusal(raw: bytes, code: str) -> None:
    with pytest.raises(QuickNodeRefused) as caught:
        parse_json_bounded(raw)
    assert caught.value.code == code
    assert caught.value.__cause__ is None and caught.value.__context__ is None


def test_b2_scanner_uses_no_regular_expression_on_json_bodies() -> None:
    assert not hasattr(quicknode, "_JSON_STRING")
    assert not hasattr(quicknode, "_INNERMOST_PAIR")


def test_b2_maximum_size_and_oversized_bodies() -> None:
    at_cap = b'"' + b"a" * (MAX_RESPONSE_BYTES - 2) + b'"'
    assert len(at_cap) == MAX_RESPONSE_BYTES
    assert parse_json_bounded(at_cap) == "a" * (MAX_RESPONSE_BYTES - 2)
    with pytest.raises(QuickNodeRefused, match="RESPONSE_TOO_LARGE"):
        parse_json_bounded(at_cap + b" ")


PUBLIC_READS = [
    "validated_ledger_identity",
    "validated_ledger_observation",
    "sample_node_identity",
    "account_state",
    "account_objects",
    "account_lines",
    "observe_payment",
]
FAULTED_METHOD = {
    "validated_ledger_identity": "server_info",
    "validated_ledger_observation": "account_info",
    "sample_node_identity": "server_state",
    "account_state": "account_info",
    "account_objects": "account_objects",
    "account_lines": "account_lines",
    "observe_payment": "tx",
}


@pytest.mark.parametrize("read", PUBLIC_READS)
def test_b2_hostile_json_through_every_public_read_is_typed_and_prompt(read: str) -> None:
    node = FakeQuickNode()
    funded_sender(node)
    tx_hash = node.include(signed_testnet_payment())
    reader = transport(node)
    identity = reader.validated_ledger_identity()
    node.faults[FAULTED_METHOD[read]] = lambda request: hostile(512 * 1024)
    calls = {
        "validated_ledger_identity": lambda: reader.validated_ledger_identity(),
        "validated_ledger_observation": lambda: reader.validated_ledger_observation(SENDER.account),
        "sample_node_identity": lambda: reader.sample_node_identity(),
        "account_state": lambda: reader.account_state(SENDER.account, identity),
        "account_objects": lambda: reader.account_objects(SENDER.account, identity),
        "account_lines": lambda: reader.account_lines(SENDER.account, identity),
        "observe_payment": lambda: reader.observe_payment(tx_hash),
    }
    started = time.monotonic()
    if read == "observe_payment":
        observation = calls[read]()
        assert (observation.status, observation.code) == (
            HostedObservationStatus.REFUSED,
            "MALFORMED_RESPONSE",
        )
    else:
        assert raised(calls[read]).code == "MALFORMED_RESPONSE"
    assert time.monotonic() - started < 1.0


# Performance gates: bounds are orders of magnitude above the measured linear cost (about 75 ms
# for a cap-sized hostile body) and below what a quadratic pre-check needs (about 31 s at 128 KB).


def test_b2_perf_cap_sized_hostile_input_is_refused_promptly() -> None:
    raw = hostile(MAX_RESPONSE_BYTES)
    started = time.monotonic()
    with pytest.raises(QuickNodeRefused, match="MALFORMED_RESPONSE"):
        parse_json_bounded(raw)
    assert time.monotonic() - started < 3.0


def test_b2_perf_unrelated_threads_keep_progressing() -> None:
    raw = hostile(MAX_RESPONSE_BYTES)
    outcome: list[str] = []

    def scan() -> None:
        try:
            parse_json_bounded(raw)
        except QuickNodeRefused as exc:
            outcome.append(exc.code)

    worker = threading.Thread(target=scan)
    ticks = 0
    worker.start()
    while worker.is_alive():
        ticks += 1
        time.sleep(0.001)
    worker.join()
    assert outcome == ["MALFORMED_RESPONSE"]
    assert ticks >= 1
