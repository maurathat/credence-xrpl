"""Deterministic fake QuickNode (Clio) XRPL Testnet endpoint for X1q tests. No network access.

Shapes follow QuickNode's documented Clio responses (``server_info`` with ``clio_version`` and
warning 2001; ``server_state`` forwarded to a rippled node) and rippled's API v2 ``tx``/``ledger``
shapes. They have not been checked against a real QuickNode endpoint (real verification PENDING).

Payments are Testnet-shaped: no ``NetworkID`` field (network 1 forbids it). Accounts and keys are
derived at runtime from public labels, as in ``x1a_world``; nothing here is real key material.
The endpoint token is derived at runtime and is not a real credential.
"""

from __future__ import annotations

import hashlib
import json
import socket
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from x1a_world import xrpl_test_key
from xrpl.core.binarycodec import decode, encode, encode_for_signing
from xrpl.core.keypairs import sign

from credence.xrpl_adapter.preparation import TRLUSD_WIRE_CODE, transaction_hash
from credence.xrpl_adapter.quicknode import (
    RIPPLE_EPOCH_OFFSET,
    HostedNetwork,
    QuickNodeConfig,
    QuickNodeEndpoint,
)

TESTNET_ID = 1
VALIDATED = 5_000
HISTORY = f"4000-{VALIDATED}"
CLOSE_TIME = 800_000_000
NOW = CLOSE_TIME + RIPPLE_EPOCH_OFFSET + 4

SENDER = xrpl_test_key("x1q-testnet-sender")
RECEIVER = xrpl_test_key("x1q-testnet-receiver")
ISSUER = xrpl_test_key("x1q-testnet-issuer")
OTHER_ISSUER = xrpl_test_key("x1q-testnet-other-issuer")
SIGNER_A = xrpl_test_key("x1q-testnet-signer-a")
SIGNER_B = xrpl_test_key("x1q-testnet-signer-b")

TOKEN = "x1q" + hashlib.sha256(b"x1q fake quicknode token - not a credential").hexdigest()[:29]
HOST = "fake-endpoint-words.xrp-testnet.quiknode.pro"
URL = f"https://{HOST}/{TOKEN}/"


def endpoint(url: str = URL) -> QuickNodeEndpoint:
    return QuickNodeEndpoint(url, HostedNetwork.TESTNET)


def config(**changes: Any) -> QuickNodeConfig:
    fields: dict[str, Any] = {"endpoint": endpoint(), "network": HostedNetwork.TESTNET}
    fields.update(changes)
    return QuickNodeConfig(**fields)


def ledger_hash_for(index: int) -> str:
    return hashlib.sha256(f"x1q-ledger-{index}".encode()).hexdigest().upper()


def issued(
    value: str, issuer: str | None = None, currency: str = TRLUSD_WIRE_CODE
) -> dict[str, str]:
    return {"currency": currency, "issuer": issuer or ISSUER.account, "value": value}


def signed_testnet_payment(
    *, amount: object = None, flags: int = 0, network_id: int | None = None, sequence: int = 9
) -> bytes:
    """A signed Testnet Payment from SENDER to RECEIVER (really signed with a test key)."""
    transaction: dict[str, Any] = {
        "TransactionType": "Payment",
        "Account": SENDER.account,
        "Destination": RECEIVER.account,
        "Amount": amount if amount is not None else issued("2.5"),
        "Fee": "12",
        "Flags": flags,
        "Sequence": sequence,
        "LastLedgerSequence": VALIDATED + 20,
        "SigningPubKey": SENDER.public_key,
    }
    if network_id is not None:
        transaction["NetworkID"] = network_id
    transaction["TxnSignature"] = sign(
        bytes.fromhex(encode_for_signing(transaction)), SENDER.private_key
    )
    return bytes.fromhex(encode(transaction))


@dataclass
class StoredTx:
    blob: bytes
    validated: bool
    ledger_index: int | None
    result: str = "tesSUCCESS"
    delivered_binary: object = None
    delivered_json: object = "same"


