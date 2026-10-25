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
  ``credence.xrpl.validated-payment-evidence.v1`` from the exact transaction blob and metadata,
  cross-checked across the binary and JSON forms and against the validated ledger hash;
- ``submit``: the X0t ``SubmissionTransport``. It refuses unless submission was explicitly enabled
  in its configuration. X1a never enables it, and ``offline_only`` is false, so the X0t
  ``PaymentService`` refuses this transport outright.

The client accepts only loopback endpoints, bypasses any HTTP proxy, and refuses any request that
carries a secret, seed or key parameter, so the node is never asked to sign.

The response shapes are rippled's documented API v2 shapes. They were exercised only against the
deterministic fake client in ``tests/unit/test_xrpl_x1a_transport.py``, not against the pinned
server. Read-only verification against the pinned rippled is a listed X1 dependency.

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
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

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


def refuse_secret_parameters(params: Mapping[str, Any]) -> None:
    if _FORBIDDEN_PARAMETERS & set(params):
        raise ValueError("Credence never sends signing secrets to a ledger node")


@dataclass(frozen=True)
class RippledTransportConfig:
    source_id: str
    expected_network_id: int
    expected_build_version: str | None = None
    submission_enabled: bool = False


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
    ledger_index: int
    ledger_hash: str
    base_fee_drops: int
    reserve_base_drops: int
    reserve_increment_drops: int
    complete_ledgers: str
    complete_history_ranges: tuple[LedgerRange, ...]
    raw: tuple[RawEvidence, ...]


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
    def __init__(self, error: str) -> None:
        super().__init__(error)
        self.error = error


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


class RippledTransport:
    """Trusted local rippled seams for X1. Not accepted by the X0t offline PaymentService."""

    offline_only = False

    def __init__(self, client: JsonRpcClient, config: RippledTransportConfig) -> None:
        if not config.source_id.strip():
            raise ValueError("source_id is required")
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
            raise _RpcError(str(result.get("error", "unknown")))
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
        expected_build = self._config.expected_build_version
        if expected_build is not None and build_version != expected_build:
            raise TransportRefused("BUILD_VERSION_MISMATCH", build_version)
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

    def ledger_context(self, policy: MultisignPolicy) -> LedgerContext:
        """Agent account and both signer accounts, all from one validated ledger."""
        identity = self.validated_ledger_identity()
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
            identity = self.validated_ledger_identity()
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
        )

    # --- X0t HashLookup ----------------------------------------------------------------------

    def lookup(self, transaction_hash_value: str) -> LookupResult:
        try:
            return self._lookup(transaction_hash_value.upper())
        except _RpcError as exc:
            if exc.error == "txnNotFound":
                return LookupResult(LookupDisposition.NOT_FOUND)
            return LookupResult(LookupDisposition.UNAVAILABLE, detail=f"RPC_ERROR:{exc.error}")
        except (RpcTimeout, RpcUnavailable) as exc:
            return LookupResult(LookupDisposition.UNAVAILABLE, detail=f"RPC_UNAVAILABLE:{exc}")
        except TransportRefused as exc:
            return LookupResult(LookupDisposition.UNAVAILABLE, detail=exc.code)

    def _lookup(self, expected_hash: str) -> LookupResult:
        binary, binary_raw = self._call("tx", {"transaction": expected_hash, "binary": True})
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
        meta_hex = binary.get("meta_blob", binary.get("meta"))
        if not isinstance(meta_hex, str):
            raise TransportRefused("MALFORMED_RESPONSE", "meta_blob")
        try:
            binary_meta = decode(meta_hex)
        except Exception as exc:  # xrpl-py raises several codec error types
            raise TransportRefused("MALFORMED_RESPONSE", "meta_blob") from exc

        detail, detail_raw = self._call("tx", {"transaction": expected_hash, "binary": False})
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
        header = ledger_result.get("ledger")
        ledger_hash_value = ledger_result.get("ledger_hash")
        if ledger_hash_value is None and isinstance(header, dict):
            ledger_hash_value = header.get("ledger_hash")
        ledger_hash = _ledger_hash(ledger_hash_value)
        for stated in (binary.get("ledger_hash"), detail.get("ledger_hash")):
            if stated is not None and _ledger_hash(stated) != ledger_hash:
                raise TransportRefused("LEDGER_HASH_MISMATCH")

        delivered = meta.get("delivered_amount")
        delivered_micro: int | None = None
        if isinstance(delivered, dict):
            try:
                delivered_micro = issued_micro_units(delivered.get("value"))
            except TransportRefused:
                delivered_micro = None
        envelope = json.dumps(
            {
                "schema": EVIDENCE_SCHEMA,
                "validated": True,
                "ledger_index": ledger_index,
                "ledger_hash": ledger_hash,
                "transaction_hash": expected_hash,
                "signed_transaction": blob.hex().upper(),
                "meta": {
                    "TransactionResult": meta.get("TransactionResult"),
                    "delivered_amount": delivered,
                },
                "source_id": self._config.source_id,
                "raw": [
                    {
                        "method": item.method,
                        "params": item.params,
                        "response": item.response.decode("utf-8", errors="replace"),
                    }
                    for item in (binary_raw, detail_raw, ledger_raw)
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
                result=cast(str | None, meta.get("TransactionResult")),
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
