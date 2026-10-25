"""X402-P3 G1: outbound requests are immutable and re-validated immediately before they are sent.

Skopje's attack: build an HttpRequest from a mutable list (it validated), then mutate the list (or
an inner pair, or bypass the frozen dataclass with object.__setattr__) so a credential or payment
header reaches the wire. Offline except the loopback-only HTTP server in the redirect test.
"""

from __future__ import annotations

import http.server
import socket
import threading
from collections.abc import Iterator
from typing import Any

import pytest

from credence.x402.errors import X402Refusal, X402Refused
from credence.x402.mock import MockTransport
from credence.x402.transport import (
    GuardedClient,
    HttpRequest,
    OriginAllowList,
    StdlibTransport,
    validate_request,
)

URL = "https://merchant.example/v1/chat"
JSON = ("content-type", "application/json")


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield


offline = pytest.mark.usefixtures("no_network")


def guarded() -> tuple[GuardedClient, MockTransport]:
    transport = MockTransport({("POST", URL): (402, [], b""), ("GET", URL): (402, [], b"")})
    return GuardedClient(transport, OriginAllowList.of(URL)), transport


def bypass(request: HttpRequest, **values: Any) -> HttpRequest:
    for name, value in values.items():
        object.__setattr__(request, name, value)
    return request


# --- mutable caller input is copied at construction ---------------------------------------


@offline
def test_mutable_header_list_is_frozen_into_a_tuple() -> None:
    headers = [JSON]
    request = HttpRequest("POST", URL, headers, b"{}")  # type: ignore[arg-type]
    assert type(request.headers) is tuple and request.headers == (JSON,)
    headers.append(("Authorization", "Bearer stolen"))  # Skopje's mutation
    assert request.headers == (JSON,)
    client, transport = guarded()
    client.send(request)
    assert [r.headers for r in transport.sent] == [(JSON,)]


@offline
def test_inner_mutable_pairs_are_copied() -> None:
    pair = ["accept", "application/json"]
    request = HttpRequest("POST", URL, [pair], b"{}")  # type: ignore[list-item]
    pair[0], pair[1] = "PAYMENT-SIGNATURE", "eyJ4NDAyVmVyc2lvbiI6Mn0="
    assert request.headers == (("accept", "application/json"),)
    assert all(type(item) is tuple for item in request.headers)
    client, transport = guarded()
    client.send(request)
    assert transport.sent[0].headers == (("accept", "application/json"),)


@offline
def test_mutable_body_is_frozen_into_bytes() -> None:
    body = bytearray(b"{}")
    request = HttpRequest("POST", URL, (JSON,), body)  # type: ignore[arg-type]
    digest = request.body_digest
    body[:] = b'{"prompt":"swapped after digest"}'
    assert type(request.body) is bytes and request.body == b"{}"
    assert request.body_digest == digest
    assert HttpRequest("POST", URL, (JSON,), memoryview(b"{}")).body == b"{}"  # type: ignore[arg-type]


@offline
@pytest.mark.parametrize(
    "headers",
    [
        "accept: application/json",
        b"accept",
        {"accept": "application/json"},
        [("accept",)],
        [("accept", "application/json", "extra")],
        [("accept", b"application/json")],
        [(b"accept", "application/json")],
        None,
    ],
)
def test_malformed_header_containers_are_refused(headers: Any) -> None:
    with pytest.raises(X402Refused) as exc:
        HttpRequest("GET", URL, headers)
    assert exc.value.code is X402Refusal.HEADER_NOT_ALLOWED


@offline
def test_str_subclass_header_names_are_refused() -> None:
    class Sneaky(str):
        def lower(self) -> str:
            return "accept"

    with pytest.raises(X402Refused):
        HttpRequest("GET", URL, ((Sneaky("Authorization"), "Bearer x"),))


@offline
@pytest.mark.parametrize("body", ["{}", 123, None])
def test_non_bytes_body_is_refused(body: Any) -> None:
    with pytest.raises(X402Refused) as exc:
        HttpRequest("POST", URL, (JSON,), body)
    assert exc.value.code is X402Refusal.MALFORMED_REQUEST


# --- revalidation at every sender (frozen-dataclass bypass) -------------------------------


