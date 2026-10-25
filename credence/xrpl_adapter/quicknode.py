"""X1q: read-only QuickNode transport for the public XRPL Testnet, built and tested offline.

QuickNode is an optional public-network connectivity provider. It does not replace the pinned local
rippled transport (``RippledTransport``), and nothing here changes that transport's rules.

QuickNode serves XRPL JSON-RPC through Clio (its documented ``server_info`` reports
``clio_version``, and ``server_state`` is answered by a rippled node behind it with
``"forwarded": true``). That gives this module its trust model:

- The endpoint is one explicitly configured HTTPS URL on ``*.quiknode.pro``, read from the
  environment. Its path token is a credential: it never appears in a repr, log line, exception,
  evidence envelope or probe report. Only a one-way fingerprint does.
- TLS is verified (default trust store, hostname check, TLS 1.2 or later). Proxies from the
  environment are ignored and redirects are never followed, so the endpoint cannot be substituted.
- The expected network is explicit (only Testnet, network ID 1). ``server_info`` must report that
  network ID on every identity read. A missing network ID is refused; a different one, or a
  forwarded ``server_state`` that contradicts it, latches the transport closed. Local standalone
  network 21337 and mainnet are refused at configuration time. There is no fallback in either
  direction.
- ``pubkey_node`` comes from whichever rippled node Clio forwarded to. It is sampled and recorded,
  never pinned: a load-balanced hosted service has no permanent node key to pin.
- Trust comes instead from the validated ledger: index and hash from ``server_info`` confirmed by
  ``ledger``, the same index never answered with two hashes (that latches the transport closed),
  the validated ledger never moving backwards (stale reads are refused), a bounded ledger age,
  every account read pinned to the identity's ledger hash, and every transaction ledger inside
  Clio's reported ``complete_ledgers``.
- One QuickNode endpoint is a trusted source, not independent consensus verification.

The transport deliberately has no ``submit``, ``lookup``, ``validated_ledger`` or
``ledger_context`` method, so it cannot stand in for the X0t ``SubmissionTransport``,
``HashLookup`` or ``ValidatedLedgerObserver``, or for the policy signer's ``TrustedLedgerSource``.
``QuickNodeReconciliationView`` is the only binding to the X0t seams. It returns a typed
unavailable result for everything, because the X0t journal's finality and non-inclusion rules need
the pinned-node evidence that a hosted source cannot supply. No QuickNode observation finalizes a
journal entry, emits a settlement outbox event or touches Core.

X1s hardening:

- Every remote response is parsed within size, nesting and number limits. Public reads raise only
  ``QuickNodeError``, and ``observe_payment`` never raises.
- Each HTTP attempt runs under one absolute deadline, enforced by a watchdog that shuts the socket
  down, and retries share a total budget.
- The credential-bearing request target lives only in ``QuickNodeJsonRpcClient._exchange``, which
  returns its failures instead of raising them.

Limitations are documented in the contract.

WebSocket transport is a documented extension (``contracts/QUICKNODE-ADAPTER-CONTRACT.md``), not
implemented. Response shapes follow QuickNode's and rippled's documented API v2 shapes; they were
exercised only against the deterministic fake in ``tests/unit/x1q_quicknode.py``.

Owner: XRPL Integration. X1q and X1s delegated to Da Nang (Claude Code) by Maura.
"""

from __future__ import annotations

import functools
import hashlib
import http.client
import json
import os
import re
import socket
import ssl
import threading
import time
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

from xrpl.core.binarycodec import decode

from credence.xrpl_adapter.multisign import AccountSnapshot, SignerEntry, SignerListSnapshot
from credence.xrpl_adapter.preparation import MICRO_UNITS_PER_TRLUSD, transaction_hash
from credence.xrpl_adapter.rippled import (
    JsonRpcClient,
    TransportRefused,
    parse_complete_ledgers,
    refuse_secret_parameters,
)
from credence.xrpl_adapter.types import (
    Asset,
    LedgerRange,
    LookupDisposition,
    LookupResult,
    LookupUnavailable,
    ValidatedLedgerObservation,
    VerificationDisposition,
)

OBSERVATION_SCHEMA = "credence.xrpl.hosted-payment-observation.v1"
API_VERSION = 2
LOCAL_STANDALONE_NETWORK_ID = 21337
QUICKNODE_HOST_SUFFIX = ".quiknode.pro"
ENV_URL = "CREDENCE_QUICKNODE_XRPL_URL"
ENV_NETWORK = "CREDENCE_QUICKNODE_XRPL_NETWORK"
ENV_LABEL = "CREDENCE_QUICKNODE_XRPL_LABEL"
RIPPLE_EPOCH_OFFSET = 946_684_800
TF_PARTIAL_PAYMENT = 0x00020000
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_JSON_DEPTH = 32
MAX_JSON_INTEGER_DIGITS = 24
MAX_JSON_FLOAT_CHARS = 32
MAX_BLOB_HEX_CHARS = 1024 * 1024
MAX_DROPS = 100_000_000_000_000_000
# XRPL issued amounts: at most 16 significant digits, mantissa exponent -96..80.
MAX_ISSUED_SIGNIFICANT_DIGITS = 16
ISSUED_ADJUSTED_EXPONENT_RANGE = (-81, 95)
_READ_CHUNK = 64 * 1024
_SYNCED_SERVER_STATES = frozenset({"full", "proposing", "validating"})
_RETRYABLE_RPC_ERRORS = frozenset(
    {"tooBusy", "slowDown", "noNetwork", "noCurrent", "noClosed", "lgrNotFound"}
)
_LEDGER_HASH = re.compile(r"^[0-9A-F]{64}$")
_LABEL = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_PAGE_LIMIT = 200
_MAX_PAGES = 10
_MAX_REMEMBERED = 4096
_RECENT_READS = 64


# --- errors --------------------------------------------------------------------------------------


class QuickNodeError(Exception):
    """A typed QuickNode failure. Its message never contains the endpoint URL or its token."""

    retryable = False

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class QuickNodeConfigError(QuickNodeError, ValueError):
    """The configuration is missing, ambiguous or not authorized."""


class NetworkIsolationError(QuickNodeConfigError):
    """A hosted source was bound to a different XRPL network."""


class QuickNodeTimeout(QuickNodeError):
    retryable = True


class QuickNodeRateLimited(QuickNodeError):
    retryable = True

    def __init__(self, code: str, detail: str = "", retry_after: float | None = None) -> None:
        super().__init__(code, detail)
        self.retry_after = retry_after


class QuickNodeUnavailable(QuickNodeError):
    """Transient: the endpoint failed, lagged or is busy. A later read may succeed."""

    retryable = True


class QuickNodeRefused(QuickNodeError):
    """The response is malformed, inconsistent, from the wrong network or otherwise untrusted."""


class _RpcError(RuntimeError):
    def __init__(self, error: str) -> None:
        super().__init__(error)
        self.error = error


def _rpc_error_code(value: object) -> str:
    """A node-reported error token, bounded so it is safe to embed in a typed code."""
    if isinstance(value, str) and _RPC_ERROR_CODE.match(value):
        return value
    return "malformedError"


def _typed_errors[**P, R](method: Callable[P, R]) -> Callable[P, R]:
    """Public transport reads raise only ``QuickNodeError``.

    Any other exception that hostile input still reaches (a parser, codec or arithmetic edge this
    module did not anticipate) becomes ``RESPONSE_PROCESSING_FAILED``, raised outside the
    ``except`` so it carries no cause or context.
    """

    @functools.wraps(method)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        failure: QuickNodeError | None = None
        try:
            return method(*args, **kwargs)
        except QuickNodeError:
            raise
        except Exception:
            failure = QuickNodeRefused("RESPONSE_PROCESSING_FAILED")
        raise failure

    return wrapper


# --- configuration -------------------------------------------------------------------------------


class HostedNetwork(Enum):
    """Public networks a hosted provider may serve to Credence. Only Testnet is authorized."""

    TESTNET = "testnet"

    @property
    def network_id(self) -> int:
        return _HOSTED_NETWORK_IDS[self]

    @classmethod
    def parse(cls, text: str) -> HostedNetwork:
        value = text.strip().lower()
        if value == cls.TESTNET.value:
            return cls.TESTNET
        if value in ("mainnet", "livenet", "0"):
            raise QuickNodeConfigError("MAINNET_NOT_AUTHORIZED")
        if value in ("local", "standalone", str(LOCAL_STANDALONE_NETWORK_ID)):
            raise QuickNodeConfigError(
                "LOCAL_NETWORK_NOT_HOSTED", "network 21337 is the local standalone rippled"
            )
        raise QuickNodeConfigError("UNSUPPORTED_NETWORK", "only 'testnet' is authorized")


