"""X402-P4a G1: forged str subclasses and other request bypasses never reach the wire.

Skopje's attack: a `str` subclass whose `lower()` returns "accept" while the underlying text is
"Authorization", injected by a frozen-dataclass bypass, passed validation and was transmitted by a
direct `StdlibTransport.send`. Every case here is observed on a loopback-only HTTP server that
records exactly what arrives. No external network; nothing signed, submitted or paid.
"""

from __future__ import annotations

import http.server
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from credence.x402.errors import X402Refusal, X402Refused
from credence.x402.transport import (
    GuardedClient,
    HttpRequest,
    OriginAllowList,
    StdlibTransport,
    validate_request,
)

JSON = ("content-type", "application/json")
FRAMING = {"host", "accept-encoding", "content-length"}


@dataclass
class Wire:
    """What the loopback server actually received."""

    requests: list[tuple[str, str, list[tuple[str, str]], bytes]] = field(default_factory=list)


@pytest.fixture
def wire() -> Iterator[tuple[str, Wire]]:
    seen = Wire()

    class Recorder(http.server.BaseHTTPRequestHandler):
        def _answer(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            seen.requests.append((self.command, self.path, list(self.headers.items()), body))
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/next")
            else:
                self.send_response(402)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = _answer
        do_POST = _answer

        def log_message(self, format: str, *args: object) -> None:
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Recorder)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", seen
    finally:
        server.shutdown()
        server.server_close()


class LowerLies(str):
    def lower(self) -> str:
        return "accept"


class EncodeLies(str):
    def encode(self, *args: object, **kwargs: object) -> bytes:  # type: ignore[override]
        return b"application/json\r\nAuthorization: Bearer injected"


class StrLies(str):
    def __str__(self) -> str:
        return "Authorization"


class EqLies(str):
    def __eq__(self, other: object) -> bool:
        return True

    __hash__ = str.__hash__


def send(via: str, base: str, request: HttpRequest) -> None:
    if via == "guarded":
        GuardedClient(StdlibTransport(timeout_seconds=5), OriginAllowList.of(request.url)).send(
            request
        )
    else:
        StdlibTransport(timeout_seconds=5).send(request)


def forged(base: str, **values: Any) -> HttpRequest:
    request = HttpRequest("POST", f"{base}/v1/chat", (JSON,), b"{}")
    for name, value in values.items():
        object.__setattr__(request, name, value)  # frozen-dataclass bypass
    return request


FORGERIES: list[tuple[str, dict[str, Any], X402Refusal]] = [
    (
        "name lower() lies",
        {"headers": ((LowerLies("Authorization"), "Bearer x"),)},
        X402Refusal.HEADER_NOT_ALLOWED,
    ),
    (
        "name lower() lies, cookie",
        {"headers": ((LowerLies("Cookie"), "a=b"),)},
        X402Refusal.HEADER_NOT_ALLOWED,
    ),
    (
        "name __str__ lies",
        {"headers": ((StrLies("accept"), "a/b"),)},
        X402Refusal.HEADER_NOT_ALLOWED,
    ),
    (
        "name __eq__ lies",
        {"headers": ((EqLies("PAYMENT-SIGNATURE"), "x"),)},
        X402Refusal.HEADER_NOT_ALLOWED,
    ),
    (
        "value encode() lies",
        {"headers": (("content-type", EncodeLies("application/json")),)},
        X402Refusal.HEADER_NOT_ALLOWED,
    ),
    (
        "value __str__ lies",
        {"headers": (("accept", StrLies("a/b")),)},
        X402Refusal.HEADER_NOT_ALLOWED,
    ),
    ("mutable header list", {"headers": [JSON]}, X402Refusal.HEADER_NOT_ALLOWED),
    ("nested mutable pair", {"headers": (["accept", "a/b"],)}, X402Refusal.HEADER_NOT_ALLOWED),
    (
        "authorization",
        {"headers": (("Authorization", "Bearer x"),)},
        X402Refusal.PAYMENT_MATERIAL_REFUSED,
    ),
    (
        "payment header",
        {"headers": (("PAYMENT-SIGNATURE", "x"),)},
        X402Refusal.PAYMENT_MATERIAL_REFUSED,
    ),
    ("whitespace name", {"headers": (("Authorization ", "x"),)}, X402Refusal.HEADER_NOT_ALLOWED),
    ("CRLF in value", {"headers": (("accept", "a\r\nCookie: x"),)}, X402Refusal.HEADER_NOT_ALLOWED),
    ("bytes name", {"headers": ((b"accept", "a/b"),)}, X402Refusal.HEADER_NOT_ALLOWED),
    ("three-field pair", {"headers": (("accept", "a/b", "c"),)}, X402Refusal.HEADER_NOT_ALLOWED),
    ("bytearray body", {"body": bytearray(b"{}")}, X402Refusal.MALFORMED_REQUEST),
]


