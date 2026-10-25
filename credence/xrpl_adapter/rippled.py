"""X1a: trusted transport for the pinned local standalone rippled, built and tested offline.

Every read is pinned to one validated ledger. ``validated_ledger_identity`` takes the validated
ledger index, hash, network ID, fees, reserves and complete-ledger ranges from ``server_state``,
then confirms the same hash with ``ledger``. Account, SignerList, ticket and trust-line reads ask for
that exact ``ledger_hash`` and refuse a response from any other ledger. Each response is kept
byte-for-byte as raw evidence.

Implemented seams (all JSON-RPC, ``api_version`` 2):

- ``validated_ledger_identity``: network verification and validated-ledger identity;
- ``account_snapshot`` / ``ledger_context``: account flags, RegularKey, Sequence, tickets,
  balance, owner count, TransferRate and the SignerList;
- ``trust_line``: one holder's tRLUSD trust line to the issuer;
- ``validated_ledger``: the X0t ``ValidatedLedgerObserver`` (anchor, history ranges, sender state);
- ``lookup``: the X0t ``HashLookup``, producing the X0s evidence envelope
  ``credence.xrpl.validated-payment-evidence.v1`` from the exact transaction blob and metadata.
  Each lookup first re-reads and re-verifies the node's validated-ledger identity (network, build,
  required node key). The transaction's ledger must be validated, at or below the current validated
  ledger, inside the node's complete history, and its hash must match the fetched ledger. The
  result, and the delivered amount, must agree between the binary and JSON forms (X1c, B1/B2).
  After the evidence is read, the node identity is re-read and must be unchanged, not regressed,
  and still cover the transaction's ledger (X1e, N2);
- positive lookup is performed without the proof range, so a valid inclusion is not blocked by a
  long expiry window. Only after an ordinary ``txnNotFound`` does the transport attempt the
  bounded anchor+1 through ``LastLedgerSequence`` query supported by rippled 3.4.0 (X1i, H1);
- a not-found result is permanent only when that bounded query returns explicit
  ``searched_all=true``. Both identity reads must cover the full range. An ordinary
  ``txnNotFound`` or ``searched_all=false`` is a non-final ``NOT_FOUND`` observation; a node,
  history or network contradiction is ``UNAVAILABLE`` (X1g/X1i);
- ``diagnostic_lookup``: a non-authoritative summary that cannot be used as settlement evidence;
- ``submit``: the X0t ``SubmissionTransport``. It refuses unless submission was explicitly enabled
  in its configuration. X1a never enables it, and ``offline_only`` is false, so the X0t
  ``PaymentService`` refuses this transport outright.

The client accepts only loopback endpoints, bypasses any HTTP proxy, never follows an HTTP
redirect (X1c, C3), and refuses any request that carries a secret, seed or key parameter, so the
node is never asked to sign.

Delivered amount (X1c, B1). The binary metadata is authoritative for what moved. When it carries
``DeliveredAmount`` that is the delivered amount; otherwise XRPL's rule for a successful Payment
without ``tfPartialPayment`` applies and the delivered amount is the signed ``Amount`` (the X0
inspector refuses any Payment flags, so a partial payment never reaches this point). The JSON
``delivered_amount`` must equal it in currency, issuer and exact decimal value, or the lookup is
refused ``EVIDENCE_INCONSISTENT`` and nothing settles. Both observations are kept in the evidence
envelope. An unsuccessful result must carry neither.

N1/H3 (X1e/X1i): every transport configuration requires a syntactically valid XRPL node public
key. A changed key is ``NODE_IDENTITY_MISMATCH``. On an unauthenticated loopback HTTP connection,
the self-reported key pin detects accidental source changes and inconsistent responses; it is not
cryptographic server authentication or independent ledger consensus.

Response shapes were checked read-only against the pinned rippled 3.4.0 (network 21337) in X1e:
``server_state``, ``ledger``, ``account_info``, ``account_objects``, ``account_lines`` and ``tx``
not-found responses (``spike/xrpl/x1e/``). A found ``tx`` (binary ``tx_blob``/``meta_blob`` and
JSON ``meta``) has not been observed on the real node, because no transaction may be submitted;
those paths remain verified only against the deterministic fake.

The configured ledger source remains a trust boundary. It is not independent consensus proof.

Owner: XRPL Integration (X1a, authorized by Maura).
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol, cast

from xrpl.core.addresscodec import decode_node_public_key
from xrpl.core.binarycodec import decode

from credence.xrpl_adapter.multisign import (
    AccountSnapshot,
    LedgerContext,
    MultisignPolicy,
    SignerEntry,
    SignerListSnapshot,
)
from credence.xrpl_adapter.preparation import (
    MICRO_UNITS_PER_TRLUSD,
    XrplSignedPaymentInspector,
    transaction_hash,
)
from credence.xrpl_adapter.types import (
    LedgerPayment,
    LedgerRange,
    LookupDisposition,
    LookupResult,
    LookupUnavailable,
    NonInclusionProof,
    SignedTransactionError,
    SubmissionResponse,
    SubmissionTimeout,
    SubmissionTransportError,
    TransactionType,
    ValidatedLedgerObservation,
)

EVIDENCE_SCHEMA = "credence.xrpl.validated-payment-evidence.v1"
API_VERSION = 2
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_FORBIDDEN_PARAMETERS = frozenset(
    {"secret", "seed", "seed_hex", "passphrase", "key_type", "private_key"}
)
_LEDGER_HASH = re.compile(r"^[0-9A-F]{64}$")
_TICKET_PAGE_LIMIT = 400
_MAX_TICKET_PAGES = 10


class RpcTimeout(TimeoutError):
    """The node did not answer in time; the effect of a write is unknown."""


class RpcUnavailable(ConnectionError):
    """The node could not be reached or returned an unusable response."""


class TransportRefused(RuntimeError):
    """The ledger source is not the trusted one, or its evidence is inconsistent."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class SubmissionNotAuthorized(RuntimeError):
    """Submission is disabled for this transport (X1a authorizes no live submission)."""