_HOSTED_NETWORK_IDS = {HostedNetwork.TESTNET: 1}


class QuickNodeEndpoint:
    """One pinned QuickNode HTTPS endpoint. The URL is a secret; it is never rendered."""

    __slots__ = ("_url", "network", "fingerprint")

    _url: str
    network: HostedNetwork
    fingerprint: str

    def __init__(self, url: str, network: HostedNetwork) -> None:
        if not isinstance(url, str) or not url or url != url.strip():
            raise QuickNodeConfigError("ENDPOINT_MALFORMED")
        if any(ord(character) < 0x21 or ord(character) == 0x7F for character in url):
            raise QuickNodeConfigError("ENDPOINT_MALFORMED")
        try:
            parts = urllib.parse.urlsplit(url)
            port = parts.port
        except ValueError:
            raise QuickNodeConfigError("ENDPOINT_MALFORMED") from None
        if parts.scheme != "https":
            raise QuickNodeConfigError("ENDPOINT_NOT_HTTPS")
        if parts.username is not None or parts.password is not None:
            raise QuickNodeConfigError("ENDPOINT_HAS_USERINFO")
        host = parts.hostname or ""
        if not host.endswith(QUICKNODE_HOST_SUFFIX) or host == QUICKNODE_HOST_SUFFIX[1:]:
            raise QuickNodeConfigError("ENDPOINT_NOT_QUICKNODE")
        if len(host) <= len(QUICKNODE_HOST_SUFFIX):
            raise QuickNodeConfigError("ENDPOINT_NOT_QUICKNODE")
        if port not in (None, 443):
            raise QuickNodeConfigError("ENDPOINT_PORT_NOT_ALLOWED")
        if parts.query or parts.fragment:
            raise QuickNodeConfigError("ENDPOINT_HAS_QUERY")
        labels = host[: -len(QUICKNODE_HOST_SUFFIX)].split(".")
        if any(word in label for label in labels for word in ("mainnet", "devnet")):
            raise QuickNodeConfigError(
                "ENDPOINT_NETWORK_MISMATCH", f"endpoint host names another network than {network}"
            )
        object.__setattr__(self, "_url", url)
        object.__setattr__(self, "network", network)
        object.__setattr__(
            self,
            "fingerprint",
            hashlib.sha256(b"credence.quicknode.endpoint.v1\x00" + url.encode("utf-8")).hexdigest()[
                :16
            ],
        )

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("a QuickNode endpoint is immutable")

    def _request_target(self) -> tuple[str, str]:
        """Host and credential-bearing path. Call only from the frame that sends the request."""
        parts = urllib.parse.urlsplit(self._url)
        return parts.hostname or "", parts.path or "/"

    def __repr__(self) -> str:
        return (
            f"QuickNodeEndpoint(<redacted>{QUICKNODE_HOST_SUFFIX}, fingerprint={self.fingerprint})"
        )

    __str__ = __repr__

    def __reduce__(self) -> Any:
        raise TypeError("a QuickNode endpoint is not serializable")


@dataclass(frozen=True)
class QuickNodeConfig:
    """Explicit network, pinned endpoint and bounded read behavior. No node-key pin exists here."""

    endpoint: QuickNodeEndpoint
    network: HostedNetwork
    label: str = "quicknode"
    timeout_seconds: float = 10.0
    max_attempts: int = 3
    backoff_seconds: float = 0.5
    max_backoff_seconds: float = 5.0
    total_budget_seconds: float = 30.0
    max_validated_ledger_age_seconds: int = 120
    max_clock_skew_seconds: int = 30

    def __post_init__(self) -> None:
        if not isinstance(self.endpoint, QuickNodeEndpoint):
            raise QuickNodeConfigError("ENDPOINT_REQUIRED")
        if not isinstance(self.network, HostedNetwork):
            raise QuickNodeConfigError("NETWORK_NOT_CONFIGURED")
        if self.endpoint.network is not self.network:
            raise NetworkIsolationError("ENDPOINT_NETWORK_MISMATCH")
        if not _LABEL.match(self.label):
            raise QuickNodeConfigError("LABEL_INVALID", "lower-case letters, digits and '-'")
        if not 0 < self.timeout_seconds <= 30:
            raise QuickNodeConfigError("TIMEOUT_OUT_OF_RANGE")
        if not 1 <= self.max_attempts <= 5:
            raise QuickNodeConfigError("ATTEMPTS_OUT_OF_RANGE")
        if not 0 <= self.backoff_seconds <= self.max_backoff_seconds <= 30:
            raise QuickNodeConfigError("BACKOFF_OUT_OF_RANGE")
        if not self.timeout_seconds <= self.total_budget_seconds <= 120:
            raise QuickNodeConfigError("BUDGET_OUT_OF_RANGE", "timeout <= budget <= 120 s")
        if not 0 < self.max_validated_ledger_age_seconds <= 3600:
            raise QuickNodeConfigError("LEDGER_AGE_OUT_OF_RANGE")
        if not 0 <= self.max_clock_skew_seconds <= 300:
            raise QuickNodeConfigError("CLOCK_SKEW_OUT_OF_RANGE")

    @property
    def source_id(self) -> str:
        return f"quicknode:{self.label}:{self.network.value}"

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> QuickNodeConfig | None:
        """``None`` when no endpoint is configured. A configured endpoint needs an explicit network."""
        env = os.environ if environ is None else environ
        url = env.get(ENV_URL, "")
        if not url:
            return None
        network_text = env.get(ENV_NETWORK, "")
        if not network_text:
            raise QuickNodeConfigError("NETWORK_NOT_CONFIGURED", f"set {ENV_NETWORK}=testnet")
        network = HostedNetwork.parse(network_text)
        return cls(
            endpoint=QuickNodeEndpoint(url, network),
            network=network,
            label=env.get(ENV_LABEL, "quicknode") or "quicknode",
        )


# --- HTTPS JSON-RPC client -----------------------------------------------------------------------


def verified_tls_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


