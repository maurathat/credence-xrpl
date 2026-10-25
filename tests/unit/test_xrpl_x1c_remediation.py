"""X1c regressions for Salvador's X1b findings B1-B5 and C2-C3. Offline; loopback sockets only.

Each test reproduces the reviewer's exploit and asserts the corrected, fail-closed outcome.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import re
import shutil
import sqlite3
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
from x1a_rippled import NODE_KEY, FakeRippled, standard_ledger
from x1a_world import (
    ATTACKER,
    ISSUER,
    LAST_LEDGER,
    NETWORK_ID,
    OWNER,
    POLICY,
    PROVIDER_A,
    RESEARCHBOT,
    RULES,
    SEQUENCE,
    VALIDATED_LEDGER,
    account_snapshot,
    direct_policy_signature,
    expected_payment,
    ledger_context,
    researchbot_signature,
    signer_list,
    signer_snapshot,
    unsigned_payment,
    xrpl_test_key,
)
from xrpl.constants import CryptoAlgorithm
from xrpl.core.addresscodec import decode_classic_address
from xrpl.core.binarycodec import encode

from credence.xrpl_adapter.journal import PaymentJournal
from credence.xrpl_adapter.mock import DeterministicMockTransport
from credence.xrpl_adapter.multisign import (
    MultisignRejected,
    MultisignSignature,
    SignerEntry,
    assemble_multisigned,
    is_canonical_signature,
    sign_as_signer,
    verify_multisigned_payment,
)
from credence.xrpl_adapter.preparation import (
    TRLUSD_WIRE_CODE,
    OfflinePaymentPreparer,
    XrplSignedPaymentInspector,
    transaction_hash,
)
from credence.xrpl_adapter.rippled import (
    LoopbackJsonRpcClient,
    RippledTransport,
    RippledTransportConfig,
    RpcUnavailable,
)
from credence.xrpl_adapter.service import PaymentService
from credence.xrpl_adapter.settlement import SqliteSettlementEventConsumer
from credence.xrpl_adapter.types import (
    Asset,
    LegacySettlementClaimRetired,
    LookupDisposition,
    PaymentRequest,
    SettlementEvent,
    SubmissionResponse,
    SupersededJournalSchema,
    TransactionState,
    UnsupportedJournalSchema,
)

OPERATION = "operation-x1c"
REVIEW_AMOUNT = 2_500_000  # Salvador's reproduction pays 2.5 tRLUSD (a test token)
CONFIG = RippledTransportConfig(
    source_id="local-rippled",
    expected_network_id=NETWORK_ID,
    expected_build_version="3.4.0",
    expected_node_public_key=NODE_KEY,
)
ASSET = Asset("tRLUSD", TRLUSD_WIRE_CODE, ISSUER.account)


@dataclass
class Chain:
    rippled: FakeRippled
    service: PaymentService
    offline: DeterministicMockTransport
    signed: bytes
    database: Path


def chain(
    tmp_path: Path, *, amount: int = REVIEW_AMOUNT, config: RippledTransportConfig = CONFIG
) -> Chain:
    """Multisigned payment -> X0t journal -> offline submit -> lookup through RippledTransport."""
    rippled = standard_ledger()
    trusted = RippledTransport(rippled, config)
    transaction = unsigned_payment(amount_micro_units=amount)
    signed = assemble_multisigned(
        transaction, [researchbot_signature(transaction), direct_policy_signature(transaction)]
    )
    offline = DeterministicMockTransport(ledger=trusted.validated_ledger(OWNER.account))
    database = tmp_path / "journal.sqlite3"
    service = PaymentService(
        journal=PaymentJournal(database),
        preparer=OfflinePaymentPreparer(maximum_fee_drops=100),
        inspector=XrplSignedPaymentInspector(),
        transport=offline,
        lookup=trusted,
        ledger_observer=trusted,
    )
    service.prepare_and_persist(
        PaymentRequest(
            operation_id=OPERATION,
            payment_authorization_id="authorization-x1c",
            account=OWNER.account,
            destination=PROVIDER_A.account,
            asset=ASSET,
            amount_micro_units=amount,
            fee_drops=30,
            network_id=NETWORK_ID,
            last_ledger_sequence=LAST_LEDGER,
            earliest_ledger_sequence=VALIDATED_LEDGER,
            sequence=SEQUENCE,
        ),
        signed,
    )
    offline.queue_submission(SubmissionResponse(True, "tesSUCCESS", "queued offline"))
    assert service.submit(OPERATION).state is TransactionState.PENDING_CONFIRMATION
    return Chain(rippled, service, offline, signed, database)


def trlusd(
    value: str, issuer: str | None = None, currency: str = TRLUSD_WIRE_CODE
) -> dict[str, str]:
    return {"currency": currency, "issuer": issuer or ISSUER.account, "value": value}


def json_reports(rippled: FakeRippled, delivered: object) -> None:
    """Make only the JSON form of `tx` report `delivered` (the binary form is untouched)."""
    real = rippled._tx

    def fault(request: dict[str, Any]) -> dict[str, Any] | None:
        if request["binary"]:
            return None
        result = real(request)
        result["meta"]["delivered_amount"] = delivered
        return result

    rippled.faults["tx"] = fault


def outbox_events(database: Path) -> int:
    with sqlite3.connect(database) as connection:
        return int(connection.execute("SELECT COUNT(*) FROM xrpl_settlement_outbox").fetchone()[0])


# --- B1: binary and JSON delivered amounts must agree ----------------------------------------


def test_b1_binary_point_one_versus_json_two_point_five_never_settles(tmp_path: Path) -> None:
    """Salvador's exploit: binary DeliveredAmount 0.1, JSON delivered_amount 2.5."""
    flow = chain(tmp_path)
    flow.rippled.include(flow.signed, ledger_index=VALIDATED_LEDGER + 1, delivered=trlusd("0.1"))
    flow.rippled.advance(VALIDATED_LEDGER + 1)
    json_reports(flow.rippled, trlusd("2.5"))

    record = flow.service.reconcile(OPERATION)
    assert record.state is TransactionState.UNKNOWN
    assert record.last_provisional_status == "EVIDENCE_INCONSISTENT"
    assert record.settlement_status is None
    assert record.verified_delivered_amount_micro_units is None
    assert outbox_events(flow.database) == 0


