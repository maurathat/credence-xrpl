"""The buyer's HTTP boundary for x402 discovery: one non-paying request, no redirects, no secrets.

Merchant responses are untrusted input. This module enforces, before any byte leaves:

- the target origin (scheme, host, port) is on the operator's allow-list;
- the request carries only headers on an explicit allow-list (ALLOWED_REQUEST_HEADERS: `accept`
  and `content-type`, matched case-insensitively). Every other name is refused, including
  authentication, cookie, proxy-credential and payment headers, unknown names, names with
  whitespace or other characters outside the HTTP token set, and repeated names. Values must be
  printable ASCII without leading or trailing whitespace, and `content-type` must be
  `application/json` (optionally `; charset=utf-8`). So an x402 payment header, a bearer token or a
  cookie can never be sent, to the merchant or to anyone else. The transport itself adds only the
  framing headers http.client always sends (Host, Accept-Encoding: identity, Content-Length);
- `GuardedClient` alone enforces the origin allow-list; `StdlibTransport.send` enforces request
  validity even when called directly, but not the allow-list;
- redirects are never followed. A 3xx is returned to the caller as an observation; its Location is
  recorded only as text and never requested. A transport that follows redirects is a defect.

What it keeps of a response is limited to the status, the headers in PERMITTED_RESPONSE_HEADERS,
and at most MAX_BODY_BYTES of body, so a hostile or verbose merchant cannot make Credence store
cookies, tokens or large payloads. Prompt bodies are never logged: the request body is held only
as bytes for the transport and as a digest for the record.

The default transport for tests is a deterministic in-memory mock (credence.x402.mock). The
stdlib transport here exists for operator-run, non-paying discovery against allow-listed origins.

Owner: x402 (X402-P0).
"""

from __future__ import annotations

import hashlib
import http.client
import re
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from credence.x402.errors import X402Refusal, X402Refused

# The only request headers a non-paying discovery request may carry. Lower case.
ALLOWED_REQUEST_HEADERS = frozenset({"accept", "content-type"})
# Named only to give a clearer refusal code; anything off the allow-list is refused regardless.
SENSITIVE_REQUEST_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "x-api-key",
        "api-key",
        "x-payment",  # x402 v1 payment header
        "payment-signature",  # x402 v2 payment header
        "x-payment-signature",
        "x-payment-secure",
        "x-risk-session",
    }
)
_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")  # RFC 9110 field-name token
_VALUE = re.compile(r"[\x21-\x7e](?:[\x20-\x7e]*[\x21-\x7e])?")
_JSON_CONTENT_TYPE = re.compile(r"application/json(?:; ?charset=utf-8)?", re.IGNORECASE)

# Response headers Credence keeps. Everything else (Set-Cookie, tracing, server banners) is dropped.
PERMITTED_RESPONSE_HEADERS = frozenset(
    {
        "content-type",
        "content-length",
        "location",
        "payment-required",  # x402 v2 challenge header (base64 JSON)
        "x-payment-required",
        "payment-response",  # x402 v2 settlement response header
        "x-payment-response",  # x402 v1 settlement response header
        "date",
        "cache-control",
    }
)

MAX_BODY_BYTES = 65_536
# Stands in for a permitted header the merchant sent twice with different values; parsers refuse it.
DUPLICATE_HEADER_MARKER = "\x00duplicate\x00"
TIMEOUT_SECONDS = 15


@dataclass(frozen=True)
class Origin:
    """scheme://host:port, compared exactly. Only https, except loopback http for local tests."""

    scheme: str
    host: str
    port: int

    @classmethod
    def of(cls, url: str) -> Origin:
        parts = urllib.parse.urlsplit(url)
        scheme = parts.scheme.lower()
        host = (parts.hostname or "").lower()
        if scheme not in ("https", "http") or not host:
            raise X402Refused(X402Refusal.ORIGIN_NOT_ALLOWED, f"not an http(s) URL: {url!r}")
        if parts.username is not None or parts.password is not None:
            raise X402Refused(X402Refusal.ORIGIN_NOT_ALLOWED, "credentials in URL")
        if scheme == "http" and host not in ("127.0.0.1", "localhost", "::1"):
            raise X402Refused(X402Refusal.ORIGIN_NOT_ALLOWED, "plain http is loopback-only")
        port = parts.port or (443 if scheme == "https" else 80)
        return cls(scheme, host, port)

    def __str__(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}"


