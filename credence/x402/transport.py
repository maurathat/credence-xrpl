"""The buyer's HTTP boundary for x402 discovery: one non-paying request, no redirects, no secrets.

Merchant responses are untrusted input. This module enforces, before any byte leaves:

- the target origin (scheme, host, port) is on the operator's allow-list;
- the request carries no payment or authentication material: every header name in
  FORBIDDEN_REQUEST_HEADERS (case-insensitive) is refused, so an x402 payment header, a bearer
  token or a cookie can never be sent, to the merchant or to anyone else;
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
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from credence.x402.errors import X402Refusal, X402Refused

# Header names a non-paying discovery request must never carry. Lower case.
FORBIDDEN_REQUEST_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "x-api-key",
        "api-key",
        "x-payment",  # x402 v1 payment header
        "payment-signature",  # x402 v2 payment header
        "x-payment-signature",
    }
)

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


@dataclass(frozen=True)
class HttpRequest:
    """One outgoing request. `body` is transient; only its digest is ever recorded."""

    method: str
    url: str
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes = b""

    def __post_init__(self) -> None:
        if self.method not in ("GET", "POST", "HEAD"):
            raise X402Refused(X402Refusal.UNSUPPORTED_METHOD, self.method)
        for name, _ in self.headers:
            if name.lower() in FORBIDDEN_REQUEST_HEADERS:
                raise X402Refused(X402Refusal.PAYMENT_MATERIAL_REFUSED, name)

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
    """The only path from the x402 adapter to the network: allow-list, then one transport call."""

    def __init__(self, transport: HttpTransport, allow_list: OriginAllowList) -> None:
        self._transport = transport
        self._allow_list = allow_list

    def send(self, request: HttpRequest) -> tuple[Origin, HttpResponse]:
        origin = self._allow_list.check(request.url)
        response = self._transport.send(request)
        if response.status not in range(100, 600):
            raise X402Refused(X402Refusal.MALFORMED_RESPONSE, f"status {response.status}")
        return origin, response


class StdlibTransport:
    """Single-request http.client transport for operator-run non-paying discovery.

    http.client never follows redirects by itself, which is why it is used instead of urllib's
    opener. TLS uses the platform's default verification.
    """

    def __init__(self, timeout_seconds: int = TIMEOUT_SECONDS) -> None:
        self._timeout = timeout_seconds

    def send(self, request: HttpRequest) -> HttpResponse:
        parts = urllib.parse.urlsplit(request.url)
        origin = Origin.of(request.url)
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
            connection.request(
                request.method, path, body=request.body or None, headers=dict(request.headers)
            )
            raw = connection.getresponse()
            body = raw.read(MAX_BODY_BYTES + 1)
            return filter_response(
                raw.status, raw.getheaders(), body, truncated=len(body) > MAX_BODY_BYTES
            )
        except (OSError, http.client.HTTPException) as exc:
            raise X402Refused(X402Refusal.TRANSPORT_FAILED, type(exc).__name__) from None
        finally:
            connection.close()