BYPASSES: list[tuple[str, dict[str, Any], X402Refusal]] = [
    (
        "auth header",
        {"headers": (("Authorization", "Bearer x"),)},
        X402Refusal.PAYMENT_MATERIAL_REFUSED,
    ),
    ("cookie", {"headers": (("Cookie", "a=b"),)}, X402Refusal.PAYMENT_MATERIAL_REFUSED),
    (
        "payment header",
        {"headers": (("PAYMENT-SIGNATURE", "x"),)},
        X402Refusal.PAYMENT_MATERIAL_REFUSED,
    ),
    ("unknown header", {"headers": (("X-Custom", "x"),)}, X402Refusal.HEADER_NOT_ALLOWED),
    ("mutable list", {"headers": [JSON]}, X402Refusal.HEADER_NOT_ALLOWED),
    ("inner list", {"headers": (["accept", "a/b"],)}, X402Refusal.HEADER_NOT_ALLOWED),
    ("repeated", {"headers": (JSON, JSON)}, X402Refusal.HEADER_NOT_ALLOWED),
    ("CRLF value", {"headers": (("accept", "a\r\nCookie: x"),)}, X402Refusal.HEADER_NOT_ALLOWED),
    ("bytearray body", {"body": bytearray(b"{}")}, X402Refusal.MALFORMED_REQUEST),
    ("bad method", {"method": "DELETE"}, X402Refusal.UNSUPPORTED_METHOD),
]


@offline
@pytest.mark.parametrize(("label", "values", "code"), BYPASSES, ids=[b[0] for b in BYPASSES])
def test_guarded_client_revalidates_before_forwarding(
    label: str, values: dict[str, Any], code: X402Refusal
) -> None:
    request = bypass(HttpRequest("POST", URL, (JSON,), b"{}"), **values)
    client, transport = guarded()
    with pytest.raises(X402Refused) as exc:
        client.send(request)
    assert exc.value.code is code
    assert transport.sent == []  # nothing reached the transport


@offline
@pytest.mark.parametrize(("label", "values", "code"), BYPASSES, ids=[b[0] for b in BYPASSES])
def test_stdlib_transport_refuses_an_invalid_request_called_directly(
    label: str, values: dict[str, Any], code: X402Refusal
) -> None:
    """Called directly, bypassing GuardedClient: refused before any socket is opened (a socket
    attempt would raise AssertionError from the no_network fixture instead)."""
    request = bypass(HttpRequest("POST", URL, (JSON,), b"{}"), **values)
    with pytest.raises(X402Refused) as exc:
        StdlibTransport().send(request)
    assert exc.value.code is code


@offline
def test_guarded_client_forwards_a_fresh_validated_copy() -> None:
    request = HttpRequest("POST", URL, (JSON,), b"{}")
    client, transport = guarded()
    client.send(request)
    sent = transport.sent[0]
    assert sent == request and sent is not request
    validate_request(sent)


@offline
def test_unauthorized_headers_are_still_refused_at_construction() -> None:
    for name in ("Authorization", "Cookie", "Proxy-Authorization", "X-PAYMENT", "X-Other"):
        with pytest.raises(X402Refused):
            HttpRequest("POST", URL, [(name, "x")])  # type: ignore[list-item]


@offline
def test_redirect_through_guarded_client_is_one_request() -> None:
    transport = MockTransport({("POST", URL): (302, [("location", "https://evil.example/")], b"")})
    client = GuardedClient(transport, OriginAllowList.of(URL))
    headers = [JSON]
    request = HttpRequest("POST", URL, headers, b"{}")  # type: ignore[arg-type]
    headers.append(("Cookie", "session=x"))
    _, response = client.send(request)
    assert response.status == 302
    assert [(r.method, r.url, r.headers) for r in transport.sent] == [("POST", URL, (JSON,))]


class _Recorder(http.server.BaseHTTPRequestHandler):
    seen: list[tuple[str, list[str]]] = []

    def do_POST(self) -> None:
        type(self).seen.append((self.path, sorted(k.lower() for k in self.headers.keys())))
        self.rfile.read(int(self.headers.get("Content-Length") or 0))  # drain the request body
        self.send_response(302)
        self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/collect")
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = do_POST

    def log_message(self, format: str, *args: object) -> None:
        return


def test_real_transport_mutation_and_redirect() -> None:
    """Over a real loopback socket: a header list mutated after construction adds nothing to the
    wire, and a 302 is not followed (exactly one request)."""
    _Recorder.seen = []
    server = http.server.HTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/v1/chat"
        client = GuardedClient(StdlibTransport(timeout_seconds=5), OriginAllowList.of(url))
        headers = [JSON]
        request = HttpRequest("POST", url, headers, b"{}")  # type: ignore[arg-type]
        headers.append(("Authorization", "Bearer stolen"))
        _, response = client.send(request)
    finally:
        server.shutdown()
        server.server_close()
    assert response.status == 302
    assert [path for path, _ in _Recorder.seen] == ["/v1/chat"]
    assert set(_Recorder.seen[0][1]) <= {
        "host",
        "accept-encoding",
        "content-length",
        "content-type",
    }