@dataclass(frozen=True)
class OriginAllowList:
    """The operator's configured origins. Empty means nothing may be contacted."""

    origins: frozenset[Origin]

    @classmethod
    def of(cls, *urls: str) -> OriginAllowList:
        return cls(frozenset(Origin.of(url) for url in urls))

    def check(self, url: str) -> Origin:
        origin = Origin.of(url)
        if origin not in self.origins:
            raise X402Refused(X402Refusal.ORIGIN_NOT_ALLOWED, str(origin))
        return origin


def check_request_header(name: object, value: object) -> None:
    """Refuse any outgoing header that is not exactly an allow-listed name with a clean value.

    Name and value must be exactly the built-in `str` (X402-P4a, G1): a subclass could override
    `lower()`, `encode()` or other methods so that validation sees one header and http.client puts
    another on the wire. Only built-in `str` methods run on the checked objects."""
    if type(name) is not str or type(value) is not str:
        raise X402Refused(X402Refusal.HEADER_NOT_ALLOWED, "header must be exactly str")
    if not _TOKEN.fullmatch(name):
        raise X402Refused(X402Refusal.HEADER_NOT_ALLOWED, f"malformed name {name!r}")
    lowered = name.lower()
    if lowered in SENSITIVE_REQUEST_HEADERS or "auth" in lowered or "cookie" in lowered:
        raise X402Refused(X402Refusal.PAYMENT_MATERIAL_REFUSED, lowered)
    if lowered not in ALLOWED_REQUEST_HEADERS:
        raise X402Refused(X402Refusal.HEADER_NOT_ALLOWED, lowered)
    if not _VALUE.fullmatch(value):
        raise X402Refused(X402Refusal.HEADER_NOT_ALLOWED, f"malformed value for {lowered}")
    if lowered == "content-type" and not _JSON_CONTENT_TYPE.fullmatch(value):
        raise X402Refused(X402Refusal.HEADER_NOT_ALLOWED, "content-type must be application/json")


def _frozen_headers(headers: object) -> tuple[tuple[str, str], ...]:
    if isinstance(headers, (str, bytes, bytearray)) or not isinstance(headers, (tuple, list)):
        raise X402Refused(X402Refusal.HEADER_NOT_ALLOWED, "headers must be a sequence of pairs")
    frozen: list[tuple[str, str]] = []
    for item in headers:
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            raise X402Refused(X402Refusal.HEADER_NOT_ALLOWED, "header must be a (name, value) pair")
        name, value = item[0], item[1]
        if type(name) is not str or type(value) is not str:
            raise X402Refused(X402Refusal.HEADER_NOT_ALLOWED, "header must be text")
        frozen.append((name, value))
    return tuple(frozen)


def _frozen_body(body: object) -> bytes:
    if type(body) is bytes:
        return body
    if isinstance(body, (bytearray, memoryview)):
        return bytes(body)
    raise X402Refused(X402Refusal.MALFORMED_REQUEST, "request body must be bytes")


def validate_request(request: HttpRequest) -> None:
    """Re-check a request as it is about to be sent: method, URL type, headers (exactly a tuple of
    `(str, str)` tuples, every one allow-listed, no repeats) and body (exactly `bytes`). Called at
    construction, by GuardedClient.send and by StdlibTransport.send."""
    if type(request.method) is not str or request.method not in ("GET", "POST", "HEAD"):
        raise X402Refused(X402Refusal.UNSUPPORTED_METHOD, str(request.method))
    if type(request.url) is not str:
        raise X402Refused(X402Refusal.ORIGIN_NOT_ALLOWED, "URL must be text")
    if type(request.headers) is not tuple:
        raise X402Refused(X402Refusal.HEADER_NOT_ALLOWED, "headers are not frozen")
    seen: set[str] = set()
    for item in request.headers:
        if type(item) is not tuple or len(item) != 2:
            raise X402Refused(X402Refusal.HEADER_NOT_ALLOWED, "headers are not frozen")
        name, value = item
        if type(name) is not str or type(value) is not str:  # exact built-in str, never a subclass
            raise X402Refused(X402Refusal.HEADER_NOT_ALLOWED, "header must be exactly str")
        check_request_header(name, value)
        if name.lower() in seen:
            raise X402Refused(X402Refusal.HEADER_NOT_ALLOWED, f"repeated {name.lower()}")
        seen.add(name.lower())
    if type(request.body) is not bytes:
        raise X402Refused(X402Refusal.MALFORMED_REQUEST, "request body is not frozen bytes")