class RequestWatchdog:
    """Shuts a request's socket down at its absolute deadline, from a timer thread.

    Shutting a socket down wakes a thread blocked in ``connect``, the TLS handshake or ``recv``
    (Linux and macOS), so a slow or dripping peer cannot extend a request past its deadline.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._socket: socket.socket | None = None
        self.fired = False

    def attach(self, sock: socket.socket) -> None:
        with self._lock:
            self._socket = sock
            fired = self.fired
        if fired:
            _shutdown(sock)

    def fire(self) -> None:
        with self._lock:
            self.fired = True
            sock = self._socket
        if sock is not None:
            _shutdown(sock)


def _shutdown(sock: socket.socket) -> None:
    try:
        # The base-class call skips SSLSocket.shutdown, which would also discard its TLS state
        # underneath a reader in another thread.
        socket.socket.shutdown(sock, socket.SHUT_RDWR)
    except OSError:
        pass


Address = tuple[int, Any]
Resolver = Callable[[str, int, float], Address]
Connector = Callable[[Address, str, float, RequestWatchdog], socket.socket]


def resolve_with_deadline(host: str, port: int, timeout: float) -> Address:
    """``getaddrinfo`` bounded for the caller.

    The lookup itself cannot be cancelled. On timeout it finishes in a daemon thread and its
    result is discarded; the caller has already returned ``TIMEOUT``.
    """
    box: list[object] = []

    def lookup() -> None:
        try:
            box.append(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except OSError:
            box.append(None)

    worker = threading.Thread(target=lookup, name="quicknode-dns", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive() or not box:
        raise TimeoutError("dns")
    found = box[0]
    if not isinstance(found, list) or not found:
        raise OSError("dns")
    family, _, _, _, sockaddr = found[0]
    return family, sockaddr


class QuickNodeJsonRpcClient:
    """HTTPS JSON-RPC to the one configured endpoint under absolute deadlines.

    Each attempt has one deadline (``timeout_seconds``) covering DNS, connect, the TLS handshake,
    the request, the response headers and the whole body; a retry sequence is bounded by
    ``total_budget_seconds``. Retries apply only to timeouts, rate limits and server errors.
    Redirects are never followed and proxies are never used (``http.client`` does neither).

    ``resolver`` and ``connector`` are test seams. The default connector always negotiates verified
    TLS; there is no configuration that disables it.
    """

    def __init__(
        self,
        config: QuickNodeConfig,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        resolver: Resolver | None = None,
        connector: Connector | None = None,
    ) -> None:
        self._config = config
        self._sleep = sleep
        self._clock = clock
        self.tls_context = verified_tls_context()
        self._resolver = resolver or resolve_with_deadline
        self._connector = connector or self._tls_connect

    def __repr__(self) -> str:
        return f"QuickNodeJsonRpcClient({self._config.endpoint!r})"

    def call(self, method: str, params: Mapping[str, Any]) -> bytes:
        refuse_secret_parameters(params)
        body = json.dumps({"method": method, "params": [dict(params)]}, sort_keys=True).encode()
        budget_end = self._clock() + self._config.total_budget_seconds
        attempt = 0
        while True:
            attempt += 1
            now = self._clock()
            if now >= budget_end:
                raise QuickNodeTimeout("TIMEOUT", f"{method}: retry budget exhausted")
            outcome = self._exchange(
                method, body, min(now + self._config.timeout_seconds, budget_end)
            )
            if isinstance(outcome, bytes):
                return outcome
            # Raised here, outside any ``except``: no cause, no context, no transport frames.
            if not outcome.retryable or attempt >= self._config.max_attempts:
                raise outcome
            delay = self._delay(attempt, outcome)
            if self._clock() + delay >= budget_end:
                raise outcome
            self._sleep(delay)

    def _delay(self, attempt: int, error: QuickNodeError) -> float:
        cap = self._config.max_backoff_seconds
        if isinstance(error, QuickNodeRateLimited) and error.retry_after is not None:
            return min(max(error.retry_after, 0.0), cap)
        return float(min(self._config.backoff_seconds * 2 ** (attempt - 1), cap))

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise TimeoutError("deadline")
        return remaining

    def _tls_connect(
        self, address: Address, server_hostname: str, deadline: float, watchdog: RequestWatchdog
    ) -> socket.socket:
        family, sockaddr = address
        raw = socket.socket(family, socket.SOCK_STREAM)
        watchdog.attach(raw)
        try:
            raw.settimeout(self._remaining(deadline))
            raw.connect(sockaddr)
            tls = self.tls_context.wrap_socket(
                raw, server_hostname=server_hostname, do_handshake_on_connect=False
            )
        except BaseException:
            raw.close()
            raise
        watchdog.attach(tls)
        try:
            tls.settimeout(self._remaining(deadline))
            tls.do_handshake()
        except BaseException:
            tls.close()
            raise
        return tls

    def _exchange(self, method: str, body: bytes, deadline: float) -> bytes | QuickNodeError:
        """One request under one absolute deadline.

        The credential-bearing request target exists only in this frame. Every failure is
        returned, never raised, so no exception that saw the target (or a frame holding it)
        outlives this call.
        """
        watchdog = RequestWatchdog()
        timer = threading.Timer(max(deadline - self._clock(), 0.0), watchdog.fire)
        timer.daemon = True
        timer.start()
        connection: http.client.HTTPSConnection | None = None
        sock: socket.socket | None = None
        try:
            host, target = self._config.endpoint._request_target()
            address = self._resolver(host, 443, self._remaining(deadline))
            sock = self._connector(address, host, deadline, watchdog)
            connection = http.client.HTTPSConnection(host, 443, context=self.tls_context)
            connection.set_debuglevel(0)
            connection.sock = sock
            sock.settimeout(self._remaining(deadline))
            connection.request(
                "POST",
                target,
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Connection": "close",
                },
            )
            del target
            response = connection.getresponse()
            if response.status != 200:
                return _http_failure(method, response.status, response.getheader("Retry-After"))
            framing = _framing_refusal(response.headers)
            if framing is not None:
                return QuickNodeRefused(framing, method)
            chunks: list[bytes] = []
            size = 0
            # Completion comes from http.client's framing state, never from the socket. Once the
            # parser has the whole body (Content-Length reached, terminating chunk read, or EOF
            # for a close-delimited body) it closes the response; for a ``Connection: close`` or
            # HTTP/1.0 response that also releases the socket. Nothing touches the socket after.
            while not response.isclosed():
                sock.settimeout(self._remaining(deadline))
                chunk = response.read1(_READ_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    return QuickNodeRefused("RESPONSE_TOO_LARGE", method)
                chunks.append(chunk)
            if watchdog.fired:
                return QuickNodeTimeout("TIMEOUT", method)
            if response.length not in (None, 0):
                return QuickNodeUnavailable("CONNECTION_CLOSED", f"{method}: body incomplete")
            return b"".join(chunks)
        except Exception as exc:  # classified, then dropped: nothing here is chained or re-raised
            return self._failure(method, exc, watchdog, deadline)
        finally:
            timer.cancel()
            for closable in (connection, sock):
                if closable is not None:
                    try:
                        closable.close()
                    except OSError:
                        pass

    def _failure(
        self, method: str, exc: Exception, watchdog: RequestWatchdog, deadline: float
    ) -> QuickNodeError:
        if watchdog.fired or self._clock() >= deadline or isinstance(exc, TimeoutError):
            return QuickNodeTimeout("TIMEOUT", method)
        if isinstance(exc, ssl.SSLError):
            return QuickNodeRefused("TLS_VERIFICATION_FAILED", method)
        if isinstance(exc, (http.client.RemoteDisconnected, http.client.IncompleteRead)):
            return QuickNodeUnavailable("CONNECTION_CLOSED", method)
        if isinstance(exc, http.client.HTTPException):
            return QuickNodeRefused("MALFORMED_HTTP_RESPONSE", method)
        if isinstance(exc, OSError):
            return QuickNodeUnavailable("CONNECTION_FAILED", method)
        return QuickNodeRefused("HTTP_PROTOCOL_ERROR", method)


def _framing_refusal(headers: Any) -> str | None:
    """Refuse ambiguous or oversized body framing before any of the body is read.

    ``http.client`` silently prefers ``Transfer-Encoding`` over ``Content-Length`` and treats an
    unparseable length as "read to EOF"; both would let a hostile peer choose the framing.
    """
    lengths = [value.strip() for value in headers.get_all("Content-Length") or []]
    encodings = [value.strip() for value in headers.get_all("Transfer-Encoding") or []]
    if encodings:
        tokens = [token.strip().lower() for token in ",".join(encodings).split(",")]
        if lengths or tokens != ["chunked"]:
            return "MALFORMED_HTTP_RESPONSE"
    if lengths:
        if len(set(lengths)) != 1:
            return "MALFORMED_HTTP_RESPONSE"
        declared = lengths[0]
        if not (declared.isascii() and declared.isdigit()):
            return "MALFORMED_HTTP_RESPONSE"
        if len(declared) > 12 or int(declared) > MAX_RESPONSE_BYTES:
            return "RESPONSE_TOO_LARGE"
    return None


def _http_failure(method: str, status: int, retry_after_header: str | None) -> QuickNodeError:
    if 300 <= status < 400:
        return QuickNodeRefused("REDIRECT_REFUSED", f"{method}: HTTP {status}")
    if status in (401, 403):
        return QuickNodeRefused("ENDPOINT_AUTH_REJECTED", f"{method}: HTTP {status}")
    if status == 429:
        retry_after: float | None = None
        raw = (retry_after_header or "").strip()
        if raw.isdigit() and len(raw) <= 6:
            retry_after = float(raw)
        return QuickNodeRateLimited("RATE_LIMITED", method, retry_after)
    if 500 <= status < 600:
        return QuickNodeUnavailable("SERVER_ERROR", f"{method}: HTTP {status}")
    return QuickNodeRefused("HTTP_STATUS", f"{method}: HTTP {status}")


# --- observations --------------------------------------------------------------------------------


@dataclass(frozen=True)
class RawRead:
    """One request (never the URL) and the exact response bytes."""

    method: str
    params: str
    response: bytes


@dataclass(frozen=True)
class HostedLedgerIdentity:
    source_id: str
    endpoint_fingerprint: str
    network_id: int
    source_kind: str
    server_version: str
    rippled_version: str | None
    ledger_index: int
    ledger_hash: str
    close_time: int | None
    complete_ledgers: str
    complete_history_ranges: tuple[LedgerRange, ...]
    raw: tuple[RawRead, ...]

    def covers(self, ledger_index: int) -> bool:
        return any(item.first <= ledger_index <= item.last for item in self.complete_history_ranges)


@dataclass(frozen=True)
class NodeIdentitySample:
    """What the rippled node behind the endpoint reported once. Informational; never a pin."""

    node_public_key: str | None
    build_version: str
    network_id: int
    server_state: str
    forwarded: bool
    raw: RawRead


@dataclass(frozen=True)
class HostedAmount:
    """An XRPL amount as observed. XRP is integer drops; an issued value is its exact decimal."""

    currency: str
    issuer: str | None
    value: str

    def micro_units(self) -> int | None:
        """Exact integer micro-units of an issued amount, or ``None`` if XRP or not exact."""
        if self.issuer is None:
            return None
        scaled = Decimal(self.value) * MICRO_UNITS_PER_TRLUSD
        if scaled != scaled.to_integral_value():
            return None
        return int(scaled)


@dataclass(frozen=True)
class HostedTrustLine:
    """One trust line as observed; balance and limit are the ledger's exact decimals."""

    account: str
    peer: str
    currency: str
    balance: str
    limit: str
    limit_peer: str
    no_ripple: bool
    no_ripple_peer: bool