@pytest.mark.parametrize(
    ("binary", "observed", "outcome"),
    [
        (trlusd("2.5"), trlusd("2.50"), "settles"),  # exact decimal equality
        (None, trlusd("2.5"), "settles"),  # no DeliveredAmount: the signed Amount applies
        (None, trlusd("0.1"), "EVIDENCE_INCONSISTENT"),
        (trlusd("2.5"), trlusd("2.5", issuer=ATTACKER.account), "EVIDENCE_INCONSISTENT"),
        (trlusd("2.5"), trlusd("2.5", currency="USD"), "EVIDENCE_INCONSISTENT"),
        (trlusd("2.5"), "unavailable", "EVIDENCE_INCONSISTENT"),
        (trlusd("2.5"), None, "EVIDENCE_INCONSISTENT"),
    ],
)
def test_b1_delivered_amount_observations(
    tmp_path: Path, binary: dict[str, str] | None, observed: object, outcome: str
) -> None:
    flow = chain(tmp_path)
    flow.rippled.include(flow.signed, ledger_index=VALIDATED_LEDGER + 1, delivered=binary)
    flow.rippled.advance(VALIDATED_LEDGER + 1)
    json_reports(flow.rippled, observed)
    record = flow.service.reconcile(OPERATION)
    if outcome == "settles":
        assert record.state is TransactionState.VALIDATED_SUCCESS
        assert record.verified_delivered_amount_micro_units == REVIEW_AMOUNT
        assert outbox_events(flow.database) == 1
    else:
        assert (record.state, record.last_provisional_status) == (TransactionState.UNKNOWN, outcome)
        assert outbox_events(flow.database) == 0


