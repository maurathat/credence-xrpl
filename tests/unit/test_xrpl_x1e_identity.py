"""X1e N1/N2: pinned node identity for authoritative reads, and identity stability across lookup."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from x1a_rippled import NODE_KEY, FakeRippled, ledger_hash_for, standard_ledger
from x1a_world import (
    ISSUER,
    NETWORK_ID,
    VALIDATED_LEDGER,
    StaticLedgerSource,
    direct_policy_signature,
    policy_signer,
    researchbot_signature,
    signed_authorization,
    unsigned_payment,
)

from credence.core.primitives import Timestamp
from credence.xrpl_adapter.multisign import assemble_multisigned
from credence.xrpl_adapter.preparation import (
    TRLUSD_WIRE_CODE,
)
from credence.xrpl_adapter.rippled import (
    DiagnosticLookup,
    RippledTransport,
    RippledTransportConfig,
)
from credence.xrpl_adapter.types import (
    LookupDisposition,
    LookupResult,
)

PINNED = RippledTransportConfig(
    source_id="local-rippled",
    expected_network_id=NETWORK_ID,
    expected_build_version="3.4.0",
    expected_node_public_key=NODE_KEY,
)
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


# --- N1: every transport configuration requires a valid pinned node key ------------------------


@pytest.mark.parametrize("value", [None, "", "   ", "n9not-a-real-node-key"])
def test_n1_missing_or_malformed_node_pin_is_rejected_at_config_time(value: object) -> None:
    with pytest.raises(ValueError, match="expected_node_public_key"):
        RippledTransportConfig(
            source_id="local-rippled",
            expected_network_id=NETWORK_ID,
            expected_build_version="3.4.0",
            expected_node_public_key=value,  # type: ignore[arg-type]
        )


def test_n1_pinned_lookup_is_authoritative() -> None:
    rippled, tx_hash = node_with_payment()
    result = RippledTransport(rippled, PINNED).lookup(tx_hash)
    assert result.disposition is LookupDisposition.FOUND
    assert result.payment is not None and result.payment.validated


def test_n1_changed_node_key_is_refused() -> None:
    rippled, tx_hash = node_with_payment()
    rippled.node_public_key = "n9SomeOtherStandaloneNode"
    result = RippledTransport(rippled, PINNED).lookup(tx_hash)
    assert (result.disposition, result.detail) == (
        LookupDisposition.UNAVAILABLE,
        "NODE_IDENTITY_MISMATCH",
    )


def test_n1_diagnostic_lookup_carries_no_authoritative_evidence() -> None:
    rippled, tx_hash = node_with_payment()
    diagnostic = RippledTransport(rippled, PINNED).diagnostic_lookup(tx_hash)
    assert isinstance(diagnostic, DiagnosticLookup) and not isinstance(diagnostic, LookupResult)
    assert diagnostic.authoritative is False
    assert (diagnostic.disposition, diagnostic.validated, diagnostic.result) == (
        LookupDisposition.FOUND,
        True,
        "tesSUCCESS",
    )
    assert not hasattr(diagnostic, "payment") and not hasattr(diagnostic, "evidence")
    missing = RippledTransport(rippled, PINNED).diagnostic_lookup("AB" * 32)
    assert missing.disposition is LookupDisposition.NOT_FOUND


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


def test_n2_diagnostic_also_detects_identity_change() -> None:
    rippled, tx_hash = node_with_payment()
    change_on_second_identity_read(rippled, lambda state: state.update(pubkey_node="n9Other"))
    diagnostic = RippledTransport(rippled, PINNED).diagnostic_lookup(tx_hash)
    assert (diagnostic.disposition, diagnostic.detail) == (
        LookupDisposition.UNAVAILABLE,
        "NODE_IDENTITY_MISMATCH",
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