class JsonRpcClient(Protocol):
    def call(self, method: str, params: Mapping[str, Any]) -> bytes: ...


class LoopbackJsonRpcClient:
    """Minimal JSON-RPC client for a node on this machine only. Proxies are never used."""

    def __init__(self, url: str, *, timeout_seconds: float = 5.0) -> None:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or parts.hostname not in LOOPBACK_HOSTS:
            raise ValueError("the trusted rippled endpoint must be a loopback http(s) URL")
        if parts.username or parts.password:
            raise ValueError("the rippled endpoint must not carry credentials")
        if timeout_seconds <= 0:
            raise ValueError("timeout must be positive")
        self._url = url
        self._timeout = timeout_seconds
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _RefuseRedirects()
        )

    def call(self, method: str, params: Mapping[str, Any]) -> bytes:
        refuse_secret_parameters(params)
        body = json.dumps({"method": method, "params": [dict(params)]}).encode("utf-8")
        request = urllib.request.Request(
            self._url, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                return cast(bytes, response.read())
        except TimeoutError as exc:
            raise RpcTimeout(method) from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise RpcTimeout(method) from exc
            raise RpcUnavailable(method) from exc
        except OSError as exc:
            raise RpcUnavailable(method) from exc


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """A pinned endpoint never follows a redirect to another host or URL."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


def refuse_secret_parameters(params: Mapping[str, Any]) -> None:
    if _FORBIDDEN_PARAMETERS & set(params):
        raise ValueError("Credence never sends signing secrets to a ledger node")


@dataclass(frozen=True)
class RippledTransportConfig:
    """The trusted node's pinned identity. Every identity read checks the network ID and build
    version and node public key."""

    source_id: str
    expected_network_id: int
    expected_build_version: str
    expected_node_public_key: str
    submission_enabled: bool = False

    def __post_init__(self) -> None:
        key = self.expected_node_public_key
        if not isinstance(key, str) or not key or key != key.strip():
            raise ValueError("expected_node_public_key is required")
        try:
            decoded = decode_node_public_key(key)
        except Exception as exc:  # xrpl-py exposes codec-specific subclasses across releases
            raise ValueError(
                "expected_node_public_key is not a valid XRPL node public key"
            ) from exc
        if len(decoded) != 33:
            raise ValueError("expected_node_public_key is not a valid XRPL node public key")


@dataclass(frozen=True)
class RawEvidence:
    """One request and the exact response bytes the node returned."""

    method: str
    params: str
    response: bytes


@dataclass(frozen=True)
class ValidatedLedgerIdentity:
    source_id: str
    network_id: int
    build_version: str
    node_public_key: str | None
    ledger_index: int
    ledger_hash: str
    base_fee_drops: int
    reserve_base_drops: int
    reserve_increment_drops: int
    complete_ledgers: str
    complete_history_ranges: tuple[LedgerRange, ...]
    raw: tuple[RawEvidence, ...]


@dataclass(frozen=True)
class DiagnosticLookup:
    """Non-authoritative lookup summary (no pinned node identity). Never settlement evidence."""

    disposition: LookupDisposition
    validated: bool | None = None
    result: str | None = None
    ledger_index: int | None = None
    delivered_amount_micro_units: int | None = None
    detail: str | None = None
    authoritative: bool = False


@dataclass(frozen=True)
class TrustLineSnapshot:
    account: str
    issuer: str
    currency_code: str
    exists: bool
    ledger_index: int
    ledger_hash: str
    balance_micro_units: int = 0
    limit_micro_units: int = 0
    no_ripple: bool = False
    no_ripple_peer: bool = False


class _RpcError(RuntimeError):
    def __init__(self, error: str, result: Mapping[str, Any], evidence: RawEvidence) -> None:
        super().__init__(error)
        self.error = error
        self.result = result
        self.evidence = evidence


def parse_complete_ledgers(text: str) -> tuple[LedgerRange, ...]:
    """Parse rippled's ``complete_ledgers`` (for example ``"2-40,45-60"``). ``"empty"`` is ()."""
    if not isinstance(text, str):
        raise TransportRefused("MALFORMED_COMPLETE_LEDGERS")
    if text.strip() in ("", "empty"):
        return ()
    ranges: list[LedgerRange] = []
    for part in text.split(","):
        bounds = part.strip().split("-")
        try:
            numbers = [int(bound) for bound in bounds]
        except ValueError as exc:
            raise TransportRefused("MALFORMED_COMPLETE_LEDGERS", text) from exc
        if len(numbers) == 1:
            numbers.append(numbers[0])
        if len(numbers) != 2 or not 0 < numbers[0] <= numbers[1]:
            raise TransportRefused("MALFORMED_COMPLETE_LEDGERS", text)
        ranges.append(LedgerRange(numbers[0], numbers[1]))
    return tuple(ranges)


def issued_micro_units(value: object) -> int:
    """Exact integer micro-units of an issued-amount decimal string; signed values allowed."""
    if not isinstance(value, str):
        raise TransportRefused("MALFORMED_AMOUNT")
    try:
        scaled = Decimal(value) * MICRO_UNITS_PER_TRLUSD
    except InvalidOperation as exc:
        raise TransportRefused("MALFORMED_AMOUNT", value) from exc
    if not scaled.is_finite() or scaled != scaled.to_integral_value():
        raise TransportRefused("MALFORMED_AMOUNT", value)
    return int(scaled)


def _int(value: object, name: str) -> int:
    if isinstance(value, bool):
        raise TransportRefused("MALFORMED_RESPONSE", name)
    try:
        return int(cast(int | str, value))
    except (TypeError, ValueError) as exc:
        raise TransportRefused("MALFORMED_RESPONSE", name) from exc


def _ledger_hash(value: object) -> str:
    if not isinstance(value, str) or not _LEDGER_HASH.match(value.upper()):
        raise TransportRefused("MALFORMED_RESPONSE", "ledger hash")
    return value.upper()


def _issued(amount: object) -> tuple[str, str, Decimal]:
    if not isinstance(amount, dict):
        raise TransportRefused("EVIDENCE_INCONSISTENT", "delivered amount is not an issued amount")
    currency, issuer, value = amount.get("currency"), amount.get("issuer"), amount.get("value")
    if not isinstance(currency, str) or not isinstance(issuer, str) or not isinstance(value, str):
        raise TransportRefused("EVIDENCE_INCONSISTENT", "delivered amount is incomplete")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise TransportRefused("EVIDENCE_INCONSISTENT", "delivered value is not decimal") from exc
    if not number.is_finite():
        raise TransportRefused("EVIDENCE_INCONSISTENT", "delivered value is not finite")
    return currency.upper(), issuer, number


def _binary_delivered_amount(
    binary_meta: Mapping[str, Any], blob: bytes, result_code: object
) -> dict[str, str] | None:
    """What moved, from the binary metadata, with XRPL's Payment fallback to the signed Amount."""
    if result_code != "tesSUCCESS":
        if "DeliveredAmount" in binary_meta:
            raise TransportRefused("EVIDENCE_INCONSISTENT", "failed Payment reports a delivery")
        return None
    if "DeliveredAmount" in binary_meta:
        source, amount = "DeliveredAmount", binary_meta["DeliveredAmount"]
    else:
        # Only valid without tfPartialPayment; the X0 inspector has refused any Payment flags.
        source, amount = "Amount", decode(blob.hex().upper()).get("Amount")
    currency, issuer, value = _issued(amount)
    return {"currency": currency, "issuer": issuer, "value": str(value), "source": source}


def _require_delivered_agreement(binary: dict[str, str] | None, observed: object) -> None:
    if binary is None:
        if observed is not None:
            raise TransportRefused("EVIDENCE_INCONSISTENT", "failed Payment reports a delivery")
        return
    currency, issuer, value = _issued(observed)
    if (currency, issuer, value) != (
        binary["currency"],
        binary["issuer"],
        Decimal(binary["value"]),
    ):
        raise TransportRefused(
            "EVIDENCE_INCONSISTENT", "binary and JSON delivered amounts disagree"
        )


def _require_stable_identity(
    before: ValidatedLedgerIdentity,
    after: ValidatedLedgerIdentity,
    ledger_index: int,
    ledger_hash: str,
) -> None:
    """N2: the node did not change identity, regress, or lose the transaction's ledger."""
    _require_stable_node(before, after)
    if not _history_covers(after.complete_history_ranges, ledger_index, ledger_index):
        raise TransportRefused("LEDGER_OUTSIDE_COMPLETE_HISTORY", str(ledger_index))
    if ledger_index > after.ledger_index:
        raise TransportRefused("LEDGER_BEYOND_VALIDATED", str(ledger_index))
    if ledger_index == after.ledger_index and ledger_hash != after.ledger_hash:
        raise TransportRefused("LEDGER_HASH_MISMATCH", "differs from the re-read identity")


def _require_stable_node(before: ValidatedLedgerIdentity, after: ValidatedLedgerIdentity) -> None:
    """The same pinned node and a non-regressing validated ledger bracket one lookup."""
    if (before.network_id, before.build_version, before.node_public_key) != (
        after.network_id,
        after.build_version,
        after.node_public_key,
    ):
        raise TransportRefused("IDENTITY_CHANGED_DURING_LOOKUP")
    if after.ledger_index < before.ledger_index:
        raise TransportRefused("VALIDATED_LEDGER_REGRESSED")
    if after.ledger_index == before.ledger_index and after.ledger_hash != before.ledger_hash:
        raise TransportRefused("IDENTITY_CHANGED_DURING_LOOKUP", "validated ledger hash changed")


def _history_covers(ranges: tuple[LedgerRange, ...], first: int, last: int) -> bool:
    if not (0 < first <= last):
        return False
    covered_through = first - 1
    for ledger_range in sorted(ranges, key=lambda item: (item.first, item.last)):
        if ledger_range.last < first or ledger_range.first > last:
            continue
        if ledger_range.first > covered_through + 1:
            return False
        covered_through = max(covered_through, ledger_range.last)
        if covered_through >= last:
            return True
    return False


def _identity_evidence(identity: ValidatedLedgerIdentity) -> dict[str, object]:
    return {
        "build_version": identity.build_version,
        "complete_ledgers": identity.complete_ledgers,
        "complete_history_ranges": [
            [ledger_range.first, ledger_range.last]
            for ledger_range in identity.complete_history_ranges
        ],
        "ledger_hash": identity.ledger_hash,
        "ledger_index": identity.ledger_index,
        "network_id": identity.network_id,
        "node_public_key": identity.node_public_key,
    }


class RippledTransport:
    """Trusted local rippled seams for X1. Not accepted by the X0t offline PaymentService."""

    offline_only = False

    def __init__(self, client: JsonRpcClient, config: RippledTransportConfig) -> None:
        if not config.source_id.strip():
            raise ValueError("source_id is required")
        if not config.expected_build_version.strip():
            raise ValueError("expected_build_version is required")
        self._client = client
        self._config = config

    # --- RPC ---------------------------------------------------------------------------------

    def _call(self, method: str, params: Mapping[str, Any]) -> tuple[dict[str, Any], RawEvidence]:
        request = {**params, "api_version": API_VERSION}
        refuse_secret_parameters(request)
        raw = self._client.call(method, request)
        evidence = RawEvidence(method, json.dumps(request, sort_keys=True), raw)
        try:
            document = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RpcUnavailable(f"{method}: response is not JSON") from exc
        result = document.get("result") if isinstance(document, dict) else None
        if not isinstance(result, dict):
            raise RpcUnavailable(f"{method}: response has no result")
        if result.get("status") == "error" or "error" in result:
            raise _RpcError(str(result.get("error", "unknown")), result, evidence)
        return result, evidence

    # --- validated ledger identity -----------------------------------------------------------

    def validated_ledger_identity(self) -> ValidatedLedgerIdentity:
        state_result, state_raw = self._call("server_state", {})
        state = state_result.get("state")
        if not isinstance(state, dict):
            raise TransportRefused("MALFORMED_RESPONSE", "server_state")
        network_id = state.get("network_id")
        if network_id != self._config.expected_network_id:
            raise TransportRefused("NETWORK_MISMATCH", f"network_id is {network_id!r}")
        build_version = str(state.get("build_version", ""))
        if build_version != self._config.expected_build_version:
            raise TransportRefused("BUILD_VERSION_MISMATCH", build_version)
        node_public_key = state.get("pubkey_node")
        expected_node = self._config.expected_node_public_key
        if expected_node is not None and node_public_key != expected_node:
            raise TransportRefused("NODE_IDENTITY_MISMATCH", str(node_public_key))
        validated = state.get("validated_ledger")
        if not isinstance(validated, dict):
            raise TransportRefused("NO_VALIDATED_LEDGER")
        ledger_index = _int(validated.get("seq"), "validated_ledger.seq")
        ledger_hash = _ledger_hash(validated.get("hash"))
        complete = state.get("complete_ledgers", "")
        ranges = parse_complete_ledgers(cast(str, complete))

        ledger_result, ledger_raw = self._call(
            "ledger", {"ledger_index": ledger_index, "transactions": False, "expand": False}
        )
        if ledger_result.get("validated") is not True:
            raise TransportRefused("LEDGER_NOT_VALIDATED")
        header = ledger_result.get("ledger")
        confirmed = ledger_result.get("ledger_hash")
        if confirmed is None and isinstance(header, dict):
            confirmed = header.get("ledger_hash")
        if _ledger_hash(confirmed) != ledger_hash:
            raise TransportRefused("LEDGER_IDENTITY_MISMATCH")
        if _int(ledger_result.get("ledger_index"), "ledger_index") != ledger_index:
            raise TransportRefused("LEDGER_IDENTITY_MISMATCH")
        return ValidatedLedgerIdentity(
            source_id=self._config.source_id,
            network_id=cast(int, network_id),
            build_version=build_version,
            node_public_key=None if node_public_key is None else str(node_public_key),
            ledger_index=ledger_index,
            ledger_hash=ledger_hash,
            base_fee_drops=_int(validated.get("base_fee"), "base_fee"),
            reserve_base_drops=_int(validated.get("reserve_base"), "reserve_base"),
            reserve_increment_drops=_int(validated.get("reserve_inc"), "reserve_inc"),
            complete_ledgers=cast(str, complete),
            complete_history_ranges=ranges,
            raw=(state_raw, ledger_raw),
        )

    def _pinned(self, result: Mapping[str, Any], identity: ValidatedLedgerIdentity) -> None:
        if result.get("validated") is not True:
            raise TransportRefused("LEDGER_NOT_VALIDATED")
        if _ledger_hash(result.get("ledger_hash")) != identity.ledger_hash:
            raise TransportRefused("LEDGER_STATE_INCONSISTENT", "response is from another ledger")
        if _int(result.get("ledger_index"), "ledger_index") != identity.ledger_index:
            raise TransportRefused("LEDGER_STATE_INCONSISTENT", "response is from another ledger")

    # --- account state -----------------------------------------------------------------------

    def account_snapshot(self, account: str, identity: ValidatedLedgerIdentity) -> AccountSnapshot:
        try:
            result, _ = self._call(
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
            raise TransportRefused("ACCOUNT_INFO_FAILED", exc.error) from exc
        self._pinned(result, identity)
        data = result.get("account_data")
        if not isinstance(data, dict) or data.get("Account") != account:
            raise TransportRefused("MALFORMED_RESPONSE", "account_data")
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
            tickets=self._tickets(account, identity),
            signer_list=self._signer_list(account, result, data),
        )

    @staticmethod
    def _signer_list(
        account: str, result: Mapping[str, Any], data: Mapping[str, Any]
    ) -> SignerListSnapshot | None:
        lists = result.get("signer_lists", data.get("signer_lists", []))
        if not isinstance(lists, list):
            raise TransportRefused("MALFORMED_RESPONSE", "signer_lists")
        if not lists:
            return None
        if len(lists) != 1 or not isinstance(lists[0], dict):
            raise TransportRefused("MALFORMED_RESPONSE", "more than one SignerList")
        signer_list = lists[0]
        entries: list[SignerEntry] = []
        for wrapper in signer_list.get("SignerEntries", []):
            entry = wrapper.get("SignerEntry") if isinstance(wrapper, dict) else None
            if not isinstance(entry, dict):
                raise TransportRefused("MALFORMED_RESPONSE", "SignerEntry")
            entries.append(
                SignerEntry(
                    account=str(entry.get("Account")),
                    weight=_int(entry.get("SignerWeight"), "SignerWeight"),
                )
            )
        return SignerListSnapshot(
            account=account,
            quorum=_int(signer_list.get("SignerQuorum"), "SignerQuorum"),
            entries=tuple(entries),
        )

    def _tickets(self, account: str, identity: ValidatedLedgerIdentity) -> tuple[int, ...]:
        tickets: list[int] = []
        marker: object = None
        for _ in range(_MAX_TICKET_PAGES):
            params: dict[str, Any] = {
                "account": account,
                "ledger_hash": identity.ledger_hash,
                "type": "ticket",
                "limit": _TICKET_PAGE_LIMIT,
            }
            if marker is not None:
                params["marker"] = marker
            try:
                result, _ = self._call("account_objects", params)
            except _RpcError as exc:
                raise TransportRefused("ACCOUNT_OBJECTS_FAILED", exc.error) from exc
            self._pinned(result, identity)
            for item in result.get("account_objects", []):
                if not isinstance(item, dict) or item.get("LedgerEntryType") != "Ticket":
                    raise TransportRefused("MALFORMED_RESPONSE", "ticket object")
                tickets.append(_int(item.get("TicketSequence"), "TicketSequence"))
            marker = result.get("marker")
            if marker is None:
                return tuple(sorted(tickets))
        raise TransportRefused("TOO_MANY_TICKETS")

    def _authoritative_identity(self) -> ValidatedLedgerIdentity:
        """Read and verify the required node-identity pin for an authoritative operation."""
        return self.validated_ledger_identity()

    def ledger_context(self, policy: MultisignPolicy) -> LedgerContext:
        """Agent account and both signer accounts, all from one validated ledger."""
        identity = self._authoritative_identity()
        if identity.network_id != policy.network_id:
            raise TransportRefused("NETWORK_MISMATCH")
        return LedgerContext(
            validated_ledger_index=identity.ledger_index,
            validated_ledger_hash=identity.ledger_hash,
            base_fee_drops=identity.base_fee_drops,
            network_id=identity.network_id,
            account=self.account_snapshot(policy.account, identity),
            signers={
                signer: self.account_snapshot(signer, identity)
                for signer in sorted(policy.signer_accounts)
            },
        )

    def trust_line(
        self, account: str, issuer: str, currency_code: str, identity: ValidatedLedgerIdentity
    ) -> TrustLineSnapshot:
        try:
            result, _ = self._call(
                "account_lines",
                {"account": account, "peer": issuer, "ledger_hash": identity.ledger_hash},
            )
        except _RpcError as exc:
            raise TransportRefused("ACCOUNT_LINES_FAILED", exc.error) from exc
        self._pinned(result, identity)
        if result.get("marker") is not None:
            raise TransportRefused("MALFORMED_RESPONSE", "paged trust lines for one peer")
        matches = [
            line
            for line in result.get("lines", [])
            if isinstance(line, dict)
            and line.get("account") == issuer
            and line.get("currency") == currency_code
        ]
        if len(matches) > 1:
            raise TransportRefused("MALFORMED_RESPONSE", "duplicate trust line")
        if not matches:
            return TrustLineSnapshot(
                account, issuer, currency_code, False, identity.ledger_index, identity.ledger_hash
            )
        line = matches[0]
        return TrustLineSnapshot(
            account=account,
            issuer=issuer,
            currency_code=currency_code,
            exists=True,
            ledger_index=identity.ledger_index,
            ledger_hash=identity.ledger_hash,
            balance_micro_units=issued_micro_units(line.get("balance")),
            limit_micro_units=issued_micro_units(line.get("limit")),
            no_ripple=line.get("no_ripple") is True,
            no_ripple_peer=line.get("no_ripple_peer") is True,
        )

    # --- X0t ValidatedLedgerObserver ---------------------------------------------------------

    def validated_ledger(self, account: str) -> ValidatedLedgerObservation:
        try:
            identity = self._authoritative_identity()
            snapshot = self.account_snapshot(account, identity)
        except (RpcTimeout, RpcUnavailable, TransportRefused, _RpcError) as exc:
            raise LookupUnavailable(f"validated ledger unavailable: {exc}") from exc
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
            available_tickets=snapshot.tickets,
            network_id=identity.network_id,
            build_version=identity.build_version,
            node_public_key=identity.node_public_key,
        )

    # --- X0t HashLookup ----------------------------------------------------------------------

    def lookup(
        self,
        transaction_hash_value: str,
        *,
        min_ledger: int | None = None,
        max_ledger: int | None = None,
    ) -> LookupResult:
        """Authoritative lookup. Permanent non-inclusion requires a bounded full search."""
        try:
            return self._lookup(
                transaction_hash_value.upper(),
                authoritative=True,
                min_ledger=min_ledger,
                max_ledger=max_ledger,
            )
        except _RpcError as exc:
            return LookupResult(LookupDisposition.UNAVAILABLE, detail=f"RPC_ERROR:{exc.error}")
        except (RpcTimeout, RpcUnavailable) as exc:
            return LookupResult(LookupDisposition.UNAVAILABLE, detail=f"RPC_UNAVAILABLE:{exc}")
        except TransportRefused as exc:
            return LookupResult(LookupDisposition.UNAVAILABLE, detail=exc.code)

    def diagnostic_lookup(self, transaction_hash_value: str) -> DiagnosticLookup:
        """Non-authoritative summary from the required pinned transport.

        It returns only a summary, never a ``LookupResult``, ``LedgerPayment`` or evidence
        envelope, so its output cannot be handed to the X0t journal as settlement evidence."""
        try:
            result = self._lookup(
                transaction_hash_value.upper(),
                authoritative=False,
                min_ledger=None,
                max_ledger=None,
            )
        except _RpcError as exc:
            if exc.error == "txnNotFound":
                return DiagnosticLookup(LookupDisposition.NOT_FOUND)
            return DiagnosticLookup(LookupDisposition.UNAVAILABLE, detail=f"RPC_ERROR:{exc.error}")
        except (RpcTimeout, RpcUnavailable) as exc:
            return DiagnosticLookup(LookupDisposition.UNAVAILABLE, detail=f"RPC_UNAVAILABLE:{exc}")
        except TransportRefused as exc:
            return DiagnosticLookup(LookupDisposition.UNAVAILABLE, detail=exc.code)
        payment = result.payment
        return DiagnosticLookup(
            disposition=result.disposition,
            validated=None if payment is None else payment.validated,
            result=None if payment is None else payment.result,
            ledger_index=None if payment is None else payment.ledger_index,
            delivered_amount_micro_units=(
                None if payment is None else payment.delivered_amount_micro_units
            ),
        )

    def _lookup(
        self,
        expected_hash: str,
        *,
        authoritative: bool,
        min_ledger: int | None,
        max_ledger: int | None,
    ) -> LookupResult:
        # B2: the node's identity is re-read and re-verified for every lookup.
        identity = (
            self._authoritative_identity() if authoritative else self.validated_ledger_identity()
        )
        if (min_ledger is None) != (max_ledger is None):
            raise TransportRefused("MALFORMED_LOOKUP_RANGE")
        if min_ledger is not None and max_ledger is not None:
            if not 0 < min_ledger <= max_ledger:
                raise TransportRefused("MALFORMED_LOOKUP_RANGE")
        # A positive hash lookup is not constrained by the non-inclusion proof window. rippled
        # returns a found transaction even when it lies outside a requested range, but doing the
        # ordinary lookup first also avoids rejecting a valid inclusion merely because the
        # requested non-inclusion window exceeds rippled's bounded-search limit.
        tx_params: dict[str, Any] = {"transaction": expected_hash, "binary": True}
        try:
            binary, binary_raw = self._call("tx", tx_params)
        except _RpcError as exc:
            if exc.error != "txnNotFound":
                raise
            first_not_found = exc.evidence
            bounded_not_found: _RpcError | None = None
            # rippled 3.4.0 accepts max_ledger - min_ledger <= 1,000. Because its interval is
            # closed, that implementation permits at most 1,001 ledger indexes.
            # Longer windows can still find a transaction through the ordinary lookup, but they
            # cannot produce permanent non-inclusion evidence through this mechanism.
            bounded_supported = bool(
                authoritative
                and min_ledger is not None
                and max_ledger is not None
                and max_ledger - min_ledger <= 1_000
            )
            if bounded_supported:
                tx_params = {
                    "transaction": expected_hash,
                    "binary": True,
                    "min_ledger": min_ledger,
                    "max_ledger": max_ledger,
                }
                try:
                    binary, binary_raw = self._call("tx", tx_params)
                except _RpcError as bounded_error:
                    if bounded_error.error != "txnNotFound":
                        raise
                    bounded_not_found = bounded_error

            if bounded_not_found is None and bounded_supported:
                # The transaction appeared between the ordinary and bounded reads. Continue down
                # the positive-inclusion path with the returned bytes.
                pass
            else:
                after = (
                    self._authoritative_identity()
                    if authoritative
                    else self.validated_ledger_identity()
                )
                _require_stable_node(identity, after)
                if identity.complete_history_ranges != after.complete_history_ranges:
                    raise TransportRefused("HISTORY_CHANGED_DURING_LOOKUP") from exc
                if not authoritative:
                    return LookupResult(LookupDisposition.NOT_FOUND)
                if bounded_not_found is None:
                    return LookupResult(
                        LookupDisposition.NOT_FOUND,
                        detail="BOUNDED_NON_INCLUSION_PROOF_UNAVAILABLE",
                    )
                if bounded_not_found.result.get("searched_all") is not True:
                    return LookupResult(
                        LookupDisposition.NOT_FOUND,
                        detail="TRANSACTION_RANGE_NOT_FULLY_SEARCHED",
                    )
                assert min_ledger is not None and max_ledger is not None
                before_covers = identity.ledger_index >= max_ledger and _history_covers(
                    identity.complete_history_ranges, min_ledger, max_ledger
                )
                after_covers = after.ledger_index >= max_ledger and _history_covers(
                    after.complete_history_ranges, min_ledger, max_ledger
                )
                if not before_covers or not after_covers:
                    raise TransportRefused("LEDGER_OUTSIDE_COMPLETE_HISTORY") from exc
                evidence = json.dumps(
                    {
                        "schema": "credence.xrpl.non-inclusion-evidence.v1",
                        "transaction_hash": expected_hash,
                        "searched_all": True,
                        "min_ledger": min_ledger,
                        "max_ledger": max_ledger,
                        "source_id": self._config.source_id,
                        "validated_identity": _identity_evidence(identity),
                        "validated_identity_after": _identity_evidence(after),
                        "raw": [
                            {
                                "method": item.method,
                                "params": item.params,
                                "response": item.response.decode("utf-8", errors="replace"),
                            }
                            for item in (
                                *identity.raw,
                                first_not_found,
                                bounded_not_found.evidence,
                                *after.raw,
                            )
                        ],
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
                return LookupResult(
                    LookupDisposition.NOT_FOUND,
                    non_inclusion_proof=NonInclusionProof(
                        transaction_hash=expected_hash,
                        first_ledger_sequence=min_ledger,
                        last_ledger_sequence=max_ledger,
                        searched_all=True,
                        source_id=after.source_id,
                        network_id=after.network_id,
                        build_version=after.build_version,
                        node_public_key=after.node_public_key or "",
                        validated_ledger_index=after.ledger_index,
                        validated_ledger_hash=after.ledger_hash,
                        complete_history_ranges=after.complete_history_ranges,
                        evidence=evidence,
                    ),
                )
        blob_hex = binary.get("tx_blob")
        if not isinstance(blob_hex, str) or not blob_hex:
            raise TransportRefused("MALFORMED_RESPONSE", "tx_blob")
        try:
            blob = bytes.fromhex(blob_hex)
        except ValueError as exc:
            raise TransportRefused("MALFORMED_RESPONSE", "tx_blob") from exc
        if transaction_hash(blob) != expected_hash or str(binary.get("hash", "")).upper() != (
            expected_hash
        ):
            raise TransportRefused("EVIDENCE_HASH_MISMATCH")
        try:
            parsed = XrplSignedPaymentInspector().inspect_payment(blob)
        except SignedTransactionError as exc:
            raise TransportRefused("EVIDENCE_NOT_A_PAYMENT", str(exc)) from exc

        if binary.get("validated") is not True:
            return LookupResult(
                LookupDisposition.FOUND,
                LedgerPayment(
                    transaction_hash=expected_hash,
                    validated=False,
                    result=None,
                    transaction_type=TransactionType.PAYMENT,
                    account=parsed.account,
                    destination=parsed.destination,
                    asset=parsed.asset,
                    amount_micro_units=parsed.amount_micro_units,
                    delivered_amount_micro_units=None,
                    fee_drops=parsed.fee_drops,
                    network_id=parsed.network_id,
                    last_ledger_sequence=parsed.last_ledger_sequence,
                    sequence=parsed.sequence,
                    ticket_sequence=parsed.ticket_sequence,
                    ledger_index=None,
                    ledger_hash=None,
                    evidence=b"",
                ),
            )

        ledger_index = _int(binary.get("ledger_index"), "ledger_index")
        if ledger_index > identity.ledger_index:
            raise TransportRefused("LEDGER_BEYOND_VALIDATED", str(ledger_index))
        if not any(
            item.first <= ledger_index <= item.last for item in identity.complete_history_ranges
        ):
            raise TransportRefused("LEDGER_OUTSIDE_COMPLETE_HISTORY", str(ledger_index))
        meta_hex = binary.get("meta_blob", binary.get("meta"))
        if not isinstance(meta_hex, str):
            raise TransportRefused("MALFORMED_RESPONSE", "meta_blob")
        try:
            binary_meta = decode(meta_hex)
        except Exception as exc:  # xrpl-py raises several codec error types
            raise TransportRefused("MALFORMED_RESPONSE", "meta_blob") from exc

        detail_params = {**tx_params, "binary": False}
        detail, detail_raw = self._call("tx", detail_params)
        meta = detail.get("meta")
        if (
            detail.get("validated") is not True
            or str(detail.get("hash", "")).upper() != expected_hash
            or _int(detail.get("ledger_index"), "ledger_index") != ledger_index
            or not isinstance(meta, dict)
            or meta.get("TransactionResult") != binary_meta.get("TransactionResult")
        ):
            raise TransportRefused("EVIDENCE_INCONSISTENT", "binary and JSON lookups disagree")

        ledger_result, ledger_raw = self._call(
            "ledger", {"ledger_index": ledger_index, "transactions": False, "expand": False}
        )
        if ledger_result.get("validated") is not True:
            raise TransportRefused("LEDGER_NOT_VALIDATED")
        if _int(ledger_result.get("ledger_index"), "ledger_index") != ledger_index:
            raise TransportRefused("LEDGER_HASH_MISMATCH", "ledger index differs")
        header = ledger_result.get("ledger")
        ledger_hash_value = ledger_result.get("ledger_hash")
        if ledger_hash_value is None and isinstance(header, dict):
            ledger_hash_value = header.get("ledger_hash")
        ledger_hash = _ledger_hash(ledger_hash_value)
        if ledger_index == identity.ledger_index and ledger_hash != identity.ledger_hash:
            raise TransportRefused("LEDGER_HASH_MISMATCH", "differs from the validated identity")
        for stated in (binary.get("ledger_hash"), detail.get("ledger_hash")):
            if stated is not None and _ledger_hash(stated) != ledger_hash:
                raise TransportRefused("LEDGER_HASH_MISMATCH")

        # N2 (X1e): re-read the node's identity after the evidence and require it unchanged.
        after = (
            self._authoritative_identity() if authoritative else self.validated_ledger_identity()
        )
        _require_stable_identity(identity, after, ledger_index, ledger_hash)

        result_code = meta.get("TransactionResult")
        delivered = meta.get("delivered_amount")
        binary_delivered = _binary_delivered_amount(binary_meta, blob, result_code)
        _require_delivered_agreement(binary_delivered, delivered)
        delivered_micro: int | None = None
        if isinstance(delivered, dict):
            delivered_micro = issued_micro_units(delivered.get("value"))
        envelope = json.dumps(
            {
                "schema": EVIDENCE_SCHEMA,
                "validated": True,
                "ledger_index": ledger_index,
                "ledger_hash": ledger_hash,
                "transaction_hash": expected_hash,
                "signed_transaction": blob.hex().upper(),
                "meta": {
                    "TransactionResult": result_code,
                    "delivered_amount": delivered,
                },
                "delivered_amount_observations": {
                    "binary": binary_delivered,
                    "json": delivered,
                },
                "validated_identity": {
                    "build_version": identity.build_version,
                    "complete_ledgers": identity.complete_ledgers,
                    "ledger_hash": identity.ledger_hash,
                    "ledger_index": identity.ledger_index,
                    "network_id": identity.network_id,
                    "node_public_key": identity.node_public_key,
                },
                "validated_identity_after": {
                    "build_version": after.build_version,
                    "complete_ledgers": after.complete_ledgers,
                    "ledger_hash": after.ledger_hash,
                    "ledger_index": after.ledger_index,
                    "network_id": after.network_id,
                    "node_public_key": after.node_public_key,
                },
                "source_id": self._config.source_id,
                "raw": [
                    {
                        "method": item.method,
                        "params": item.params,
                        "response": item.response.decode("utf-8", errors="replace"),
                    }
                    for item in (*identity.raw, binary_raw, detail_raw, ledger_raw, *after.raw)
                ],
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return LookupResult(
            LookupDisposition.FOUND,
            LedgerPayment(
                transaction_hash=expected_hash,
                validated=True,
                result=cast(str | None, result_code),
                transaction_type=TransactionType.PAYMENT,
                account=parsed.account,
                destination=parsed.destination,
                asset=parsed.asset,
                amount_micro_units=parsed.amount_micro_units,
                delivered_amount_micro_units=delivered_micro,
                fee_drops=parsed.fee_drops,
                network_id=parsed.network_id,
                last_ledger_sequence=parsed.last_ledger_sequence,
                sequence=parsed.sequence,
                ticket_sequence=parsed.ticket_sequence,
                ledger_index=ledger_index,
                ledger_hash=ledger_hash,
                evidence=envelope,
            ),
        )

    # --- X0t SubmissionTransport -------------------------------------------------------------

    def submit(self, signed_transaction: bytes, transaction_hash_value: str) -> SubmissionResponse:
        if not self._config.submission_enabled:
            raise SubmissionNotAuthorized("live submission is not authorized for this transport")
        if transaction_hash(signed_transaction) != transaction_hash_value:
            raise SubmissionTransportError("hash does not match the signed bytes")
        try:
            result, _ = self._call("submit", {"tx_blob": signed_transaction.hex().upper()})
        except RpcTimeout as exc:
            raise SubmissionTimeout(str(exc)) from exc
        except (RpcUnavailable, _RpcError) as exc:
            raise SubmissionTransportError(str(exc)) from exc
        tx_json = result.get("tx_json")
        submitted_hash = tx_json.get("hash") if isinstance(tx_json, dict) else None
        if submitted_hash is not None and str(submitted_hash).upper() != transaction_hash_value:
            raise SubmissionTransportError("node reports a different transaction hash")
        return SubmissionResponse(
            accepted=result.get("accepted") is True,
            engine_result=str(result.get("engine_result", "")),
            message=str(result.get("engine_result_message", "")),
        )