def test_b1_failed_payment_reporting_a_delivery_is_inconsistent(tmp_path: Path) -> None:
    flow = chain(tmp_path)
    flow.rippled.include(flow.signed, ledger_index=VALIDATED_LEDGER + 1, result="tecPATH_DRY")
    flow.rippled.advance(VALIDATED_LEDGER + 1)
    json_reports(flow.rippled, trlusd("2.5"))
    record = flow.service.reconcile(OPERATION)
    assert (record.state, record.last_provisional_status) == (
        TransactionState.UNKNOWN,
        "EVIDENCE_INCONSISTENT",
    )


def test_b1_evidence_keeps_both_observations(tmp_path: Path) -> None:
    flow = chain(tmp_path)
    flow.rippled.include(flow.signed, ledger_index=VALIDATED_LEDGER + 1, delivered=None)
    flow.rippled.advance(VALIDATED_LEDGER + 1)
    json_reports(flow.rippled, trlusd("2.5"))
    record = flow.service.reconcile(OPERATION)
    assert record.state is TransactionState.VALIDATED_SUCCESS
    envelope = json.loads(record.final_evidence or b"")
    observations = envelope["delivered_amount_observations"]
    assert observations["binary"] == {**trlusd("2.5"), "source": "Amount"}
    assert observations["json"] == trlusd("2.5")


# --- B2: the node's identity is re-verified on every lookup ----------------------------------


def include_and_advance(flow: Chain, index: int = VALIDATED_LEDGER + 1) -> None:
    flow.rippled.include(flow.signed, ledger_index=index, delivered=trlusd("2.5"))
    flow.rippled.advance(index)


@pytest.mark.parametrize(
    ("change", "detail"),
    [
        (lambda node: setattr(node, "network_id", 1), "NETWORK_MISMATCH"),
        (lambda node: setattr(node, "build_version", "3.5.0-b1"), "BUILD_VERSION_MISMATCH"),
        (lambda node: setattr(node, "complete_ledgers", "1-50"), "LEDGER_OUTSIDE_COMPLETE_HISTORY"),
        (
            lambda node: setattr(
                node,
                "complete_ledgers",
                f"1-{VALIDATED_LEDGER},{VALIDATED_LEDGER + 2}-{VALIDATED_LEDGER + 3}",
            ),
            "LEDGER_OUTSIDE_COMPLETE_HISTORY",
        ),
    ],
)
def test_b2_node_identity_change_between_preparation_and_lookup(
    tmp_path: Path, change: Any, detail: str
) -> None:
    flow = chain(tmp_path)
    include_and_advance(flow)
    if detail == "LEDGER_OUTSIDE_COMPLETE_HISTORY":
        flow.rippled.advance(VALIDATED_LEDGER + 3)
    change(flow.rippled)
    record = flow.service.reconcile(OPERATION)
    assert (record.state, record.last_provisional_status) == (TransactionState.UNKNOWN, detail)
    assert outbox_events(flow.database) == 0


def test_b2_recovers_once_the_trusted_identity_is_restored(tmp_path: Path) -> None:
    flow = chain(tmp_path)
    include_and_advance(flow)
    flow.rippled.network_id = 1
    assert flow.service.reconcile(OPERATION).state is TransactionState.UNKNOWN
    flow.rippled.network_id = NETWORK_ID
    assert flow.service.reconcile(OPERATION).state is TransactionState.VALIDATED_SUCCESS
    assert outbox_events(flow.database) == 1


def test_b2_transaction_ledger_beyond_the_validated_ledger_is_refused(tmp_path: Path) -> None:
    flow = chain(tmp_path)
    flow.rippled.include(flow.signed, ledger_index=VALIDATED_LEDGER + 5, delivered=trlusd("2.5"))
    flow.rippled.advance(VALIDATED_LEDGER + 4)
    record = flow.service.reconcile(OPERATION)
    assert (record.state, record.last_provisional_status) == (
        TransactionState.UNKNOWN,
        "LEDGER_BEYOND_VALIDATED",
    )


