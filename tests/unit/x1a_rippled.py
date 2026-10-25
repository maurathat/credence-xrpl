"""Deterministic fake rippled for X1a transport tests. No network access.

It answers JSON-RPC with rippled API v2 response shapes from a tiny in-memory ledger and lets a
test inject the faults the transport must survive. Shapes follow rippled's documented API; they
have not been checked against the pinned server (a listed X1 dependency).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from x1a_world import (
    BASE_FEE,
    LEDGER_HASH,
    NETWORK_ID,
    OWNER,
    POLICY,
    RESEARCHBOT,
    SEQUENCE,
    TICKET,
    VALIDATED_LEDGER,
)
from xrpl.core.binarycodec import encode

from credence.xrpl_adapter.preparation import transaction_hash

NODE_KEY = "n9TestLocalStandaloneNode"  # the fake node's public identity; tests pin it


def ledger_hash_for(index: int) -> str:
    return LEDGER_HASH if index == VALIDATED_LEDGER else f"{index:064X}"


@dataclass
class StoredTransaction:
    blob: bytes
    validated: bool
    ledger_index: int | None
    result: str = "tesSUCCESS"
    delivered: Mapping[str, str] | None = None


@dataclass
class FakeRippled:
    network_id: int = NETWORK_ID
    build_version: str = "3.4.0"
    node_public_key: str = NODE_KEY
    ledger_index: int = VALIDATED_LEDGER
    complete_ledgers: str = f"1-{VALIDATED_LEDGER}"
    base_fee: int = BASE_FEE
    reserve_base: int = 1_000_000
    reserve_inc: int = 200_000
    accounts: dict[str, dict[str, Any]] = field(default_factory=dict)
    signer_lists: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    tickets: dict[str, list[int]] = field(default_factory=dict)
    lines: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    transactions: dict[str, StoredTransaction] = field(default_factory=dict)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    faults: dict[str, Callable[[dict[str, Any]], dict[str, Any] | None]] = field(
        default_factory=dict
    )
    submit_results: list[dict[str, Any] | Exception] = field(default_factory=list)
    ticket_page_size: int = 400

    @property
    def ledger_hash(self) -> str:
        return ledger_hash_for(self.ledger_index)

    def advance(self, index: int, complete: str | None = None) -> None:
        self.ledger_index = index
        self.complete_ledgers = complete if complete is not None else f"1-{index}"

    def include(
        self,
        blob: bytes,
        *,
        ledger_index: int | None,
        validated: bool = True,
        result: str = "tesSUCCESS",
        delivered: Mapping[str, str] | None = None,
    ) -> str:
        tx_hash = transaction_hash(blob)
        self.transactions[tx_hash] = StoredTransaction(
            blob, validated, ledger_index, result, delivered
        )
        return tx_hash

    # --- JsonRpcClient -----------------------------------------------------------------------

    def call(self, method: str, params: Mapping[str, Any]) -> bytes:
        request = dict(params)
        self.calls.append((method, request))
        fault = self.faults.get(method)
        if fault is not None:
            override = fault(request)
            if override is not None:
                return json.dumps({"result": override}).encode()
        handler = getattr(self, f"_{method}")
        return json.dumps({"result": handler(request)}).encode()

    def _error(self, error: str) -> dict[str, Any]:
        return {"status": "error", "error": error}

    def _pinned(self, request: Mapping[str, Any]) -> dict[str, Any]:
        assert request["ledger_hash"] == self.ledger_hash, "reads must be pinned by ledger hash"
        return {
            "ledger_hash": self.ledger_hash,
            "ledger_index": self.ledger_index,
            "validated": True,
            "status": "success",
        }

    def _server_state(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "status": "success",
            "state": {
                "build_version": self.build_version,
                "complete_ledgers": self.complete_ledgers,
                "network_id": self.network_id,
                "pubkey_node": self.node_public_key,
                "server_state": "full",
                "validated_ledger": {
                    "base_fee": self.base_fee,
                    "close_time": 1,
                    "hash": self.ledger_hash,
                    "reserve_base": self.reserve_base,
                    "reserve_inc": self.reserve_inc,
                    "seq": self.ledger_index,
                },
            },
        }

    def _ledger(self, request: Mapping[str, Any]) -> dict[str, Any]:
        index = int(request["ledger_index"])
        return {
            "ledger": {
                "ledger_hash": ledger_hash_for(index),
                "ledger_index": str(index),
                "closed": True,
            },
            "ledger_hash": ledger_hash_for(index),
            "ledger_index": index,
            "validated": index <= self.ledger_index,
            "status": "success",
        }

    def _account_info(self, request: Mapping[str, Any]) -> dict[str, Any]:
        account = request["account"]
        if account not in self.accounts:
            return self._error("actNotFound")
        result = self._pinned(request)
        result["account_data"] = {"Account": account, **self.accounts[account]}
        if request.get("signer_lists"):
            result["signer_lists"] = self.signer_lists.get(account, [])
        return result

    def _account_objects(self, request: Mapping[str, Any]) -> dict[str, Any]:
        result = self._pinned(request)
        tickets = self.tickets.get(request["account"], [])
        start = int(request.get("marker", 0))
        page = tickets[start : start + self.ticket_page_size]
        result["account_objects"] = [
            {"LedgerEntryType": "Ticket", "TicketSequence": ticket} for ticket in page
        ]
        if start + self.ticket_page_size < len(tickets):
            result["marker"] = str(start + self.ticket_page_size)
        return result

    def _account_lines(self, request: Mapping[str, Any]) -> dict[str, Any]:
        result = self._pinned(request)
        result["lines"] = [
            line
            for line in self.lines.get(request["account"], [])
            if line["account"] == request["peer"]
        ]
        return result

    def _tx(self, request: Mapping[str, Any]) -> dict[str, Any]:
        stored = self.transactions.get(request["transaction"])
        if stored is None:
            return self._error("txnNotFound")
        tx_hash = transaction_hash(stored.blob)
        result: dict[str, Any] = {
            "hash": tx_hash,
            "validated": stored.validated,
            "status": "success",
        }
        if stored.ledger_index is not None:
            result["ledger_index"] = stored.ledger_index
            if stored.validated:
                result["ledger_hash"] = ledger_hash_for(stored.ledger_index)
        if not stored.validated:
            if request["binary"]:
                result["tx_blob"] = stored.blob.hex().upper()
            return result
        meta: dict[str, Any] = {
            "TransactionIndex": 0,
            "TransactionResult": stored.result,
            "AffectedNodes": [],
        }
        if stored.delivered is not None:
            meta["DeliveredAmount"] = dict(stored.delivered)
        if request["binary"]:
            result["tx_blob"] = stored.blob.hex().upper()
            result["meta_blob"] = encode(meta)
        else:
            json_meta = {k: v for k, v in meta.items() if k != "DeliveredAmount"}
            # rippled adds the synthetic delivered_amount only to successful Payments.
            if stored.result == "tesSUCCESS":
                json_meta["delivered_amount"] = (
                    dict(stored.delivered) if stored.delivered is not None else "unavailable"
                )
            result["meta"] = json_meta
            result["tx_json"] = {"hash": tx_hash}
        return result

    def _submit(self, request: Mapping[str, Any]) -> dict[str, Any]:
        outcome = self.submit_results.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def standard_ledger(**changes: Any) -> FakeRippled:
    """Agent account with the expected SignerList and an unfunded pair of signers."""
    rippled = FakeRippled(**changes)
    rippled.accounts[OWNER.account] = {
        "Balance": "50000000",
        "Flags": 0,
        "OwnerCount": 2,
        "Sequence": SEQUENCE,
    }
    rippled.signer_lists[OWNER.account] = [
        {
            "LedgerEntryType": "SignerList",
            "SignerQuorum": 2,
            "SignerEntries": [
                {"SignerEntry": {"Account": RESEARCHBOT.account, "SignerWeight": 1}},
                {"SignerEntry": {"Account": POLICY.account, "SignerWeight": 1}},
            ],
        }
    ]
    rippled.tickets[OWNER.account] = [TICKET]
    return rippled
