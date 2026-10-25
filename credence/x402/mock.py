"""Deterministic in-memory transports for offline x402 tests. No sockets, no clock, no randomness.

MockTransport answers each (method, url) with a scripted HttpResponse and records every request it
was handed, so a test can assert exactly what would have left the machine (method, URL, header
names, body digest) and that nothing carrying payment material was ever attempted.

Owner: x402 (X402-P0).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from credence.x402.errors import X402Refusal, X402Refused
from credence.x402.transport import HttpRequest, HttpResponse, filter_response


@dataclass
class MockTransport:
    """Scripted responses keyed by (method, url). An unscripted request is a transport failure."""

    routes: Mapping[tuple[str, str], tuple[int, list[tuple[str, str]], bytes]] = field(
        default_factory=dict
    )
    sent: list[HttpRequest] = field(default_factory=list)

    def send(self, request: HttpRequest) -> HttpResponse:
        self.sent.append(request)
        route = self.routes.get((request.method, request.url))
        if route is None:
            raise X402Refused(X402Refusal.TRANSPORT_FAILED, "no scripted response")
        status, headers, body = route
        return filter_response(status, headers, body, truncated=False)