def test_b2_pinned_node_key_is_checked_on_lookup(tmp_path: Path) -> None:
    pinned = CONFIG
    rippled = standard_ledger()
    blob = assemble_multisigned(
        unsigned_payment(),
        [researchbot_signature(unsigned_payment()), direct_policy_signature(unsigned_payment())],
    )
    tx_hash = rippled.include(blob, ledger_index=VALIDATED_LEDGER - 1, delivered=trlusd("1.5"))
    assert RippledTransport(rippled, pinned).lookup(tx_hash).disposition is LookupDisposition.FOUND
    rippled.node_public_key = "n9SomeOtherNode"
    result = RippledTransport(rippled, pinned).lookup(tx_hash)
    assert (result.disposition, result.detail) == (
        LookupDisposition.UNAVAILABLE,
        "NODE_IDENTITY_MISMATCH",
    )


def test_b2_transport_requires_a_build_pin() -> None:
    with pytest.raises(ValueError):
        RippledTransport(standard_ledger(), replace(CONFIG, expected_build_version=" "))


# --- B4: RegularKey on either signer account --------------------------------------------------


@pytest.mark.parametrize("signer", [RESEARCHBOT, POLICY], ids=["researchbot", "policy-signer"])
def test_b4_signer_account_regular_key_is_refused(signer: Any) -> None:
    transaction = unsigned_payment()
    signed = assemble_multisigned(
        transaction, [researchbot_signature(transaction), direct_policy_signature(transaction)]
    )
    other = POLICY if signer is RESEARCHBOT else RESEARCHBOT
    ledger = ledger_context(
        signers={
            signer.account: signer_snapshot(
                signer, exists=True, sequence=1, regular_key=ATTACKER.account
            ),
            other.account: signer_snapshot(other),
        }
    )
    with pytest.raises(MultisignRejected) as raised:
        verify_multisigned_payment(
            signed, policy=RULES, expected=expected_payment(transaction), ledger=ledger
        )
    assert raised.value.code == "SIGNER_REGULAR_KEY_PRESENT"


def test_b4_funded_signer_without_regular_key_is_accepted() -> None:
    transaction = unsigned_payment()
    signed = assemble_multisigned(
        transaction, [researchbot_signature(transaction), direct_policy_signature(transaction)]
    )
    ledger = ledger_context(
        signers={
            RESEARCHBOT.account: signer_snapshot(RESEARCHBOT, exists=True, sequence=1),
            POLICY.account: signer_snapshot(POLICY, exists=True, sequence=1),
        }
    )
    verify_multisigned_payment(
        signed, policy=RULES, expected=expected_payment(transaction), ledger=ledger
    )


# --- B5: one settlement-effect authority -------------------------------------------------------


def settled_chain(tmp_path: Path) -> Chain:
    flow = chain(tmp_path)
    include_and_advance(flow)
    assert flow.service.reconcile(OPERATION).state is TransactionState.VALIDATED_SUCCESS
    return flow


def test_b5_legacy_claim_is_refused_and_creates_no_effect(tmp_path: Path) -> None:
    flow = settled_chain(tmp_path)
    with pytest.raises(LegacySettlementClaimRetired) as raised:
        flow.service.claim_xrpl_settlement(OPERATION)
    assert raised.value.code == "LEGACY_SETTLEMENT_CLAIM_RETIRED"
    assert not flow.service.status.status(OPERATION).settlement_effect_claimed
    assert outbox_events(flow.database) == 1


def test_b5_previous_claim_history_is_preserved(tmp_path: Path) -> None:
    flow = settled_chain(tmp_path)
    # A record claimed before X1c (fixture-only write simulating the X0t-era claim).
    with sqlite3.connect(flow.database) as connection:
        connection.execute(
            "UPDATE xrpl_payment_transactions SET settlement_effect_claimed = 1 "
            "WHERE operation_id = ?",
            (OPERATION,),
        )
    reopened = PaymentJournal(flow.database)
    with pytest.raises(LegacySettlementClaimRetired):
        reopened.claim_settlement_effect(OPERATION)
    record = reopened.get(OPERATION)
    assert record.settlement_effect_claimed and record.settlement_status == "XRPL_SETTLED"
    assert len(reopened.pending_settlement_events()) == 1


