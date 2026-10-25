"""X1e N1/N2: pinned node identity for authoritative reads, and identity stability across lookup."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from x1a_rippled import NODE_KEY, FakeRippled, ledger_hash_for, standard_ledger
from x1a_world import (
    ISSUER,
    LAST_LEDGER,
    NETWORK_ID,
    OWNER,
    PROVIDER_A,
    RULES,
    SEQUENCE,
    VALIDATED_LEDGER,
    StaticLedgerSource,
    direct_policy_signature,
    policy_signer,
    researchbot_signature,
    signed_authorization,
    unsigned_payment,
)

from credence.core.primitives import Timestamp
from credence.signers.policy_signer import PolicySignerRefused
from credence.xrpl_adapter.journal import PaymentJournal
from credence.xrpl_adapter.mock import DeterministicMockTransport
from credence.xrpl_adapter.multisign import assemble_multisigned
from credence.xrpl_adapter.preparation import (
    TRLUSD_WIRE_CODE,
    OfflinePaymentPreparer,
    XrplSignedPaymentInspector,
)
from credence.xrpl_adapter.rippled import (
    DiagnosticLookup,
    RippledTransport,
    RippledTransportConfig,
    TransportRefused,
)
from credence.xrpl_adapter.service import PaymentService
from credence.xrpl_adapter.types import (
    Asset,
    LookupDisposition,
    LookupResult,
    LookupUnavailable,
    PaymentRequest,
    SubmissionResponse,
    TransactionState,
)

PINNED = RippledTransportConfig(
    source_id="local-rippled",
    expected_network_id=NETWORK_ID,
    expected_build_version="3.4.0",
    expected_node_public_key=NODE_KEY,
)
UNPINNED = replace(PINNED, expected_node_public_key=None)
TRLUSD = {"currency": TRLUSD_WIRE_CODE, "issuer": ISSUER.account, "value": "1.5"}


def signed_payment() -> bytes:
    transaction = unsigned_payment()
    return assemble_multisigned(
        transaction, [researchbot_signature(transaction), direct_policy_signature(transaction)]
    )


def node_with_payment() -> tuple[FakeRippled, str]:
    rippled = standard_ledger()
    tx_hash = rippled.include(signed_payment(), ledger_index=VALIDATED_LEDGER - 1, delivered=TRLUSD)
    return rippled, tx_hash


# --- N1: authoritative reads require a pinned node key -----------------------------------------


def test_n1_unpinned_lookup_is_never_authoritative() -> None:
    rippled, tx_hash = node_with_payment()
    result = RippledTransport(rippled, UNPINNED).lookup(tx_hash)
    assert (result.disposition, result.detail, result.payment) == (
        LookupDisposition.UNAVAILABLE,
        "NODE_IDENTITY_NOT_PINNED",
        None,
    )
    assert not any(method == "tx" for method, _ in rippled.calls)


def test_n1_pinned_lookup_is_authoritative() -> None:
    rippled, tx_hash = node_with_payment()
    result = RippledTransport(rippled, PINNED).lookup(tx_hash)
    assert result.disposition is LookupDisposition.FOUND
    assert result.payment is not None and result.payment.validated


def test_n1_unpinned_observer_and_ledger_context_refuse() -> None:
    transport = RippledTransport(standard_ledger(), UNPINNED)
    with pytest.raises(LookupUnavailable, match="NODE_IDENTITY_NOT_PINNED"):
        transport.validated_ledger(OWNER.account)
    with pytest.raises(TransportRefused) as raised:
        transport.ledger_context(RULES)
    assert raised.value.code == "NODE_IDENTITY_NOT_PINNED"


def test_n1_policy_signer_cannot_use_an_unpinned_node(tmp_path: Path) -> None:
    transaction = unsigned_payment()
    signer = policy_signer(tmp_path, ledger_source=RippledTransport(standard_ledger(), UNPINNED))
    with pytest.raises(PolicySignerRefused) as raised:
        signer.sign_payment(
            signed_authorization(transaction), transaction, now=Timestamp("2026-10-24T18:00:00Z")
        )
    assert raised.value.code == "LEDGER_UNAVAILABLE"


def test_n1_changed_node_key_is_refused() -> None:
    rippled, tx_hash = node_with_payment()
    rippled.node_public_key = "n9SomeOtherStandaloneNode"
    result = RippledTransport(rippled, PINNED).lookup(tx_hash)
    assert (result.disposition, result.detail) == (
        LookupDisposition.UNAVAILABLE,
        "NODE_IDENTITY_MISMATCH",
    )


def test_n1_diagnostic_lookup_is_available_unpinned_but_carries_no_evidence() -> None:
    rippled, tx_hash = node_with_payment()
    diagnostic = RippledTransport(rippled, UNPINNED).diagnostic_lookup(tx_hash)
    assert isinstance(diagnostic, DiagnosticLookup) and not isinstance(diagnostic, LookupResult)
    assert diagnostic.authoritative is False
    assert (diagnostic.disposition, diagnostic.validated, diagnostic.result) == (
        LookupDisposition.FOUND,
        True,
        "tesSUCCESS",
    )
    assert not hasattr(diagnostic, "payment") and not hasattr(diagnostic, "evidence")
    missing = RippledTransport(rippled, UNPINNED).diagnostic_lookup("AB" * 32)
    assert missing.disposition is LookupDisposition.NOT_FOUND


def test_n1_unpinned_reconciliation_never_settles(tmp_path: Path) -> None:
    rippled = standard_ledger()
    pinned = RippledTransport(rippled, PINNED)
    unpinned = RippledTransport(rippled, UNPINNED)
    blob = signed_payment()
    offline = DeterministicMockTransport(ledger=pinned.validated_ledger(OWNER.account))
    database = tmp_path / "journal.sqlite3"
    service = PaymentService(
        journal=PaymentJournal(database),
        preparer=OfflinePaymentPreparer(maximum_fee_drops=100),
        inspector=XrplSignedPaymentInspector(),
        transport=offline,
        lookup=unpinned,
        ledger_observer=pinned,
    )
    service.prepare_and_persist(
        PaymentRequest(
            "operation-x1e",
            "authorization-x1e",
            OWNER.account,
            PROVIDER_A.account,
            Asset("tRLUSD", TRLUSD_WIRE_CODE, ISSUER.account),
            1_500_000,
            30,
            NETWORK_ID,
            LAST_LEDGER,
            VALIDATED_LEDGER,
            SEQUENCE,
        ),
        blob,
    )
    offline.queue_submission(SubmissionResponse(True, "tesSUCCESS", "queued offline"))
    service.submit("operation-x1e")
    rippled.include(blob, ledger_index=VALIDATED_LEDGER + 1, delivered=TRLUSD)
    rippled.advance(VALIDATED_LEDGER + 1)
    record = service.reconcile("operation-x1e")
    assert (record.state, record.last_provisional_status) == (
        TransactionState.UNKNOWN,
        "NODE_IDENTITY_NOT_PINNED",
    )
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM xrpl_settlement_outbox").fetchone()[0] == 0


# --- N2: identity stability across one lookup --------------------------------------------------


def change_on_second_identity_read(rippled: FakeRippled, change: Any) -> None:
    """The first server_state read is honest; from the second (the re-read) on, `change` applies."""
    real = rippled._server_state
    reads = {"count": 0}

    def fault(request: dict[str, Any]) -> dict[str, Any]:
        reads["count"] += 1
        result = real(request)
        if reads["count"] >= 2:
            change(result["state"])
        return result

    rippled.faults["server_state"] = fault


def regress(state: dict[str, Any]) -> None:
    """A self-consistent earlier validated ledger: index and hash both step back."""
    earlier = VALIDATED_LEDGER - 1
    state["validated_ledger"] = {
        **state["validated_ledger"],
        "seq": earlier,
        "hash": ledger_hash_for(earlier),
    }


@pytest.mark.parametrize(
    ("change", "detail"),
    [
        (lambda state: state.update(network_id=1), "NETWORK_MISMATCH"),
        (lambda state: state.update(pubkey_node="n9AnotherNode"), "NODE_IDENTITY_MISMATCH"),
        (lambda state: state.update(build_version="3.4.1"), "BUILD_VERSION_MISMATCH"),
        (
            lambda state: state.update(complete_ledgers=f"{VALIDATED_LEDGER}-{VALIDATED_LEDGER}"),
            "LEDGER_OUTSIDE_COMPLETE_HISTORY",
        ),
        (
            lambda state: state.update(complete_ledgers="empty"),
            "LEDGER_OUTSIDE_COMPLETE_HISTORY",
        ),
    ],
)
def test_n2_node_changing_between_reads_is_refused(change: Any, detail: str) -> None:
    rippled, tx_hash = node_with_payment()
    change_on_second_identity_read(rippled, change)
    result = RippledTransport(rippled, PINNED).lookup(tx_hash)
    assert (result.disposition, result.detail, result.payment) == (
        LookupDisposition.UNAVAILABLE,
        detail,
        None,
    )
    assert sum(1 for method, _ in rippled.calls if method == "server_state") == 2


def test_n2_unpinned_diagnostic_also_detects_identity_change() -> None:
    rippled, tx_hash = node_with_payment()
    change_on_second_identity_read(rippled, lambda state: state.update(pubkey_node="n9Other"))
    diagnostic = RippledTransport(rippled, UNPINNED).diagnostic_lookup(tx_hash)
    assert (diagnostic.disposition, diagnostic.detail) == (
        LookupDisposition.UNAVAILABLE,
        "IDENTITY_CHANGED_DURING_LOOKUP",
    )


def test_n2_validated_ledger_regression_is_refused() -> None:
    rippled, tx_hash = node_with_payment()
    change_on_second_identity_read(rippled, regress)
    result = RippledTransport(rippled, PINNED).lookup(tx_hash)
    assert (result.disposition, result.detail) == (
        LookupDisposition.UNAVAILABLE,
        "VALIDATED_LEDGER_REGRESSED",
    )


def test_n2_same_index_different_hash_is_refused() -> None:
    rippled, tx_hash = node_with_payment()
    replaced = "C3" * 32

    def swap(state: dict[str, Any]) -> None:
        state["validated_ledger"] = {**state["validated_ledger"], "hash": replaced}

    change_on_second_identity_read(rippled, swap)
    real_ledger = rippled._ledger

    def ledger(request: dict[str, Any]) -> dict[str, Any]:
        result = real_ledger(request)
        if (
            int(request["ledger_index"]) == VALIDATED_LEDGER
            and sum(1 for method, _ in rippled.calls if method == "server_state") >= 2
        ):
            result["ledger_hash"] = replaced
            result["ledger"] = {**result["ledger"], "ledger_hash": replaced}
        return result

    rippled.faults["ledger"] = ledger
    result = RippledTransport(rippled, PINNED).lookup(tx_hash)
    assert (result.disposition, result.detail) == (
        LookupDisposition.UNAVAILABLE,
        "IDENTITY_CHANGED_DURING_LOOKUP",
    )


def test_n2_stable_node_records_both_identity_reads() -> None:
    rippled, tx_hash = node_with_payment()
    result = RippledTransport(rippled, PINNED).lookup(tx_hash)
    assert result.payment is not None
    envelope = json.loads(result.payment.evidence)
    assert envelope["validated_identity"] == envelope["validated_identity_after"]
    assert envelope["validated_identity_after"]["node_public_key"] == NODE_KEY


def test_static_source_still_supported_for_policy_signer(tmp_path: Path) -> None:
    """The trusted source is any configured reader; a pinned transport or a test stand-in."""
    transaction = unsigned_payment()
    signature = policy_signer(tmp_path, ledger_source=StaticLedgerSource()).sign_payment(
        signed_authorization(transaction), transaction, now=Timestamp("2026-10-24T18:00:00Z")
    )
    assert signature.account