@dataclass
class FakeQuickNode:
    """A fake Clio endpoint implementing ``JsonRpcClient``."""

    network_id: int | None = TESTNET_ID
    ledger_index: int = VALIDATED
    complete_ledgers: str = HISTORY
    close_time: int = CLOSE_TIME
    node_keys: list[str] = field(default_factory=lambda: ["n9FakeBackendNodeA"])
    backend_network_id: int | None = TESTNET_ID
    accounts: dict[str, dict[str, Any]] = field(default_factory=dict)
    signer_lists: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    objects: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    lines: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    transactions: dict[str, StoredTx] = field(default_factory=dict)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    faults: dict[str, Callable[[dict[str, Any]], object]] = field(default_factory=dict)
    page_size: int = 200
    _state_calls: int = 0

    @property
    def ledger_hash(self) -> str:
        return ledger_hash_for(self.ledger_index)

    def include(self, blob: bytes, **changes: Any) -> str:
        fields: dict[str, Any] = {"validated": True, "ledger_index": VALIDATED - 10}
        fields.update(changes)
        self.transactions[transaction_hash(blob)] = StoredTx(blob, **fields)
        return transaction_hash(blob)

    def call(self, method: str, params: Mapping[str, Any]) -> bytes:
        request = dict(params)
        self.calls.append((method, request))
        fault = self.faults.get(method)
        if fault is not None:
            override = fault(request)
            if isinstance(override, bytes):
                return override
            if isinstance(override, Exception):
                raise override
            if override is not None:
                return json.dumps(override).encode()
        document = getattr(self, f"_{method}")(request)
        return json.dumps(document).encode()

    # --- methods ---------------------------------------------------------------------------------

    def _server_info(self, request: Mapping[str, Any]) -> dict[str, Any]:
        info: dict[str, Any] = {
            "clio_version": "2.7.0",
            "libxrpl_version": "3.0.0",
            "rippled_version": "3.1.0",
            "complete_ledgers": self.complete_ledgers,
            "validated_ledger": {
                "age": 4,
                "hash": self.ledger_hash,
                "seq": self.ledger_index,
                "base_fee_xrp": 0.00001,
                "reserve_base_xrp": 1,
                "reserve_inc_xrp": 0.2,
            },
        }
        if self.network_id is not None:
            info["network_id"] = self.network_id
        return {
            "result": {"info": info, "validated": True, "status": "success"},
            "warnings": [{"id": 2001, "message": "This is a clio server."}],
        }

    def _server_state(self, request: Mapping[str, Any]) -> dict[str, Any]:
        key = self.node_keys[self._state_calls % len(self.node_keys)]
        self._state_calls += 1
        state: dict[str, Any] = {
            "build_version": "3.1.0",
            "complete_ledgers": f"{VALIDATED - 50}-{self.ledger_index}",
            "pubkey_node": key,
            "server_state": "full",
            "validated_ledger": {"hash": self.ledger_hash, "seq": self.ledger_index},
        }
        if self.backend_network_id is not None:
            state["network_id"] = self.backend_network_id
        return {
            "result": {"state": state, "status": "success"},
            "forwarded": True,
            "warnings": [{"id": 2001, "message": "This is a clio server."}],
        }

    def _ledger(self, request: Mapping[str, Any]) -> dict[str, Any]:
        index = int(request["ledger_index"])
        return {
            "result": {
                "ledger": {
                    "ledger_hash": ledger_hash_for(index),
                    "ledger_index": str(index),
                    "close_time": self.close_time - (self.ledger_index - index) * 4,
                    "closed": True,
                },
                "ledger_hash": ledger_hash_for(index),
                "ledger_index": index,
                "validated": index <= self.ledger_index,
                "status": "success",
            }
        }

    def _pinned(self, request: Mapping[str, Any]) -> dict[str, Any]:
        assert request["ledger_hash"] == self.ledger_hash, "reads must be pinned by ledger hash"
        return {
            "ledger_hash": self.ledger_hash,
            "ledger_index": self.ledger_index,
            "validated": True,
            "status": "success",
        }

    def _account_info(self, request: Mapping[str, Any]) -> dict[str, Any]:
        account = request["account"]
        if account not in self.accounts:
            return {"result": {"status": "error", "error": "actNotFound"}}
        result = self._pinned(request)
        result["account_data"] = {"Account": account, **self.accounts[account]}
        if request.get("signer_lists"):
            result["signer_lists"] = self.signer_lists.get(account, [])
        return {"result": result}

    def _page(self, request: Mapping[str, Any], items: list[Any], key: str) -> dict[str, Any]:
        result = self._pinned(request)
        start = int(request.get("marker", 0))
        result[key] = items[start : start + self.page_size]
        if start + self.page_size < len(items):
            result["marker"] = str(start + self.page_size)
        return {"result": result}

    def _account_objects(self, request: Mapping[str, Any]) -> dict[str, Any]:
        items = self.objects.get(request["account"], [])
        if "type" in request:
            wanted = request["type"].replace("_", "").lower()
            items = [item for item in items if item["LedgerEntryType"].lower() == wanted]
        return self._page(request, items, "account_objects")

    def _account_lines(self, request: Mapping[str, Any]) -> dict[str, Any]:
        items = self.lines.get(request["account"], [])
        if "peer" in request:
            items = [item for item in items if item["account"] == request["peer"]]
        return self._page(request, items, "lines")

    def _tx(self, request: Mapping[str, Any]) -> dict[str, Any]:
        stored = self.transactions.get(request["transaction"])
        if stored is None:
            return {"result": {"status": "error", "error": "txnNotFound"}}
        tx_hash = transaction_hash(stored.blob)
        result: dict[str, Any] = {"hash": tx_hash, "validated": stored.validated}
        result["status"] = "success"
        if stored.ledger_index is not None:
            result["ledger_index"] = stored.ledger_index
            if stored.validated:
                result["ledger_hash"] = ledger_hash_for(stored.ledger_index)
        decoded = decode(stored.blob.hex().upper())
        if not stored.validated:
            if request["binary"]:
                result["tx_blob"] = stored.blob.hex().upper()
            else:
                result["tx_json"] = _v2_tx_json(decoded)
            return {"result": result}
        meta: dict[str, Any] = {
            "TransactionIndex": 0,
            "TransactionResult": stored.result,
            "AffectedNodes": [],
        }
        if stored.delivered_binary is not None:
            meta["DeliveredAmount"] = stored.delivered_binary
        if request["binary"]:
            result["tx_blob"] = stored.blob.hex().upper()
            result["meta_blob"] = encode(meta)
        else:
            json_meta = {k: v for k, v in meta.items() if k != "DeliveredAmount"}
            if stored.result == "tesSUCCESS":
                if stored.delivered_json == "same":
                    json_meta["delivered_amount"] = (
                        stored.delivered_binary
                        if stored.delivered_binary is not None
                        else decoded["Amount"]
                    )
                elif stored.delivered_json is not None:
                    json_meta["delivered_amount"] = stored.delivered_json
            result["meta"] = json_meta
            result["tx_json"] = _v2_tx_json(decoded)
        return {"result": result}