def test_b5_outbox_is_the_one_path_across_restart_and_concurrency(tmp_path: Path) -> None:
    flow = settled_chain(tmp_path)
    effects: list[str] = []
    lock = threading.Lock()

    def effect(connection: sqlite3.Connection, event: SettlementEvent) -> None:
        with lock:
            effects.append(event.event_id)

    class CrashAfterApply:
        def __init__(self, consumer: SqliteSettlementEventConsumer) -> None:
            self.consumer = consumer

        def apply(self, event: SettlementEvent) -> None:
            self.consumer.apply(event)
            raise RuntimeError("crash after the destination committed")

    consumer = SqliteSettlementEventConsumer(tmp_path / "destination.sqlite3", effect)
    assert flow.service.deliver_settlement_events(CrashAfterApply(consumer)) == ()
    services = [
        PaymentService(
            journal=PaymentJournal(flow.database),
            preparer=OfflinePaymentPreparer(maximum_fee_drops=100),
            inspector=XrplSignedPaymentInspector(),
            transport=flow.offline,
            lookup=flow.offline,
            ledger_observer=flow.offline,
        )
        for _ in range(6)
    ]
    barrier = threading.Barrier(len(services))

    def deliver(service: PaymentService) -> None:
        barrier.wait()
        service.deliver_settlement_events(consumer)
        with pytest.raises(LegacySettlementClaimRetired):
            service.claim_xrpl_settlement(OPERATION)

    threads = [threading.Thread(target=deliver, args=(service,)) for service in services]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(effects) == 1
    assert PaymentJournal(flow.database).pending_settlement_events() == ()


# --- C2: canonical signatures ------------------------------------------------------------------

SECP_RESEARCHBOT = xrpl_test_key("x1c-researchbot-secp256k1", CryptoAlgorithm.SECP256K1)
SECP_POLICY = xrpl_test_key("x1c-policy-signer-secp256k1", CryptoAlgorithm.SECP256K1)
SECP_RULES = replace(
    RULES,
    researchbot_account=SECP_RESEARCHBOT.account,
    policy_signer_account=SECP_POLICY.account,
)
SECP_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
ED25519_L = 2**252 + 27742317777372353535851937790883648493


def secp_ledger() -> Any:
    return ledger_context(
        account=account_snapshot(
            signer_list=signer_list(
                entries=(
                    SignerEntry(SECP_RESEARCHBOT.account, 1),
                    SignerEntry(SECP_POLICY.account, 1),
                )
            )
        ),
        signers={
            SECP_RESEARCHBOT.account: signer_snapshot(SECP_RESEARCHBOT),
            SECP_POLICY.account: signer_snapshot(SECP_POLICY),
        },
    )


def secp_signatures(transaction: dict[str, Any]) -> list[MultisignSignature]:
    return [
        sign_as_signer(
            transaction, account=key.account, public_key=key.public_key, private_key=key.private_key
        )
        for key in (SECP_RESEARCHBOT, SECP_POLICY)
    ]


def der(r: int, s: int) -> bytes:
    def integer(value: int) -> bytes:
        body = value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big")
        if body[0] & 0x80:
            body = b"\x00" + body
        return b"\x02" + bytes([len(body)]) + body

    content = integer(r) + integer(s)
    return b"\x30" + bytes([len(content)]) + content


def der_parts(signature: bytes) -> tuple[int, int]:
    r_length = signature[3]
    r = int.from_bytes(signature[4 : 4 + r_length], "big")
    s = int.from_bytes(signature[6 + r_length :], "big")
    return r, s


def encode_signers(transaction: dict[str, Any], signatures: list[MultisignSignature]) -> bytes:
    ordered = sorted(signatures, key=lambda item: decode_classic_address(item.account))
    return bytes.fromhex(
        encode(
            {
                **transaction,
                "Signers": [
                    {
                        "Signer": {
                            "Account": item.account,
                            "SigningPubKey": item.signing_public_key,
                            "TxnSignature": item.signature,
                        }
                    }
                    for item in ordered
                ],
            }
        )
    )