@dataclass(frozen=True)
class HttpRequest:
    """One outgoing request. `body` is transient; only its digest is ever recorded.

    Immutable once built (X402-P3, G1): whatever the caller passes (a list, a list of lists, a
    bytearray), `headers` is copied into a tuple of exact `(str, str)` pairs and `body` into
    `bytes`, so mutating the caller's objects afterwards changes nothing. Senders still re-check
    with `validate_request` before forwarding, because a frozen dataclass can be bypassed with
    `object.__setattr__`.
    """

    method: str
    url: str
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes = b""

    def __post_init__(self) -> None:
        object.__setattr__(self, "headers", _frozen_headers(self.headers))
        object.__setattr__(self, "body", _frozen_body(self.body))
        validate_request(self)

    @property
    def body_digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.body).hexdigest()


@dataclass(frozen=True)
class HttpResponse:
    """What Credence keeps of one response: status, permitted headers (lower-case names), and a
    bounded body. `body_truncated` says the merchant sent more than MAX_BODY_BYTES."""

    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""
    body_truncated: bool = False

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())


def filter_response(
    status: int, raw_headers: list[tuple[str, str]], body: bytes, truncated: bool
) -> HttpResponse:
    """Keep only permitted headers. A permitted header repeated with different values is kept as
    the literal marker so the parser refuses it, instead of choosing one."""
    kept: dict[str, str] = {}
    for name, value in raw_headers:
        key = name.lower()
        if key not in PERMITTED_RESPONSE_HEADERS:
            continue
        if key in kept and kept[key] != value:
            kept[key] = DUPLICATE_HEADER_MARKER
        else:
            kept[key] = value
    return HttpResponse(status, kept, body[:MAX_BODY_BYTES], truncated)


class HttpTransport(Protocol):
    """Sends exactly one request and never follows redirects."""

    def send(self, request: HttpRequest) -> HttpResponse: ...


class GuardedClient:
    """The only path from the x402 adapter to the network: request validation, the merchant-origin
    allow-list, then one transport call. The origin allow-list is enforced here and only here."""

    def __init__(self, transport: HttpTransport, allow_list: OriginAllowList) -> None:
        self._transport = transport
        self._allow_list = allow_list

    def send(self, request: HttpRequest) -> tuple[Origin, HttpResponse]:
        """Validate the request as it stands now, then forward a fresh copy built from the
        validated values, so nothing changed on the caller's object afterwards can be sent."""
        validate_request(request)
        outgoing = HttpRequest(request.method, request.url, request.headers, request.body)
        origin = self._allow_list.check(outgoing.url)
        response = self._transport.send(outgoing)
        if response.status not in range(100, 600):
            raise X402Refused(X402Refusal.MALFORMED_RESPONSE, f"status {response.status}")
        return origin, response


class StdlibTransport:
    """Single-request http.client transport for operator-run non-paying discovery.

    http.client never follows redirects by itself, which is why it is used instead of urllib's
    opener. TLS uses the platform's default verification.

    Boundary: `send` enforces request validity on its own (`validate_request`: method, exact-`str`
    allow-listed headers, exact-`bytes` body), so calling it directly cannot transmit a forbidden or
    forged header. It does NOT enforce the merchant-origin allow-list: that belongs to
    `GuardedClient`. Called directly, it will contact any http(s) URL that `Origin.of` accepts
    (https, or plain http to loopback, no credentials in the URL). Adapter code must send through
    `GuardedClient`.
    """

    def __init__(self, timeout_seconds: int = TIMEOUT_SECONDS) -> None:
        self._timeout = timeout_seconds

    def send(self, request: HttpRequest) -> HttpResponse:
        """Refuses an invalid request even when called directly, bypassing GuardedClient. The
        method, URL, headers and body are read once, after validation, into locals."""
        validate_request(request)
        method, url, headers, body = request.method, request.url, request.headers, request.body
        parts = urllib.parse.urlsplit(url)
        origin = Origin.of(url)
        connection: http.client.HTTPConnection
        if origin.scheme == "https":
            connection = http.client.HTTPSConnection(
                origin.host, origin.port, timeout=self._timeout
            )
        else:
            connection = http.client.HTTPConnection(origin.host, origin.port, timeout=self._timeout)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        try:
            connection.request(method, path, body=body or None, headers=dict(headers))
            raw = connection.getresponse()
            body = raw.read(MAX_BODY_BYTES + 1)
            return filter_response(
                raw.status, raw.getheaders(), body, truncated=len(body) > MAX_BODY_BYTES
            )
        except (OSError, http.client.HTTPException) as exc:
            raise X402Refused(X402Refusal.TRANSPORT_FAILED, type(exc).__name__) from None
        finally:
            connection.close()