def _v2_tx_json(decoded: dict[str, Any]) -> dict[str, Any]:
    """API v2 renames a Payment's ``Amount`` to ``DeliverMax`` and moves ``hash`` to the root."""
    tx_json = dict(decoded)
    tx_json["DeliverMax"] = tx_json.pop("Amount")
    return tx_json


def funded_sender(node: FakeQuickNode) -> None:
    node.accounts[SENDER.account] = {
        "Balance": "90000000",
        "Flags": 0,
        "OwnerCount": 2,
        "Sequence": 9,
    }
    node.signer_lists[SENDER.account] = [
        {
            "LedgerEntryType": "SignerList",
            "SignerQuorum": 2,
            "SignerEntries": [
                {"SignerEntry": {"Account": SIGNER_A.account, "SignerWeight": 1}},
                {"SignerEntry": {"Account": SIGNER_B.account, "SignerWeight": 1}},
            ],
        }
    ]
    node.objects[SENDER.account] = [
        {"LedgerEntryType": "SignerList", "SignerQuorum": 2},
        {"LedgerEntryType": "RippleState", "Balance": issued("1")},
    ]
    node.lines[SENDER.account] = [
        {
            "account": ISSUER.account,
            "currency": TRLUSD_WIRE_CODE,
            "balance": "10.5",
            "limit": "1000",
            "limit_peer": "0",
            "no_ripple": True,
        }
    ]


# --- scripted loopback HTTP endpoint (real sockets on 127.0.0.1; no TLS, no network) ----------