def test_c2_canonical_secp256k1_multisignature_verifies() -> None:
    transaction = unsigned_payment()
    signed = assemble_multisigned(transaction, secp_signatures(transaction))
    verify_multisigned_payment(
        signed, policy=SECP_RULES, expected=expected_payment(transaction), ledger=secp_ledger()
    )


def test_c2_high_s_secp256k1_signature_is_refused() -> None:
    transaction = unsigned_payment()
    canonical = secp_signatures(transaction)
    r, s = der_parts(bytes.fromhex(canonical[1].signature))
    high = replace(canonical[1], signature=der(r, SECP_N - s).hex().upper())
    malleated = encode_signers(transaction, [canonical[0], high])
    original = encode_signers(transaction, canonical)
    assert transaction_hash(malleated) != transaction_hash(original)  # the malleability at stake
    with pytest.raises(MultisignRejected) as raised:
        verify_multisigned_payment(
            malleated,
            policy=SECP_RULES,
            expected=expected_payment(transaction),
            ledger=secp_ledger(),
        )
    assert raised.value.code == "SIGNATURE_NOT_CANONICAL"
    with pytest.raises(MultisignRejected):
        assemble_multisigned(transaction, [canonical[0], high])


def test_c2_canonicality_rules() -> None:
    transaction = unsigned_payment()
    secp = secp_signatures(transaction)[0]
    good = bytes.fromhex(secp.signature)
    r, s = der_parts(good)
    key = secp.signing_public_key
    assert is_canonical_signature(key, good)
    assert not is_canonical_signature(key, der(r, SECP_N - s))  # high S
    padded = b"\x30" + bytes([good[1] + 1]) + b"\x02" + bytes([good[3] + 1]) + b"\x00" + good[4:]
    if not good[4] & 0x80:
        assert not is_canonical_signature(key, padded)  # non-minimal integer
    assert not is_canonical_signature(key, good + b"\x00")  # trailing bytes
    assert not is_canonical_signature(key, der(0, s))
    assert not is_canonical_signature(key, der(SECP_N, s))
    ed = researchbot_signature(transaction)
    ed_bytes = bytes.fromhex(ed.signature)
    assert is_canonical_signature(ed.signing_public_key, ed_bytes)
    big_s = (int.from_bytes(ed_bytes[32:], "little") + ED25519_L) % 2**256
    assert not is_canonical_signature(
        ed.signing_public_key, ed_bytes[:32] + big_s.to_bytes(32, "little")
    )
    assert not is_canonical_signature(ed.signing_public_key, ed_bytes[:63])


def test_c2_ed25519_signature_with_s_plus_l_is_refused() -> None:
    transaction = unsigned_payment()
    agent = researchbot_signature(transaction)
    raw = bytes.fromhex(agent.signature)
    s = int.from_bytes(raw[32:], "little") + ED25519_L
    if s >= 2**256:
        pytest.skip("S + L does not fit in 32 bytes for this deterministic signature")
    forged = replace(agent, signature=(raw[:32] + s.to_bytes(32, "little")).hex().upper())
    signed = encode_signers(transaction, [forged, direct_policy_signature(transaction)])
    with pytest.raises(MultisignRejected) as raised:
        verify_multisigned_payment(
            signed, policy=RULES, expected=expected_payment(transaction), ledger=ledger_context()
        )
    assert raised.value.code == "SIGNATURE_NOT_CANONICAL"


# --- C3: no redirects --------------------------------------------------------------------------