class HostedObservationStatus(Enum):
    VALIDATED = "VALIDATED"
    PROVISIONAL = "PROVISIONAL"
    NOT_FOUND = "NOT_FOUND"
    UNAVAILABLE = "UNAVAILABLE"
    REFUSED = "REFUSED"


@dataclass(frozen=True)
class HostedPaymentObservation:
    """One read-only observation of a Payment by its original hash, bound to one network.

    ``VALIDATED`` means the trusted hosted source reported it in a validated ledger inside its
    complete history, with binary and JSON forms in agreement. It is not XRPL settlement in the
    journal and never Core accounting settlement. ``NOT_FOUND`` never proves non-inclusion.
    """

    status: HostedObservationStatus
    code: str
    transaction_hash: str
    network_id: int
    source_id: str
    endpoint_fingerprint: str
    retryable: bool = False
    ledger_index: int | None = None
    ledger_hash: str | None = None
    result: str | None = None
    account: str | None = None
    destination: str | None = None
    amount: HostedAmount | None = None
    delivered: HostedAmount | None = None
    partial_payment: bool = False
    evidence: bytes = b""


# --- parsing helpers -----------------------------------------------------------------------------


_QUOTE = 0x22
_BACKSLASH = 0x5C
_CLOSER_FOR = {0x5B: 0x5D, 0x7B: 0x7D}  # "[" -> "]", "{" -> "}"
_CLOSERS = frozenset(_CLOSER_FOR.values())
_INTEGER_TEXT = re.compile(r"^-?[0-9]{1,20}$")
_RPC_ERROR_CODE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,47}$")