@dataclass
class Reply:
    status: int = 200
    body: bytes = b'{"result":{"status":"success","info":{}}}'
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class Raw:
    """Bytes sent verbatim as the whole response (for malformed status lines and headers)."""

    data: bytes


@dataclass
class Drip:
    """A 200 response sent one byte at a time; ``body`` defaults to an endless stream."""

    body: bytes | None = None
    interval: float = 0.05
    drip_headers: bool = False


@dataclass
class Stall:
    """Read the request, then send nothing until the client goes away."""

    seconds: float = 30.0


@dataclass
class Seen:
    method: str
    target: str
    headers: dict[str, str]
    body: bytes


Action = Reply | Raw | Drip | Stall


class LoopbackQuickNode:
    """One scripted action per accepted connection. Records each request and when each response
    ended (``closed``: seconds since the server started, when the client hung up or we finished)."""

    def __init__(self, script: list[Action]) -> None:
        self.script = list(script)
        self.requests: list[Seen] = []
        self.closed: list[float] = []
        self.started = time.monotonic()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(16)
        self.address = self._listener.getsockname()
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self) -> None:
        self._listener.close()

    def _accept(self) -> None:
        while True:
            try:
                connection, _ = self._listener.accept()
            except OSError:
                return
            action = self.script.pop(0) if self.script else None
            threading.Thread(target=self._serve, args=(connection, action), daemon=True).start()

    def _serve(self, connection: socket.socket, action: Action | None) -> None:
        try:
            self._read_request(connection)
            if isinstance(action, Reply):
                head = (
                    f"HTTP/1.1 {action.status} Scripted\r\nContent-Length: {len(action.body)}\r\n"
                )
                head += "".join(f"{name}: {value}\r\n" for name, value in action.headers.items())
                connection.sendall(head.encode() + b"\r\n" + action.body)
            elif isinstance(action, Raw):
                connection.sendall(action.data)
            elif isinstance(action, Drip):
                body = action.body
                length = len(body) if body is not None else 1_000_000
                head = f"HTTP/1.1 200 OK\r\nContent-Length: {length}\r\n\r\n".encode()
                for piece in head if action.drip_headers else [head]:
                    connection.sendall(bytes([piece]) if isinstance(piece, int) else piece)
                    if action.drip_headers:
                        time.sleep(action.interval)
                stream = body if body is not None else b" " * length
                for byte in stream:
                    connection.sendall(bytes([byte]))
                    time.sleep(action.interval)
            elif isinstance(action, Stall):
                connection.settimeout(action.seconds)
                while connection.recv(1024):
                    pass
        except OSError:
            pass
        finally:
            self.closed.append(time.monotonic() - self.started)
            connection.close()

    def _read_request(self, connection: socket.socket) -> None:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = connection.recv(65536)
            if not chunk:
                break
            data += chunk
            if not data[:1].isalpha():  # a TLS ClientHello or other non-HTTP bytes
                self.requests.append(Seen("<not http>", "", {}, b""))
                return
        head, _, body = data.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) != 3 or not parts[2].startswith("HTTP/"):
            self.requests.append(Seen("<not http>", "", {}, b""))
            return
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        length = int(headers.get("content-length", "0"))
        while len(body) < length:
            chunk = connection.recv(65536)
            if not chunk:
                break
            body += chunk
        self.requests.append(Seen(parts[0], parts[1], headers, body))


@dataclass
class Wiring:
    """Test seams for ``QuickNodeJsonRpcClient``: resolve to and connect to the loopback server."""

    server: LoopbackQuickNode
    failures: list[BaseException] = field(default_factory=list)
    resolved: list[tuple[str, int]] = field(default_factory=list, repr=False)
    connected: list[tuple[str, float]] = field(default_factory=list, repr=False)

    def resolver(self, host: str, port: int, timeout: float) -> tuple[int, Any]:
        self.resolved.append((host, port))
        return socket.AF_INET, self.server.address

    def connector(
        self, address: tuple[int, Any], server_hostname: str, deadline: float, watchdog: Any
    ) -> socket.socket:
        self.connected.append((server_hostname, deadline))
        if self.failures:
            raise self.failures.pop(0)
        sock = socket.create_connection(address[1], timeout=max(deadline - time.monotonic(), 0.01))
        watchdog.attach(sock)
        return sock
