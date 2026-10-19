"""Provider A: Nebius Token Factory public API (Tier 1, witnessed).

Tech: Python

Responsibility (System architecture table, build plan):
Calls Nebius Token Factory's public API (no partnership claimed); Credence witnesses the exchange

Tier 1 only. Credence's gateway witnesses the request and response and signs the receipt; Nebius
signs nothing for Credence, so this provider returns no attestation. Its ExecutionProfile is a
Credence mapping of a Nebius model identifier: every field Nebius does not substantiate with
evidence Credence can check is null (NOT ASSERTED). Catalog metadata Nebius publishes (for example a
quantization label) is a provider declaration, not evidence, and is not put into the profile.

Credential boundary: the API key enters only through the NEBIUS_API_KEY environment variable. It is
held in memory by the transport, sent only to the official HTTPS host, and never written, logged,
returned or included in any exception message.

Owner: Inference.
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from enum import Enum
from http.client import HTTPResponse
from typing import Protocol

from credence.core.objects import ExecutionProfile, SamplingPolicy
from credence.core.primitives import ProviderId, SeedPolicy
from credence.inference.provider import (
    AttestationCapability,
    ProviderExecutionError,
    ProviderRequest,
    ProviderResult,
    ProviderUnavailableError,
)

NEBIUS_PROVIDER_ID = ProviderId("nebius-token-factory")
NEBIUS_BASE_URL = "https://api.tokenfactory.nebius.com/v1"
NEBIUS_HOST = "api.tokenfactory.nebius.com"
API_KEY_ENV = "NEBIUS_API_KEY"


class NebiusFailure(Enum):
    """Why a Nebius call failed. Each maps to one typed provider failure."""

    CREDENTIAL_MISSING = "credential_missing"
    CREDENTIAL_MALFORMED = "credential_malformed"
    AUTHENTICATION = "authentication"
    PAYMENT_REQUIRED = "payment_required"
    MODEL_UNAVAILABLE = "model_unavailable"
    RATE_LIMITED = "rate_limited"
    SERVICE_UNAVAILABLE = "service_unavailable"
    UNREACHABLE = "unreachable"
    TIMEOUT = "timeout"
    REQUEST_REJECTED = "request_rejected"
    SERVER_ERROR = "server_error"
    MALFORMED_RESPONSE = "malformed_response"
    UNSUPPORTED_USAGE = "unsupported_usage"


UNAVAILABLE_FAILURES = frozenset(
    {
        NebiusFailure.CREDENTIAL_MISSING,
        NebiusFailure.CREDENTIAL_MALFORMED,
        NebiusFailure.AUTHENTICATION,
        NebiusFailure.PAYMENT_REQUIRED,
        NebiusFailure.MODEL_UNAVAILABLE,
        NebiusFailure.RATE_LIMITED,
        NebiusFailure.SERVICE_UNAVAILABLE,
        NebiusFailure.UNREACHABLE,
    }
)

# Documented HTTP statuses (Nebius API reference, create chat completion).
_STATUS_FAILURES: dict[int, NebiusFailure] = {
    400: NebiusFailure.REQUEST_REJECTED,
    401: NebiusFailure.AUTHENTICATION,
    402: NebiusFailure.PAYMENT_REQUIRED,
    403: NebiusFailure.AUTHENTICATION,
    404: NebiusFailure.MODEL_UNAVAILABLE,
    422: NebiusFailure.REQUEST_REJECTED,
    429: NebiusFailure.RATE_LIMITED,
    500: NebiusFailure.SERVER_ERROR,
    503: NebiusFailure.SERVICE_UNAVAILABLE,
    529: NebiusFailure.SERVICE_UNAVAILABLE,
}


class NebiusUnavailableError(ProviderUnavailableError):
    def __init__(self, failure: NebiusFailure, detail: str = "") -> None:
        super().__init__(f"nebius: {failure.value}" + (f" ({detail})" if detail else ""))
        self.failure = failure


class NebiusExecutionError(ProviderExecutionError):
    def __init__(self, failure: NebiusFailure, detail: str = "") -> None:
        super().__init__(f"nebius: {failure.value}" + (f" ({detail})" if detail else ""))
        self.failure = failure


def nebius_error(
    failure: NebiusFailure, detail: str = ""
) -> ProviderUnavailableError | ProviderExecutionError:
    if failure in UNAVAILABLE_FAILURES:
        return NebiusUnavailableError(failure, detail)
    return NebiusExecutionError(failure, detail)


# --- transport ---------------------------------------------------------------------------------


def credential_well_formed(api_key: str) -> bool:
    """False if the key has leading or trailing whitespace, or any whitespace or control
    character anywhere. Checked before any network call; the key itself is never reported."""
    return not any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in api_key)


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """urllib copies request headers onto a redirect; refusing redirects keeps the key on the
    official host. A 3xx then surfaces as an HTTPError."""

    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


_OPENER = urllib.request.build_opener(_RefuseRedirects)


def _open(request: urllib.request.Request, timeout: float) -> HTTPResponse:
    response: HTTPResponse = _OPENER.open(request, timeout=timeout)
    return response


class NebiusTransport(Protocol):
    def get(self, path: str) -> Mapping[str, object]: ...

    def post(self, path: str, body: Mapping[str, object]) -> Mapping[str, object]: ...


class HttpsNebiusTransport:
    """HTTPS to the official Nebius host only, with the key from NEBIUS_API_KEY."""

    def __init__(
        self, api_key: str, *, base_url: str = NEBIUS_BASE_URL, timeout_seconds: float = 60
    ) -> None:
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme != "https" or parsed.hostname != NEBIUS_HOST:
            raise ValueError(f"Nebius calls go only to https://{NEBIUS_HOST}")
        if not api_key:
            raise nebius_error(NebiusFailure.CREDENTIAL_MISSING)
        if not credential_well_formed(api_key):
            # Refused, never repaired: a padded or control-character key is not silently stripped.
            raise nebius_error(NebiusFailure.CREDENTIAL_MALFORMED)
        self._base = base_url.rstrip("/")
        self.__api_key = api_key
        self._timeout = timeout_seconds
        self.last_status: int | None = None

    @classmethod
    def from_environment(cls, *, timeout_seconds: float = 60) -> HttpsNebiusTransport:
        return cls(os.environ.get(API_KEY_ENV, ""), timeout_seconds=timeout_seconds)

    def __repr__(self) -> str:
        return f"HttpsNebiusTransport(base_url={self._base!r}, api_key=<redacted>)"

    def get(self, path: str) -> Mapping[str, object]:
        return self._send(path, None)

    def post(self, path: str, body: Mapping[str, object]) -> Mapping[str, object]:
        return self._send(path, json.dumps(body).encode("utf-8"))

    def _send(self, path: str, data: bytes | None) -> Mapping[str, object]:
        headers = {"Authorization": f"Bearer {self.__api_key}", "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self._base + path, data=data, method="GET" if data is None else "POST", headers=headers
        )
        try:
            with _open(request, self._timeout) as response:
                raw = response.read()
                self.last_status = response.status
        except urllib.error.HTTPError as exc:
            failure = _STATUS_FAILURES.get(
                exc.code,
                NebiusFailure.SERVER_ERROR if exc.code >= 500 else NebiusFailure.REQUEST_REJECTED,
            )
            raise nebius_error(failure, f"HTTP {exc.code}") from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise nebius_error(NebiusFailure.TIMEOUT) from None
            raise nebius_error(NebiusFailure.UNREACHABLE) from None
        except TimeoutError:
            raise nebius_error(NebiusFailure.TIMEOUT) from None
        try:
            decoded = json.loads(raw)
        except ValueError:
            raise nebius_error(NebiusFailure.MALFORMED_RESPONSE, "body is not JSON") from None
        if not isinstance(decoded, dict):
            raise nebius_error(NebiusFailure.MALFORMED_RESPONSE, "body is not a JSON object")
        return decoded


# --- profile -----------------------------------------------------------------------------------


def nebius_profile(model_identifier: str, *, context_min_tokens: int) -> ExecutionProfile:
    """The Credence mapping of one Nebius model identifier. Only the identifier, the context the
    caller states, and the sampling Credence itself sends are filled in; everything else is
    NOT ASSERTED."""
    return ExecutionProfile(
        model_identifier=model_identifier,
        model_artifact_digest=None,
        tokenizer_digest=None,
        adapter_digest=None,
        chat_template_digest=None,
        runtime=None,
        precision=None,
        quantization=None,
        context_min_tokens=context_min_tokens,
        sampling=SamplingPolicy(temperature_milli=0, seed_policy=SeedPolicy.PROVIDER, seed=None),
    )


# --- provider ----------------------------------------------------------------------------------


def _count(usage: Mapping[str, object], field: str) -> int:
    value = usage.get(field)
    if type(value) is not int or value < 0:
        raise nebius_error(NebiusFailure.MALFORMED_RESPONSE, f"usage.{field}")
    return value


def _detail(usage: Mapping[str, object], group: str, field: str) -> int:
    details = usage.get(group)
    if details is None:
        return 0
    if not isinstance(details, dict):
        raise nebius_error(NebiusFailure.MALFORMED_RESPONSE, f"usage.{group}")
    value = details.get(field)
    if value is None:
        return 0
    if type(value) is not int or value < 0:
        raise nebius_error(NebiusFailure.MALFORMED_RESPONSE, f"usage.{group}.{field}")
    return value


def _numeric_usage(usage: Mapping[str, object]) -> dict[str, object]:
    """The usage object reduced to its integer counts (nested detail objects kept), for evidence."""
    out: dict[str, object] = {}
    for key, value in usage.items():
        if type(value) is int:
            out[key] = value
        elif isinstance(value, dict):
            out[key] = {k: v for k, v in value.items() if type(v) is int or v is None}
        elif value is None:
            out[key] = None
    return out


class NebiusProvider:
    """Tier 1 Provider A. Returns the response and Nebius's reported usage; signs nothing."""

    attestation_capability = AttestationCapability.NONE

    def __init__(
        self,
        profile: ExecutionProfile,
        *,
        transport: NebiusTransport,
        provider_id: ProviderId = NEBIUS_PROVIDER_ID,
    ) -> None:
        self._profile = profile
        self._transport = transport
        self._provider_id = provider_id
        self.last_latency_ms: int | None = None
        self.last_finish_reason: str | None = None
        self.last_usage: Mapping[str, object] | None = None

    @property
    def provider_id(self) -> ProviderId:
        return self._provider_id

    @property
    def profile(self) -> ExecutionProfile:
        return self._profile

    def execute(self, request: ProviderRequest) -> ProviderResult:
        try:
            prompt = request.prompt.decode("utf-8")
        except UnicodeDecodeError:
            raise ProviderExecutionError("prompt is not UTF-8") from None
        temperature = self._profile.sampling.temperature_milli
        started = time.monotonic()
        reply = self._transport.post(
            "/chat/completions",
            {
                "model": self._profile.model_identifier,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": request.quote.max_output_tokens,
                "temperature": 0 if temperature is None else temperature / 1000,
                "stream": False,
            },
        )
        self.last_latency_ms = int((time.monotonic() - started) * 1000)

        if reply.get("model") != self._profile.model_identifier:
            raise nebius_error(NebiusFailure.MALFORMED_RESPONSE, "reply names another model")
        choices = reply.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise nebius_error(NebiusFailure.MALFORMED_RESPONSE, "expected exactly one choice")
        message = choices[0].get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise nebius_error(NebiusFailure.MALFORMED_RESPONSE, "no message content")
        finish = choices[0].get("finish_reason")
        self.last_finish_reason = finish if isinstance(finish, str) else None
        try:
            response = content.encode("utf-8")
        except UnicodeEncodeError:
            raise nebius_error(
                NebiusFailure.MALFORMED_RESPONSE, "content is not valid Unicode"
            ) from None

        usage = reply.get("usage")
        self.last_usage = _numeric_usage(usage) if isinstance(usage, dict) else None
        if not isinstance(usage, dict):
            raise nebius_error(NebiusFailure.MALFORMED_RESPONSE, "no usage")
        input_tokens = _count(usage, "prompt_tokens")
        output_tokens = _count(usage, "completion_tokens")
        # Fail closed on usage categories whose relation to the totals is not established.
        if _detail(usage, "prompt_tokens_details", "cached_tokens"):
            raise nebius_error(NebiusFailure.UNSUPPORTED_USAGE, "cached prompt tokens reported")
        if _detail(usage, "completion_tokens_details", "reasoning_tokens"):
            raise nebius_error(NebiusFailure.UNSUPPORTED_USAGE, "reasoning tokens reported")

        return ProviderResult(
            provider_id=self._provider_id,
            execution_profile_digest=self._profile.digest(),
            response=response,
            input_token_count=input_tokens,
            output_token_count=output_tokens,
            attestation=None,
        )