@pytest.mark.parametrize("via", ["guarded", "direct"])
@pytest.mark.parametrize(("label", "values", "code"), FORGERIES, ids=[f[0] for f in FORGERIES])
def test_forged_request_is_refused_and_nothing_reaches_the_wire(
    wire: tuple[str, Wire], via: str, label: str, values: dict[str, Any], code: X402Refusal
) -> None:
    base, seen = wire
    with pytest.raises(X402Refused) as exc:
        send(via, base, forged(base, **values))
    assert exc.value.code is code
    assert seen.requests == []


@pytest.mark.parametrize("cls", [LowerLies, EncodeLies, StrLies, EqLies])
def test_str_subclasses_are_refused_at_construction(cls: type[str]) -> None:
    for headers in (((cls("accept"), "a/b"),), (("accept", cls("a/b")),)):
        with pytest.raises(X402Refused):
            HttpRequest("POST", "https://merchant.example/v1/chat", headers)


def test_validate_request_does_not_call_subclass_methods() -> None:
    calls: list[str] = []

    class Spy(str):
        def lower(self) -> str:
            calls.append("lower")
            return "accept"

    request = HttpRequest("POST", "https://merchant.example/v1/chat", (JSON,), b"{}")
    object.__setattr__(request, "headers", ((Spy("Authorization"), "x"),))
    with pytest.raises(X402Refused):
        validate_request(request)
    assert calls == []  # refused on the exact-type check, before any method of the subclass ran


@pytest.mark.parametrize("via", ["guarded", "direct"])
def test_legitimate_request_reaches_the_wire_exactly(wire: tuple[str, Wire], via: str) -> None:
    base, seen = wire
    headers = [JSON, ("Accept", "application/json")]
    request = HttpRequest("POST", f"{base}/v1/chat", headers, bytearray(b'{"p":1}'))  # type: ignore[arg-type]
    headers.append(("Authorization", "Bearer late"))  # caller mutates its list afterwards
    send(via, base, request)
    ((method, path, received, body),) = seen.requests
    assert (method, path, body) == ("POST", "/v1/chat", b'{"p":1}')
    names = {name.lower() for name, _ in received}
    assert names - FRAMING == {"content-type", "accept"}
    assert dict((k.lower(), v) for k, v in received)["content-type"] == "application/json"


@pytest.mark.parametrize("via", ["guarded", "direct"])
def test_redirect_is_not_followed(wire: tuple[str, Wire], via: str) -> None:
    base, seen = wire
    request = HttpRequest("POST", f"{base}/redirect", (JSON,), b"{}")
    if via == "guarded":
        _, response = GuardedClient(
            StdlibTransport(timeout_seconds=5), OriginAllowList.of(request.url)
        ).send(request)
    else:
        response = StdlibTransport(timeout_seconds=5).send(request)
    assert response.status == 302
    assert [(m, p) for m, p, _, _ in seen.requests] == [("POST", "/redirect")]


def test_direct_transport_does_not_enforce_the_origin_allow_list(wire: tuple[str, Wire]) -> None:
    """Documented boundary: StdlibTransport validates the request but not the merchant origin; the
    allow-list belongs to GuardedClient."""
    base, seen = wire
    request = HttpRequest("POST", f"{base}/v1/chat", (JSON,), b"{}")
    StdlibTransport(timeout_seconds=5).send(request)  # no allow-list involved
    assert len(seen.requests) == 1
    other_origin = GuardedClient(
        StdlibTransport(timeout_seconds=5), OriginAllowList.of("https://merchant.example/v1/chat")
    )
    with pytest.raises(X402Refused) as exc:
        other_origin.send(request)
    assert exc.value.code is X402Refusal.ORIGIN_NOT_ALLOWED
    assert len(seen.requests) == 1  # the guarded client sent nothing