def test_c3_loopback_client_refuses_a_302_redirect() -> None:
    """Salvador's reproduction: a 302 from the pinned endpoint to http://0.0.0.0:<port>/."""
    hits: list[str | None] = []

    class Target(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            hits.append(self.headers.get("Host"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"result":{"status":"success","state":{}}}')

        do_GET = do_POST  # urllib turns a followed 302 POST into a GET

        def log_message(self, *args: object) -> None:
            pass

    target = http.server.HTTPServer(("127.0.0.1", 0), Target)

    class Redirect(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.send_response(302)
            self.send_header("Location", f"http://0.0.0.0:{target.server_port}/")
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    redirect = http.server.HTTPServer(("127.0.0.1", 0), Redirect)
    for server in (target, redirect):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with pytest.raises(RpcUnavailable):
            LoopbackJsonRpcClient(f"http://127.0.0.1:{redirect.server_port}").call(
                "server_state", {}
            )
    finally:
        for server in (target, redirect):
            server.shutdown()
            server.server_close()
    assert hits == []


# --- C1: validated success with the wrong delivery -------------------------------------------


def short_delivery(tmp_path: Path, value: str = "0.1") -> Chain:
    flow = chain(tmp_path)
    flow.rippled.include(flow.signed, ledger_index=VALIDATED_LEDGER + 1, delivered=trlusd(value))
    flow.rippled.advance(VALIDATED_LEDGER + 1)
    return flow


def test_c1_correct_delivery_settles(tmp_path: Path) -> None:
    flow = short_delivery(tmp_path, "2.5")
    record = flow.service.reconcile(OPERATION)
    assert record.state is TransactionState.VALIDATED_SUCCESS
    assert not flow.service.status.requires_operator_reconciliation(OPERATION)
    assert outbox_events(flow.database) == 1


def test_c1_short_delivery_is_unexpected_delivery_not_ledger_failure(tmp_path: Path) -> None:
    flow = short_delivery(tmp_path)
    record = flow.service.reconcile(OPERATION)
    assert record.state is TransactionState.VALIDATED_UNEXPECTED_DELIVERY
    assert record.state is not TransactionState.VALIDATED_FAILURE
    assert record.final_result == "DELIVERED_AMOUNT_MISMATCH"
    assert record.verified_delivered_amount_micro_units == 100_000  # what actually moved
    assert record.settlement_status is None
    envelope = json.loads(record.final_evidence or b"")
    assert envelope["meta"]["TransactionResult"] == "tesSUCCESS"
    assert envelope["delivered_amount_observations"]["binary"]["value"] == "0.1"
    assert outbox_events(flow.database) == 0
    assert flow.service.status.requires_operator_reconciliation(OPERATION)
    assert [item.operation_id for item in flow.service.status.operator_reconciliation_queue()] == [
        OPERATION
    ]


def test_c1_unexpected_delivery_is_terminal_across_restart_and_never_retried(
    tmp_path: Path,
) -> None:
    flow = short_delivery(tmp_path)
    first = flow.service.reconcile(OPERATION)
    broadcasts = len(flow.offline.submitted)
    flow.rippled.transactions.clear()  # even if the node later "forgets" the transaction
    restarted = PaymentService(
        journal=PaymentJournal(flow.database),
        preparer=OfflinePaymentPreparer(maximum_fee_drops=100),
        inspector=XrplSignedPaymentInspector(),
        transport=flow.offline,
        lookup=RippledTransport(flow.rippled, CONFIG),
        ledger_observer=RippledTransport(flow.rippled, CONFIG),
    )
    assert restarted.reconcile(OPERATION, resubmit_if_pending=True) == first
    assert restarted.reconcile_after_restart() == ()
    assert len(flow.offline.submitted) == broadcasts
    assert outbox_events(flow.database) == 0
    assert restarted.status.requires_operator_reconciliation(OPERATION)


def test_c1_wrong_asset_delivery_is_unexpected_delivery(tmp_path: Path) -> None:
    flow = chain(tmp_path)
    other = trlusd("2.5", issuer=ATTACKER.account)
    flow.rippled.include(flow.signed, ledger_index=VALIDATED_LEDGER + 1, delivered=other)
    flow.rippled.advance(VALIDATED_LEDGER + 1)
    record = flow.service.reconcile(OPERATION)
    assert (record.state, record.final_result) == (
        TransactionState.VALIDATED_UNEXPECTED_DELIVERY,
        "DELIVERED_ASSET_MISMATCH",
    )
    assert outbox_events(flow.database) == 0


def test_c1_contradictory_evidence_is_not_classified_as_delivery(tmp_path: Path) -> None:
    flow = short_delivery(tmp_path)
    json_reports(flow.rippled, trlusd("2.5"))
    record = flow.service.reconcile(OPERATION)
    assert (record.state, record.last_provisional_status) == (
        TransactionState.UNKNOWN,
        "EVIDENCE_INCONSISTENT",
    )
    assert not flow.service.status.requires_operator_reconciliation(OPERATION)


# --- C1: journal schema version 3; version 2 refused unchanged ----------------------------------

FIXTURES = Path(__file__).parents[1] / "fixtures" / "xrpl"


def snapshot(database: Path) -> tuple[str, tuple[str, ...]]:
    digest = hashlib.sha256(database.read_bytes()).hexdigest()
    with sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True) as connection:
        return digest, tuple(connection.iterdump())


@pytest.mark.parametrize("name", ["x1b_v2_journal.fixture", "x1b_v2_empty_journal.fixture"])
def test_c1_version_two_journal_is_refused_without_change(tmp_path: Path, name: str) -> None:
    database = tmp_path / "v2.db"
    shutil.copyfile(FIXTURES / name, database)
    os.chmod(database, 0o640)
    before = snapshot(database)
    before_files = tuple(tmp_path.iterdir())
    for _ in range(3):
        with pytest.raises(SupersededJournalSchema, match=r"^JOURNAL_VERSION_2_UNSUPPORTED:") as e:
            PaymentJournal(database)
        assert e.value.code == "JOURNAL_VERSION_2_UNSUPPORTED"
        assert snapshot(database) == before
        assert database.stat().st_mode & 0o777 == 0o640
        assert tuple(tmp_path.iterdir()) == before_files


def test_c1_version_two_history_is_preserved() -> None:
    uri = f"{(FIXTURES / 'x1b_v2_journal.fixture').resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        rows = connection.execute(
            "SELECT operation_id, state, final_result, settlement_status, "
            "settlement_effect_claimed, verified_delivered_amount_micro_units "
            "FROM xrpl_payment_transactions ORDER BY operation_id"
        ).fetchall()
        events = connection.execute(
            "SELECT operation_id, delivered_at FROM xrpl_settlement_outbox"
        ).fetchall()
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert version == 2
    assert rows == [
        ("v2-expired", "EXPIRED_NOT_INCLUDED", "txnNotFound", None, 0, None),
        ("v2-failure", "VALIDATED_FAILURE", "tecPATH_DRY", None, 0, None),
        ("v2-pending", "PENDING_CONFIRMATION", None, None, 0, None),
        ("v2-short-delivery", "VALIDATED_FAILURE", "DELIVERED_AMOUNT_MISMATCH", None, 0, 100_000),
        ("v2-success", "VALIDATED_SUCCESS", "tesSUCCESS", "XRPL_SETTLED", 1, 1_500_000),
    ]
    assert events == [("v2-success", None)]


def test_c1_version_label_alone_does_not_make_a_v2_journal_current(tmp_path: Path) -> None:
    database = tmp_path / "relabelled.db"
    shutil.copyfile(FIXTURES / "x1b_v2_journal.fixture", database)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version = 3")
    before = snapshot(database)
    with pytest.raises(UnsupportedJournalSchema):
        PaymentJournal(database)
    assert snapshot(database) == before


def test_c1_new_journal_is_version_three_and_admits_the_new_state(tmp_path: Path) -> None:
    database = tmp_path / "new.db"
    PaymentJournal(database)
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'xrpl_payment_transactions'"
        ).fetchone()[0]
    assert "'VALIDATED_UNEXPECTED_DELIVERY'" in sql
    PaymentJournal(database)  # reopens


def test_fixture_hashes_match_the_recorded_provenance() -> None:
    readme = (FIXTURES / "README.md").read_text(encoding="utf-8")
    recorded = dict(re.findall(r"`([a-z0-9_]+\.fixture)`: `([0-9a-f]{64})`", readme))
    actual = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in FIXTURES.glob("*.fixture")
    }
    assert recorded == actual
    assert len(actual) == 4