class _JsonLimit(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _bounded_integer(text: str) -> int:
    if len(text.lstrip("-")) > MAX_JSON_INTEGER_DIGITS:
        raise _JsonLimit("NUMBER_OUT_OF_RANGE")
    return int(text)


def _bounded_float(text: str) -> float:
    if len(text) > MAX_JSON_FLOAT_CHARS:
        raise _JsonLimit("NUMBER_OUT_OF_RANGE")
    number = float(text)
    if number in (float("inf"), float("-inf")):
        raise _JsonLimit("NUMBER_OUT_OF_RANGE")
    return number


def _refuse_constant(text: str) -> object:
    raise _JsonLimit("NUMBER_OUT_OF_RANGE")


def _nesting_code(raw: bytes) -> str | None:
    """Refuse excessive nesting, unterminated strings and mismatched brackets before parsing.

    One forward pass with constant work per byte: no regular expressions, no slicing and no
    recursion, so the cost is linear in the (already size-capped) input. It tracks whether it is
    inside a string and whether the previous byte was an escaping backslash, so an escaped quote
    never ends a string, and it counts ``[]``/``{}`` only outside strings. The stack of expected
    closers never grows beyond ``MAX_JSON_DEPTH``.

    This is a bounded pre-check, not a JSON validator: ``json.loads`` still decides what is valid.
    """
    expected: list[int] = []
    in_string = False
    escaped = False
    for byte in raw:
        if in_string:
            if escaped:
                escaped = False
            elif byte == _BACKSLASH:
                escaped = True
            elif byte == _QUOTE:
                in_string = False
        elif byte == _QUOTE:
            in_string = True
        elif byte in _CLOSER_FOR:
            if len(expected) == MAX_JSON_DEPTH:
                return "RESPONSE_TOO_DEEP"
            expected.append(_CLOSER_FOR[byte])
        elif byte in _CLOSERS:
            if not expected or expected.pop() != byte:
                return "MALFORMED_RESPONSE"
    if in_string or expected:
        return "MALFORMED_RESPONSE"
    return None


def parse_json_bounded(raw: bytes, context: str = "response") -> object:
    """Parse one remote JSON document within size, depth and number limits.

    Raises only ``QuickNodeRefused`` (``RESPONSE_TOO_LARGE``, ``RESPONSE_TOO_DEEP``,
    ``NUMBER_OUT_OF_RANGE`` or ``MALFORMED_RESPONSE``), never chained to the parser's exception.
    """
    if not isinstance(raw, (bytes, bytearray)):
        raise QuickNodeRefused("MALFORMED_RESPONSE", f"{context}: not bytes")
    if len(raw) > MAX_RESPONSE_BYTES:
        raise QuickNodeRefused("RESPONSE_TOO_LARGE", context)
    nesting = _nesting_code(bytes(raw))
    if nesting is not None:
        raise QuickNodeRefused(nesting, context)
    code = "MALFORMED_RESPONSE"
    try:
        return json.loads(
            raw,
            parse_int=_bounded_integer,
            parse_float=_bounded_float,
            parse_constant=_refuse_constant,
        )
    except _JsonLimit as exc:
        code = exc.code
    except (ValueError, RecursionError):
        pass
    raise QuickNodeRefused(code, context)


def _int(value: object, name: str) -> int:
    if isinstance(value, bool):
        raise QuickNodeRefused("MALFORMED_RESPONSE", name)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and _INTEGER_TEXT.match(value):
        return int(value)
    if isinstance(value, float) and value.is_integer() and abs(value) < 2**63:
        return int(value)
    raise QuickNodeRefused("MALFORMED_RESPONSE", name)


def _text(value: object, name: str, limit: int = 64) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise QuickNodeRefused("MALFORMED_RESPONSE", name)
    return value


def _bounded_decimal(text: object, code: str, name: str) -> Decimal:
    """An XRPL decimal within issued-amount precision and exponent range, checked before any
    arithmetic or normalization can run away."""
    if not isinstance(text, str) or not 0 < len(text) <= 48:
        raise QuickNodeRefused(code, name)
    try:
        number = Decimal(text)
    except InvalidOperation:
        raise QuickNodeRefused(code, name) from None
    if not number.is_finite():
        raise QuickNodeRefused(code, name)
    significant = "".join(str(digit) for digit in number.as_tuple().digits).rstrip("0")
    low, high = ISSUED_ADJUSTED_EXPONENT_RANGE
    if number and (
        len(significant) > MAX_ISSUED_SIGNIFICANT_DIGITS or not low <= number.adjusted() <= high
    ):
        raise QuickNodeRefused(code, name)
    return number


def _ledger_hash(value: object) -> str:
    if not isinstance(value, str) or not _LEDGER_HASH.match(value.upper()):
        raise QuickNodeRefused("MALFORMED_RESPONSE", "ledger hash")
    return value.upper()


def _ranges(text: object) -> tuple[LedgerRange, ...]:
    if not isinstance(text, str) or len(text) > 4096:
        raise QuickNodeRefused("MALFORMED_COMPLETE_LEDGERS")
    try:
        return parse_complete_ledgers(text)
    except TransportRefused:
        pass  # refused below, outside the handler, so the refusal carries no context
    raise QuickNodeRefused("MALFORMED_COMPLETE_LEDGERS")


def _amount(value: object, name: str) -> HostedAmount:
    if isinstance(value, str):
        # XRP drops: at most 10^17, so at most 18 digits; checked before int() runs.
        if not value.isdigit() or len(value) > 18 or int(value) > MAX_DROPS:
            raise QuickNodeRefused("MALFORMED_AMOUNT", name)
        return HostedAmount("XRP", None, str(int(value)))
    if not isinstance(value, dict):
        raise QuickNodeRefused("MALFORMED_AMOUNT", name)
    currency, issuer, text = value.get("currency"), value.get("issuer"), value.get("value")
    if (
        not isinstance(currency, str)
        or not 0 < len(currency) <= 40
        or not isinstance(issuer, str)
        or not 0 < len(issuer) <= 64
    ):
        raise QuickNodeRefused("MALFORMED_AMOUNT", name)
    number = _bounded_decimal(text, "MALFORMED_AMOUNT", name)
    code = currency.upper() if len(currency) == 40 else currency
    return HostedAmount(code, issuer, format(number.normalize(), "f"))


def _amount_json(amount: HostedAmount | None) -> dict[str, str | None] | None:
    if amount is None:
        return None
    return {"currency": amount.currency, "issuer": amount.issuer, "value": amount.value}


# --- transport -----------------------------------------------------------------------------------


class QuickNodeXRPLTransport:
    """Read-only, network-bound QuickNode reads with hosted-provider consistency checks.

    State kept between reads (highest validated ledger, ledger hashes, observed transactions, node
    keys) is in memory only. A contradiction latches the transport closed until it is rebuilt.
    """

    def __init__(
        self,
        client: JsonRpcClient,
        config: QuickNodeConfig,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._client = client
        self._config = config
        self._clock = clock
        self._highest_validated = 0
        self._ledger_hashes: dict[int, str] = {}
        self._transactions: dict[str, tuple[object, ...]] = {}
        self._node_keys: list[str] = []
        self._contradiction: str | None = None
        self._recent: list[RawRead] = []

    def __repr__(self) -> str:
        return f"QuickNodeXRPLTransport({self._config.source_id}, {self._config.endpoint!r})"

    @property
    def network_id(self) -> int:
        return self._config.network.network_id

    @property
    def source_id(self) -> str:
        return self._config.source_id

    @property
    def endpoint_fingerprint(self) -> str:
        return self._config.endpoint.fingerprint

    @property
    def contradiction(self) -> str | None:
        return self._contradiction

    @property
    def observed_node_public_keys(self) -> tuple[str, ...]:
        return tuple(self._node_keys)

    def recent_reads(self) -> tuple[RawRead, ...]:
        return tuple(self._recent)

    # --- RPC -------------------------------------------------------------------------------------

    def _call(
        self, method: str, params: Mapping[str, Any]
    ) -> tuple[dict[str, Any], RawRead, dict[str, Any]]:
        if self._contradiction is not None:
            raise QuickNodeRefused("SOURCE_CONTRADICTED", self._contradiction)
        request = {**params, "api_version": API_VERSION}
        refuse_secret_parameters(request)
        raw = self._client.call(method, request)
        document = parse_json_bounded(raw, method)
        read = RawRead(method, json.dumps(request, sort_keys=True), bytes(raw))
        self._recent = [*self._recent[-(_RECENT_READS - 1) :], read]
        if not isinstance(document, dict):
            raise QuickNodeRefused("MALFORMED_RESPONSE", f"{method}: not an object")
        if "error" in document and "result" not in document:
            raise _RpcError(_rpc_error_code(document.get("error")))
        result = document.get("result")
        if not isinstance(result, dict):
            raise QuickNodeRefused("MALFORMED_RESPONSE", f"{method}: no result")
        if result.get("status") == "error" or "error" in result:
            raise _RpcError(_rpc_error_code(result.get("error", "unknown")))
        if result.get("status", "success") != "success":
            raise QuickNodeRefused("MALFORMED_RESPONSE", f"{method}: status")
        return result, read, document

    def _call_checked(
        self, method: str, params: Mapping[str, Any]
    ) -> tuple[dict[str, Any], RawRead, dict[str, Any]]:
        """``_call`` with every node-reported error as a typed QuickNode error."""
        try:
            return self._call(method, params)
        except _RpcError as exc:
            if exc.error in _RETRYABLE_RPC_ERRORS:
                raise QuickNodeUnavailable(f"RPC_{exc.error}", method) from None
            raise QuickNodeRefused(f"RPC_{exc.error}", method) from None

    def _latch(self, code: str, detail: str) -> QuickNodeRefused:
        self._contradiction = f"{code}: {detail}"
        return QuickNodeRefused(code, detail)

    def _record_ledger(self, ledger_index: int, ledger_hash: str) -> None:
        known = self._ledger_hashes.get(ledger_index)
        if known is not None and known != ledger_hash:
            raise self._latch("LEDGER_HASH_CONTRADICTION", f"ledger {ledger_index}")
        if known is None:
            if len(self._ledger_hashes) >= _MAX_REMEMBERED:
                self._ledger_hashes.pop(next(iter(self._ledger_hashes)))
            self._ledger_hashes[ledger_index] = ledger_hash

    def _check_network(self, observed: object) -> None:
        if observed is None:
            raise QuickNodeRefused("NETWORK_IDENTITY_MISSING")
        if isinstance(observed, bool) or not isinstance(observed, int):
            raise QuickNodeRefused("MALFORMED_RESPONSE", "network_id")
        if observed != self.network_id:
            raise self._latch("NETWORK_MISMATCH", f"observed network_id {observed}")

    # --- validated ledger identity ---------------------------------------------------------------

    @_typed_errors
    def validated_ledger_identity(self) -> HostedLedgerIdentity:
        info_result, info_raw, _ = self._call_checked("server_info", {})
        info = info_result.get("info")
        if not isinstance(info, dict):
            raise QuickNodeRefused("MALFORMED_RESPONSE", "server_info")
        self._check_network(info.get("network_id"))
        is_clio = "clio_version" in info
        if not is_clio and info.get("server_state") not in _SYNCED_SERVER_STATES:
            raise QuickNodeUnavailable("NODE_NOT_SYNCED", str(info.get("server_state")))
        validated = info.get("validated_ledger")
        if not isinstance(validated, dict):
            raise QuickNodeRefused("NO_VALIDATED_LEDGER")
        ledger_index = _int(validated.get("seq"), "validated_ledger.seq")
        ledger_hash = _ledger_hash(validated.get("hash"))
        complete = info.get("complete_ledgers", "")
        ranges = _ranges(complete)
        if not any(item.first <= ledger_index <= item.last for item in ranges):
            raise QuickNodeRefused("VALIDATED_LEDGER_OUTSIDE_COMPLETE_HISTORY", str(ledger_index))
        if ledger_index < self._highest_validated:
            raise QuickNodeUnavailable(
                "STALE_VALIDATED_LEDGER", f"{ledger_index} < {self._highest_validated}"
            )
        self._record_ledger(ledger_index, ledger_hash)

        ledger_result, ledger_raw, _ = self._call_checked(
            "ledger", {"ledger_index": ledger_index, "transactions": False, "expand": False}
        )
        if ledger_result.get("validated") is not True:
            raise QuickNodeRefused("LEDGER_NOT_VALIDATED")
        if _int(ledger_result.get("ledger_index"), "ledger_index") != ledger_index:
            raise QuickNodeRefused("LEDGER_IDENTITY_MISMATCH")
        header = ledger_result.get("ledger")
        header = header if isinstance(header, dict) else {}
        confirmed = ledger_result.get("ledger_hash", header.get("ledger_hash"))
        self._record_ledger(ledger_index, _ledger_hash(confirmed))
        close_time = header.get("close_time")
        age: float
        if isinstance(close_time, int) and not isinstance(close_time, bool):
            age = self._clock() - (close_time + RIPPLE_EPOCH_OFFSET)
        elif isinstance(validated.get("age"), int) and not isinstance(validated.get("age"), bool):
            close_time = None
            age = float(validated["age"])
        else:
            raise QuickNodeRefused("LEDGER_AGE_UNKNOWN")
        if age < -self._config.max_clock_skew_seconds:
            raise QuickNodeRefused("LEDGER_CLOSE_TIME_IN_FUTURE", f"{age:.0f}s")
        if age > self._config.max_validated_ledger_age_seconds:
            raise QuickNodeUnavailable("STALE_VALIDATED_LEDGER", f"age {age:.0f}s")
        self._highest_validated = max(self._highest_validated, ledger_index)
        rippled_version = info.get("rippled_version")
        return HostedLedgerIdentity(
            source_id=self.source_id,
            endpoint_fingerprint=self.endpoint_fingerprint,
            network_id=self.network_id,
            source_kind="clio" if is_clio else "rippled",
            server_version=str(info.get("clio_version") if is_clio else info.get("build_version")),
            rippled_version=None if rippled_version is None else str(rippled_version),
            ledger_index=ledger_index,
            ledger_hash=ledger_hash,
            close_time=close_time,
            complete_ledgers=str(complete),
            complete_history_ranges=ranges,
            raw=(info_raw, ledger_raw),
        )

    @_typed_errors
    def validated_ledger_observation(self, account: str) -> ValidatedLedgerObservation:
        """History coverage and sender state in the X0t type, for reporting only.

        ``QuickNodeReconciliationView`` does not supply this to ``PaymentService``: a hosted
        observation is not accepted as a submission anchor or as non-inclusion evidence.
        """
        identity = self.validated_ledger_identity()
        snapshot = self.account_state(account, identity)
        ranges = identity.complete_history_ranges
        return ValidatedLedgerObservation(
            ledger_index=identity.ledger_index,
            complete_history_min=ranges[0].first if ranges else None,
            complete_history_max=ranges[-1].last if ranges else None,
            history_complete=bool(ranges),
            ledger_hash=identity.ledger_hash,
            source_id=identity.source_id,
            complete_history_ranges=ranges,
            account=account if snapshot.exists else None,
            account_sequence=snapshot.sequence,
            available_tickets=None,
        )

    @_typed_errors
    def sample_node_identity(self) -> NodeIdentitySample:
        """Ask the rippled node behind Clio who it is. Recorded to study stability; never pinned."""
        result, raw, document = self._call_checked("server_state", {})
        state = result.get("state")
        if not isinstance(state, dict):
            raise QuickNodeRefused("MALFORMED_RESPONSE", "server_state")
        self._check_network(state.get("network_id"))
        key = state.get("pubkey_node")
        if key is not None and not isinstance(key, str):
            raise QuickNodeRefused("MALFORMED_RESPONSE", "pubkey_node")
        if key and key not in self._node_keys:
            self._node_keys.append(key)
        return NodeIdentitySample(
            node_public_key=key,
            build_version=str(state.get("build_version", "")),
            network_id=self.network_id,
            server_state=str(state.get("server_state", "")),
            forwarded=document.get("forwarded") is True,
            raw=raw,
        )

    # --- account state ---------------------------------------------------------------------------

    def _pinned(self, result: Mapping[str, Any], identity: HostedLedgerIdentity) -> None:
        if result.get("validated") is not True:
            raise QuickNodeRefused("LEDGER_NOT_VALIDATED")
        if _ledger_hash(result.get("ledger_hash")) != identity.ledger_hash:
            raise QuickNodeRefused("LEDGER_STATE_INCONSISTENT", "response is from another ledger")
        if _int(result.get("ledger_index"), "ledger_index") != identity.ledger_index:
            raise QuickNodeRefused("LEDGER_STATE_INCONSISTENT", "response is from another ledger")

    @_typed_errors
    def account_state(self, account: str, identity: HostedLedgerIdentity) -> AccountSnapshot:
        """Account flags, RegularKey, Sequence, balance and SignerList on the identity's ledger."""
        try:
            result, _, _ = self._call(
                "account_info",
                {"account": account, "ledger_hash": identity.ledger_hash, "signer_lists": True},
            )
        except _RpcError as exc:
            if exc.error == "actNotFound":
                return AccountSnapshot(
                    account=account,
                    exists=False,
                    validated=True,
                    ledger_index=identity.ledger_index,
                    ledger_hash=identity.ledger_hash,
                )
            raise QuickNodeRefused("ACCOUNT_INFO_FAILED", exc.error) from None
        self._pinned(result, identity)
        data = result.get("account_data")
        if not isinstance(data, dict) or data.get("Account") != account:
            raise QuickNodeRefused("MALFORMED_RESPONSE", "account_data")
        regular_key = data.get("RegularKey")
        transfer_rate = data.get("TransferRate")
        return AccountSnapshot(
            account=account,
            exists=True,
            validated=True,
            ledger_index=identity.ledger_index,
            ledger_hash=identity.ledger_hash,
            flags=_int(data.get("Flags", 0), "Flags"),
            regular_key=None if regular_key is None else str(regular_key),
            sequence=_int(data.get("Sequence"), "Sequence"),
            balance_drops=_int(data.get("Balance"), "Balance"),
            owner_count=_int(data.get("OwnerCount", 0), "OwnerCount"),
            transfer_rate=None if transfer_rate is None else _int(transfer_rate, "TransferRate"),
            signer_list=_signer_list(account, result, data),
        )

    def _paged(
        self, method: str, params: Mapping[str, Any], key: str, identity: HostedLedgerIdentity
    ) -> list[Any]:
        items: list[Any] = []
        marker: object = None
        for _ in range(_MAX_PAGES):
            request = {**params, "ledger_hash": identity.ledger_hash, "limit": _PAGE_LIMIT}
            if marker is not None:
                request["marker"] = marker
            try:
                result, _, _ = self._call(method, request)
            except _RpcError as exc:
                raise QuickNodeRefused(f"{method.upper()}_FAILED", exc.error) from None
            self._pinned(result, identity)
            page = result.get(key)
            if not isinstance(page, list):
                raise QuickNodeRefused("MALFORMED_RESPONSE", key)
            items.extend(page)
            marker = result.get("marker")
            if marker is None:
                return items
        raise QuickNodeRefused("TOO_MANY_PAGES", method)

    @_typed_errors
    def account_objects(
        self, account: str, identity: HostedLedgerIdentity, object_type: str | None = None
    ) -> tuple[dict[str, Any], ...]:
        params: dict[str, Any] = {"account": account}
        if object_type is not None:
            params["type"] = object_type
        objects = self._paged("account_objects", params, "account_objects", identity)
        for item in objects:
            if not isinstance(item, dict) or not isinstance(item.get("LedgerEntryType"), str):
                raise QuickNodeRefused("MALFORMED_RESPONSE", "account object")
        return tuple(objects)

    @_typed_errors
    def account_lines(
        self, account: str, identity: HostedLedgerIdentity, peer: str | None = None
    ) -> tuple[HostedTrustLine, ...]:
        params: dict[str, Any] = {"account": account}
        if peer is not None:
            params["peer"] = peer
        lines: list[HostedTrustLine] = []
        for item in self._paged("account_lines", params, "lines", identity):
            if not isinstance(item, dict):
                raise QuickNodeRefused("MALFORMED_RESPONSE", "trust line")
            values = [item.get(name) for name in ("account", "currency", "balance", "limit")]
            if not all(isinstance(value, str) for value in values):
                raise QuickNodeRefused("MALFORMED_RESPONSE", "trust line")
            for name in ("balance", "limit", "limit_peer"):
                _decimal_text(item.get(name, "0"), name)
            lines.append(
                HostedTrustLine(
                    account=account,
                    peer=str(item["account"]),
                    currency=str(item["currency"]),
                    balance=str(item["balance"]),
                    limit=str(item["limit"]),
                    limit_peer=str(item.get("limit_peer", "0")),
                    no_ripple=item.get("no_ripple") is True,
                    no_ripple_peer=item.get("no_ripple_peer") is True,
                )
            )
        return tuple(lines)

    # --- payment observation by original hash ----------------------------------------------------

    def observe_payment(self, transaction_hash_value: str) -> HostedPaymentObservation:
        """Observe one Payment by its original hash. Never raises for ledger or transport faults."""
        expected = str(transaction_hash_value).upper()
        if not _LEDGER_HASH.match(expected):
            return self._failed(expected, HostedObservationStatus.REFUSED, "INVALID_HASH", False)
        try:
            return self._observe(expected)
        except _RpcError as exc:
            if exc.error == "txnNotFound":
                if expected in self._transactions:
                    error = self._latch("TRANSACTION_OBSERVATION_CONTRADICTION", expected)
                    return self._failed(
                        expected, HostedObservationStatus.REFUSED, error.code, False
                    )
                return self._failed(
                    expected, HostedObservationStatus.NOT_FOUND, "txnNotFound", False
                )
            if exc.error in _RETRYABLE_RPC_ERRORS:
                return self._failed(
                    expected, HostedObservationStatus.UNAVAILABLE, f"RPC_{exc.error}", True
                )
            return self._failed(
                expected, HostedObservationStatus.REFUSED, f"RPC_{exc.error}", False
            )
        except QuickNodeError as exc:
            status = (
                HostedObservationStatus.UNAVAILABLE
                if exc.retryable
                else HostedObservationStatus.REFUSED
            )
            return self._failed(expected, status, exc.code, exc.retryable)
        except Exception:  # hostile input reached an edge this module did not bound: fail closed
            return self._failed(
                expected, HostedObservationStatus.REFUSED, "RESPONSE_PROCESSING_FAILED", False
            )

    def _failed(
        self, expected: str, status: HostedObservationStatus, code: str, retryable: bool
    ) -> HostedPaymentObservation:
        return HostedPaymentObservation(
            status=status,
            code=code,
            transaction_hash=expected,
            network_id=self.network_id,
            source_id=self.source_id,
            endpoint_fingerprint=self.endpoint_fingerprint,
            retryable=retryable,
        )

    def _check_transaction_network(self, transaction: Mapping[str, Any]) -> None:
        stated = transaction.get("NetworkID")
        if self.network_id <= 1024:
            # Networks with ID 1024 or below forbid the field; its presence names another network.
            if stated is not None:
                raise QuickNodeRefused("TRANSACTION_NETWORK_MISMATCH", f"NetworkID {stated}")
        elif _int(stated, "NetworkID") != self.network_id:
            raise QuickNodeRefused("TRANSACTION_NETWORK_MISMATCH", f"NetworkID {stated}")

    def _observe(self, expected: str) -> HostedPaymentObservation:
        identity = self.validated_ledger_identity()
        binary, binary_raw, _ = self._call("tx", {"transaction": expected, "binary": True})
        blob_hex = binary.get("tx_blob")
        if not isinstance(blob_hex, str) or not 0 < len(blob_hex) <= MAX_BLOB_HEX_CHARS:
            raise QuickNodeRefused("MALFORMED_RESPONSE", "tx_blob")
        try:
            blob = bytes.fromhex(blob_hex)
            transaction = decode(blob_hex.upper())
        except Exception:  # xrpl-py raises several codec error types
            raise QuickNodeRefused("MALFORMED_RESPONSE", "tx_blob") from None
        if transaction_hash(blob) != expected or str(binary.get("hash", "")).upper() != expected:
            raise QuickNodeRefused("EVIDENCE_HASH_MISMATCH")
        if transaction.get("TransactionType") != "Payment":
            raise QuickNodeRefused("NOT_A_PAYMENT", str(transaction.get("TransactionType")))
        self._check_transaction_network(transaction)
        account, destination = transaction.get("Account"), transaction.get("Destination")
        if not isinstance(account, str) or not isinstance(destination, str):
            raise QuickNodeRefused("MALFORMED_RESPONSE", "Payment accounts")
        amount = _amount(transaction.get("Amount"), "Amount")
        partial = bool(_int(transaction.get("Flags", 0), "Flags") & TF_PARTIAL_PAYMENT)

        if binary.get("validated") is not True:
            return HostedPaymentObservation(
                status=HostedObservationStatus.PROVISIONAL,
                code="NOT_VALIDATED",
                transaction_hash=expected,
                network_id=self.network_id,
                source_id=self.source_id,
                endpoint_fingerprint=self.endpoint_fingerprint,
                retryable=True,
                account=account,
                destination=destination,
                amount=amount,
                partial_payment=partial,
            )

        ledger_index = _int(binary.get("ledger_index"), "ledger_index")
        if ledger_index > identity.ledger_index:
            # Behind a load balancer the tx backend may be ahead of the identity read: retry later.
            raise QuickNodeUnavailable("LEDGER_BEYOND_VALIDATED", str(ledger_index))
        if not identity.covers(ledger_index):
            raise QuickNodeRefused("LEDGER_OUTSIDE_COMPLETE_HISTORY", str(ledger_index))
        meta_hex = binary.get("meta_blob", binary.get("meta"))
        if not isinstance(meta_hex, str) or len(meta_hex) > MAX_BLOB_HEX_CHARS:
            raise QuickNodeRefused("MALFORMED_RESPONSE", "meta_blob")
        try:
            binary_meta = decode(meta_hex)
        except Exception:  # xrpl-py raises several codec error types
            raise QuickNodeRefused("MALFORMED_RESPONSE", "meta_blob") from None

        detail, detail_raw, _ = self._call_checked("tx", {"transaction": expected, "binary": False})
        meta = detail.get("meta")
        tx_json = detail.get("tx_json")
        if (
            detail.get("validated") is not True
            or str(detail.get("hash", "")).upper() != expected
            or _int(detail.get("ledger_index"), "ledger_index") != ledger_index
            or not isinstance(meta, dict)
            or not isinstance(tx_json, dict)
            or meta.get("TransactionResult") != binary_meta.get("TransactionResult")
        ):
            raise QuickNodeRefused("EVIDENCE_INCONSISTENT", "binary and JSON lookups disagree")
        stated_amount = tx_json.get("DeliverMax", tx_json.get("Amount"))
        if (
            tx_json.get("TransactionType") != "Payment"
            or tx_json.get("Account") != account
            or tx_json.get("Destination") != destination
            or _amount(stated_amount, "tx_json amount") != amount
        ):
            raise QuickNodeRefused("EVIDENCE_INCONSISTENT", "binary and JSON transactions disagree")

        ledger_result, ledger_raw, _ = self._call_checked(
            "ledger", {"ledger_index": ledger_index, "transactions": False, "expand": False}
        )
        if ledger_result.get("validated") is not True:
            raise QuickNodeRefused("LEDGER_NOT_VALIDATED")
        if _int(ledger_result.get("ledger_index"), "ledger_index") != ledger_index:
            raise QuickNodeRefused("LEDGER_HASH_MISMATCH", "ledger index differs")
        header = ledger_result.get("ledger")
        header = header if isinstance(header, dict) else {}
        ledger_hash = _ledger_hash(ledger_result.get("ledger_hash", header.get("ledger_hash")))
        self._record_ledger(ledger_index, ledger_hash)
        for stated in (binary.get("ledger_hash"), detail.get("ledger_hash")):
            if stated is not None and _ledger_hash(stated) != ledger_hash:
                raise QuickNodeRefused("LEDGER_HASH_MISMATCH")

        result_code = meta.get("TransactionResult")
        if not isinstance(result_code, str) or not result_code:
            raise QuickNodeRefused("MALFORMED_RESPONSE", "TransactionResult")
        delivered = _delivered(binary_meta, meta, amount, result_code, partial)
        self._record_transaction(expected, (ledger_index, ledger_hash, result_code, delivered))
        envelope = json.dumps(
            {
                "schema": OBSERVATION_SCHEMA,
                "trust": "single-hosted-source",
                "status": HostedObservationStatus.VALIDATED.value,
                "network_id": self.network_id,
                "ledger_index": ledger_index,
                "ledger_hash": ledger_hash,
                "transaction_hash": expected,
                "signed_transaction": blob.hex().upper(),
                "result": result_code,
                "amount": _amount_json(amount),
                "delivered": _amount_json(delivered),
                "partial_payment": partial,
                "validated_identity": {
                    "complete_ledgers": identity.complete_ledgers,
                    "ledger_hash": identity.ledger_hash,
                    "ledger_index": identity.ledger_index,
                    "network_id": identity.network_id,
                    "rippled_version": identity.rippled_version,
                    "server_version": identity.server_version,
                    "source_kind": identity.source_kind,
                },
                "source_id": self.source_id,
                "endpoint_fingerprint": self.endpoint_fingerprint,
                "raw": [
                    {
                        "method": item.method,
                        "params": item.params,
                        "response": item.response.decode("utf-8", errors="replace"),
                    }
                    for item in (*identity.raw, binary_raw, detail_raw, ledger_raw)
                ],
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return HostedPaymentObservation(
            status=HostedObservationStatus.VALIDATED,
            code=result_code,
            transaction_hash=expected,
            network_id=self.network_id,
            source_id=self.source_id,
            endpoint_fingerprint=self.endpoint_fingerprint,
            ledger_index=ledger_index,
            ledger_hash=ledger_hash,
            result=result_code,
            account=account,
            destination=destination,
            amount=amount,
            delivered=delivered,
            partial_payment=partial,
            evidence=envelope,
        )

    def _record_transaction(self, expected: str, facts: tuple[object, ...]) -> None:
        known = self._transactions.get(expected)
        if known is not None and known != facts:
            raise self._latch("TRANSACTION_OBSERVATION_CONTRADICTION", expected)
        if known is None:
            if len(self._transactions) >= _MAX_REMEMBERED:
                self._transactions.pop(next(iter(self._transactions)))
            self._transactions[expected] = facts


def _decimal_text(value: object, name: str) -> None:
    _bounded_decimal(value, "MALFORMED_RESPONSE", name)


def _signer_list(
    account: str, result: Mapping[str, Any], data: Mapping[str, Any]
) -> SignerListSnapshot | None:
    lists = result.get("signer_lists", data.get("signer_lists", []))
    if not isinstance(lists, list):
        raise QuickNodeRefused("MALFORMED_RESPONSE", "signer_lists")
    if not lists:
        return None
    if len(lists) != 1 or not isinstance(lists[0], dict):
        raise QuickNodeRefused("MALFORMED_RESPONSE", "more than one SignerList")
    entries: list[SignerEntry] = []
    for wrapper in lists[0].get("SignerEntries", []):
        entry = wrapper.get("SignerEntry") if isinstance(wrapper, dict) else None
        if not isinstance(entry, dict) or not isinstance(entry.get("Account"), str):
            raise QuickNodeRefused("MALFORMED_RESPONSE", "SignerEntry")
        entries.append(SignerEntry(entry["Account"], _int(entry.get("SignerWeight"), "weight")))
    return SignerListSnapshot(
        account=account,
        quorum=_int(lists[0].get("SignerQuorum"), "SignerQuorum"),
        entries=tuple(entries),
    )


def _delivered(
    binary_meta: Mapping[str, Any],
    meta: Mapping[str, Any],
    amount: HostedAmount,
    result_code: str,
    partial: bool,
) -> HostedAmount | None:
    """What moved: binary metadata is authoritative and the JSON ``delivered_amount`` must agree."""
    observed = meta.get("delivered_amount")
    if result_code != "tesSUCCESS":
        if "DeliveredAmount" in binary_meta or observed is not None:
            raise QuickNodeRefused("EVIDENCE_INCONSISTENT", "failed Payment reports a delivery")
        return None
    if "DeliveredAmount" in binary_meta:
        binary = _amount(binary_meta["DeliveredAmount"], "DeliveredAmount")
    elif partial:
        raise QuickNodeRefused("DELIVERED_AMOUNT_UNAVAILABLE", "partial payment")
    else:
        binary = amount
    if observed == "unavailable" or observed is None:
        raise QuickNodeRefused("DELIVERED_AMOUNT_UNAVAILABLE")
    if _amount(observed, "delivered_amount") != binary:
        raise QuickNodeRefused(
            "EVIDENCE_INCONSISTENT", "binary and JSON delivered amounts disagree"
        )
    return binary


# --- X0t seam binding (typed unsupported) --------------------------------------------------------


_VIEW_CODES = {
    HostedObservationStatus.VALIDATED: "QUICKNODE_FINALITY_UNSUPPORTED",
    HostedObservationStatus.PROVISIONAL: "QUICKNODE_PROVISIONAL_UNSUPPORTED",
    HostedObservationStatus.NOT_FOUND: "QUICKNODE_NON_INCLUSION_UNSUPPORTED",
}


class QuickNodeReconciliationView:
    """QuickNode bound to the X0t ``HashLookup`` and ``ValidatedLedgerObserver`` seams, read-only.

    The X0t journal finalizes only on pinned trusted-node evidence (network, build and node key on
    every lookup) and proves expiry only from a pre-broadcast anchor on the same source. A hosted
    source can supply neither, so every lookup is ``UNAVAILABLE`` with a typed ``QUICKNODE_*``
    detail and the observer refuses. ``PaymentService`` therefore keeps the entry ``UNKNOWN``: it is
    never finalized, never produces an outbox event, and later reconciles normally through an
    authorized source. ``last_observation`` keeps the hosted observation for operator reporting.
    """

    def __init__(self, transport: QuickNodeXRPLTransport, *, journal_network_id: int) -> None:
        if journal_network_id != transport.network_id:
            raise NetworkIsolationError(
                "NETWORK_ISOLATION",
                f"journal network {journal_network_id} is not hosted network {transport.network_id}",
            )
        self._transport = transport
        self.last_observation: HostedPaymentObservation | None = None

    def lookup(self, transaction_hash: str) -> LookupResult:
        observation = self._transport.observe_payment(transaction_hash)
        self.last_observation = observation
        detail = _VIEW_CODES.get(observation.status, f"QUICKNODE_{observation.code}")
        return LookupResult(LookupDisposition.UNAVAILABLE, detail=detail)

    def validated_ledger(self, account: str) -> ValidatedLedgerObservation:
        raise LookupUnavailable("QUICKNODE_LEDGER_ANCHOR_UNSUPPORTED")


# --- advisory assessment against an intent -------------------------------------------------------


@dataclass(frozen=True)
class HostedPaymentExpectation:
    """The Payment a caller expects, for an advisory read-only comparison."""

    transaction_hash: str
    network_id: int
    account: str
    destination: str
    asset: Asset
    amount_micro_units: int


@dataclass(frozen=True)
class HostedPaymentAssessment:
    """Advisory classification in the X0t verifier's terms. Never a settlement authority."""

    disposition: VerificationDisposition
    code: str
    detail: str
    delivered_amount_micro_units: int | None = None
    settlement_authority: bool = False


def assess_hosted_payment(
    observation: HostedPaymentObservation, expected: HostedPaymentExpectation
) -> HostedPaymentAssessment:
    """Compare a hosted observation with an expected Payment, in the X0t verifier's order.

    Contradictions with the intent are ``UNRESOLVED``; a validated non-``tesSUCCESS`` result is
    ``VALIDATED_FAILURE``; a successful Payment that delivered the wrong asset or amount is
    ``VALIDATED_UNEXPECTED_DELIVERY``. Nothing here writes a journal or settles anything.
    """
    unresolved = VerificationDisposition.UNRESOLVED
    if observation.network_id != expected.network_id:
        return HostedPaymentAssessment(
            unresolved, "NETWORK_MISMATCH", "hosted evidence is from another network"
        )
    if observation.transaction_hash != expected.transaction_hash.upper():
        return HostedPaymentAssessment(unresolved, "HASH_MISMATCH", "different transaction")
    if observation.status is HostedObservationStatus.PROVISIONAL:
        return HostedPaymentAssessment(
            VerificationDisposition.PROVISIONAL, "NOT_VALIDATED", "ledger result is provisional"
        )
    if observation.status is not HostedObservationStatus.VALIDATED:
        return HostedPaymentAssessment(unresolved, observation.code, observation.status.value)
    signed = observation.amount
    checks = (
        (observation.account == expected.account, "ACCOUNT_MISMATCH"),
        (observation.destination == expected.destination, "DESTINATION_MISMATCH"),
        (
            signed is not None
            and signed.currency == expected.asset.currency_code
            and signed.issuer == expected.asset.issuer,
            "ASSET_MISMATCH",
        ),
        (
            signed is not None and signed.micro_units() == expected.amount_micro_units,
            "AMOUNT_MISMATCH",
        ),
    )
    for passed, code in checks:
        if not passed:
            return HostedPaymentAssessment(
                unresolved, code, "observed transaction contradicts the expected Payment"
            )
    if observation.result != "tesSUCCESS":
        return HostedPaymentAssessment(
            VerificationDisposition.VALIDATED_FAILURE,
            str(observation.result),
            "hosted source reports a validated transaction failure",
        )
    delivered = observation.delivered
    if (
        delivered is None
        or delivered.currency != expected.asset.currency_code
        or delivered.issuer != expected.asset.issuer
    ):
        return HostedPaymentAssessment(
            VerificationDisposition.VALIDATED_UNEXPECTED_DELIVERY,
            "DELIVERED_ASSET_MISMATCH",
            "validated Payment delivered an unexpected asset",
        )
    delivered_micro = delivered.micro_units()
    if delivered_micro != expected.amount_micro_units:
        return HostedPaymentAssessment(
            VerificationDisposition.VALIDATED_UNEXPECTED_DELIVERY,
            "DELIVERED_AMOUNT_MISMATCH",
            "validated Payment delivered an unexpected amount",
            delivered_micro,
        )
    return HostedPaymentAssessment(
        VerificationDisposition.VALIDATED_SUCCESS,
        "tesSUCCESS",
        "hosted source reports the expected validated Payment (advisory)",
        delivered_micro,
    )
