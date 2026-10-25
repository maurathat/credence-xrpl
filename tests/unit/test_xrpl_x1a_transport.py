"""X1a trusted local-rippled transport, exercised offline against a deterministic fake node."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from x1a_rippled import FakeRippled, ledger_hash_for, standard_ledger
from x1a_world import (
    AMOUNT,
    ASSET,
    ISSUER,
    LEDGER_HASH,
    NETWORK_ID,
    OWNER,
    POLICY,
    PROVIDER_A,
    RESEARCHBOT,
    RULES,
    SEQUENCE,
    TICKET,
    VALIDATED_LEDGER,
    direct_policy_signature,
    expected_payment,
    researchbot_signature,
    unsigned_payment,
)

from credence.xrpl_adapter.journal import PaymentJournal
from credence.xrpl_adapter.mock import DeterministicMockTransport
from credence.xrpl_adapter.multisign import (
    LSF_DISABLE_MASTER,
    assemble_multisigned,
    verify_multisigned_payment,
)
from credence.xrpl_adapter.preparation import (
    TRLUSD_WIRE_CODE,
    OfflinePaymentPreparer,
    XrplSignedPaymentInspector,
    transaction_hash,
)
from credence.xrpl_adapter.rippled import (
    EVIDENCE_SCHEMA,
    LoopbackJsonRpcClient,
    RippledTransport,
    RippledTransportConfig,
    RpcTimeout,
    RpcUnavailable,
    SubmissionNotAuthorized,
    TransportRefused,
    parse_complete_ledgers,
)
from credence.xrpl_adapter.service import PaymentService
from credence.xrpl_adapter.types import (
    LedgerRange,
    LookupDisposition,
    LookupUnavailable,
    SubmissionTimeout,
    SubmissionTransportError,
)

CONFIG = RippledTransportConfig(
    source_id="local-rippled-3.4.0", expected_network_id=NETWORK_ID, expected_build_version="3.4.0"
)


def transport(rippled: FakeRippled, **changes: Any) -> RippledTransport:
    config = RippledTransportConfig(**{**CONFIG.__dict__, **changes})
    return RippledTransport(rippled, config)


def signed_payment() -> bytes:
    transaction = unsigned_payment()
    return assemble_multisigned(
        transaction, [researchbot_signature(transaction), direct_policy_signature(transaction)]
    )


def delivered(amount_value: str = "1.5", issuer: str | None = None) -> dict[str, str]:
    return {"currency": TRLUSD_WIRE_CODE, "issuer": issuer or ISSUER.account, "value": amount_value}


def refused(code: str, call: Any) -> None:
    with pytest.raises(TransportRefused) as raised:
        call()
    assert raised.value.code == code


# --- validated ledger identity and network ---------------------------------------------------


def test_validated_ledger_identity_is_confirmed_and_captured() -> None:
    rippled = standard_ledger()
    identity = transport(rippled).validated_ledger_identity()
    assert (identity.ledger_index, identity.ledger_hash) == (VALIDATED_LEDGER, LEDGER_HASH)
    assert identity.network_id == NETWORK_ID and identity.build_version == "3.4.0"
    assert (identity.base_fee_drops, identity.reserve_base_drops) == (10, 1_000_000)
    assert identity.complete_history_ranges == (LedgerRange(1, VALIDATED_LEDGER),)
    assert [item.method for item in identity.raw] == ["server_state", "ledger"]
    assert json.loads(identity.raw[0].response)["result"]["state"]["network_id"] == NETWORK_ID


def test_every_request_is_api_v2_and_carries_no_secret() -> None:
    rippled = standard_ledger()
    transport(rippled).ledger_context(RULES)
    assert rippled.calls
    for _, params in rippled.calls:
        assert params["api_version"] == 2
        assert not {"secret", "seed", "seed_hex", "passphrase", "private_key"} & set(params)


def test_wrong_network_or_build_is_refused() -> None:
    refused("NETWORK_MISMATCH", transport(standard_ledger(network_id=1)).validated_ledger_identity)
    refused(
        "BUILD_VERSION_MISMATCH",
        transport(standard_ledger(build_version="3.5.0-dev")).validated_ledger_identity,
    )


def test_unconfirmed_ledger_identity_is_refused() -> None:
    rippled = standard_ledger()
    rippled.faults["ledger"] = lambda request: {
        "ledger_hash": "B2" * 32,
        "ledger_index": VALIDATED_LEDGER,
        "validated": True,
    }
    refused("LEDGER_IDENTITY_MISMATCH", transport(rippled).validated_ledger_identity)
    rippled.faults["ledger"] = lambda request: {
        "ledger_hash": LEDGER_HASH,
        "ledger_index": VALIDATED_LEDGER,
        "validated": False,
    }
    refused("LEDGER_NOT_VALIDATED", transport(rippled).validated_ledger_identity)
    rippled.faults["ledger"] = lambda request: None
    rippled.faults["server_state"] = lambda request: {
        "state": {"network_id": NETWORK_ID, "build_version": "3.4.0"}
    }
    refused("NO_VALIDATED_LEDGER", transport(rippled).validated_ledger_identity)


def test_complete_ledgers_parsing() -> None:
    assert parse_complete_ledgers("2-40,45-60") == (LedgerRange(2, 40), LedgerRange(45, 60))
    assert parse_complete_ledgers("7") == (LedgerRange(7, 7),)
    assert parse_complete_ledgers("empty") == ()
    for bad in ("1-x", "10-2", "0-5", "1-2-3"):
        with pytest.raises(TransportRefused):
            parse_complete_ledgers(bad)


# --- account state -----------------------------------------------------------------------------


def test_ledger_context_feeds_multisign_verification() -> None:
    rippled = standard_ledger()
    context = transport(rippled).ledger_context(RULES)
    assert context.account.sequence == SEQUENCE and context.account.tickets == (TICKET,)
    assert context.account.signer_list is not None and context.account.signer_list.quorum == 2
    assert not context.signers[RESEARCHBOT.account].exists
    transaction = unsigned_payment()
    verify_multisigned_payment(
        signed_payment(), policy=RULES, expected=expected_payment(transaction), ledger=context
    )


def test_account_flags_regular_key_and_paged_tickets() -> None:
    rippled = standard_ledger()
    rippled.accounts[OWNER.account].update(
        {"Flags": LSF_DISABLE_MASTER, "RegularKey": PROVIDER_A.account}
    )
    rippled.tickets[OWNER.account] = [9, 3, 12, 5, 7]
    rippled.ticket_page_size = 2
    t = transport(rippled)
    snapshot = t.account_snapshot(OWNER.account, t.validated_ledger_identity())
    assert snapshot.master_key_disabled and snapshot.regular_key == PROVIDER_A.account
    assert snapshot.tickets == (3, 5, 7, 9, 12)
    assert sum(1 for method, _ in rippled.calls if method == "account_objects") == 3


def test_account_read_from_another_ledger_is_refused() -> None:
    rippled = standard_ledger()
    t = transport(rippled)
    identity = t.validated_ledger_identity()
    rippled.faults["account_info"] = lambda request: {
        "account_data": {"Account": OWNER.account, "Sequence": 1, "Balance": "1"},
        "ledger_hash": "B2" * 32,
        "ledger_index": VALIDATED_LEDGER,
        "validated": True,
    }
    refused("LEDGER_STATE_INCONSISTENT", lambda: t.account_snapshot(OWNER.account, identity))
    rippled.faults["account_info"] = lambda request: {
        "account_data": {"Account": OWNER.account, "Sequence": 1, "Balance": "1"},
        "ledger_hash": LEDGER_HASH,
        "ledger_index": VALIDATED_LEDGER,
        "validated": False,
    }
    refused("LEDGER_NOT_VALIDATED", lambda: t.account_snapshot(OWNER.account, identity))


def test_more_than_one_signer_list_is_refused() -> None:
    rippled = standard_ledger()
    rippled.signer_lists[OWNER.account] = rippled.signer_lists[OWNER.account] * 2
    t = transport(rippled)
    refused(
        "MALFORMED_RESPONSE",
        lambda: t.account_snapshot(OWNER.account, t.validated_ledger_identity()),
    )


def test_unfunded_account_is_reported_as_absent() -> None:
    t = transport(standard_ledger())
    snapshot = t.account_snapshot(POLICY.account, t.validated_ledger_identity())
    assert not snapshot.exists and snapshot.validated and snapshot.ledger_hash == LEDGER_HASH


def test_trust_line_snapshot() -> None:
    rippled = standard_ledger()
    rippled.lines[OWNER.account] = [
        {
            "account": ISSUER.account,
            "currency": TRLUSD_WIRE_CODE,
            "balance": "25.000001",
            "limit": "1000",
            "limit_peer": "0",
            "no_ripple": True,
        }
    ]
    t = transport(rippled)
    identity = t.validated_ledger_identity()
    line = t.trust_line(OWNER.account, ISSUER.account, TRLUSD_WIRE_CODE, identity)
    assert line.exists and line.balance_micro_units == 25_000_001
    assert line.limit_micro_units == 1_000_000_000 and line.no_ripple
    missing = t.trust_line(PROVIDER_A.account, ISSUER.account, TRLUSD_WIRE_CODE, identity)
    assert not missing.exists


# --- X0t ValidatedLedgerObserver -------------------------------------------------------------


def test_validated_ledger_observation_reports_history_ranges() -> None:
    rippled = standard_ledger(complete_ledgers="1-50,60-100")
    observation = transport(rippled).validated_ledger(OWNER.account)
    assert observation.has_trusted_identity()
    assert observation.source_id == CONFIG.source_id
    assert observation.complete_history_ranges == (LedgerRange(1, 50), LedgerRange(60, 100))
    assert observation.account_state_matches(
        account=OWNER.account, sequence=SEQUENCE, ticket_sequence=None
    )
    # The gap means non-inclusion over 45..99 cannot be proven.
    assert not observation.proves_non_inclusion(earliest=45, last=99)
    assert observation.proves_non_inclusion(earliest=61, last=99)


def test_missing_history_is_not_complete() -> None:
    observation = transport(standard_ledger(complete_ledgers="empty")).validated_ledger(
        OWNER.account
    )
    assert not observation.history_complete
    assert not observation.proves_non_inclusion(earliest=1, last=50)


def test_observer_failures_are_unavailable() -> None:
    rippled = standard_ledger(network_id=1)
    with pytest.raises(LookupUnavailable):
        transport(rippled).validated_ledger(OWNER.account)

    def timeout(request: dict[str, Any]) -> dict[str, Any]:
        raise RpcTimeout("server_state")

    rippled = standard_ledger()
    rippled.faults["server_state"] = timeout
    with pytest.raises(LookupUnavailable):
        transport(rippled).validated_ledger(OWNER.account)


# --- X0t HashLookup and evidence -------------------------------------------------------------


def test_lookup_not_found() -> None:
    result = transport(standard_ledger()).lookup("AB" * 32)
    assert result.disposition is LookupDisposition.NOT_FOUND


def test_lookup_unvalidated_transaction_is_provisional() -> None:
    rippled = standard_ledger()
    blob = signed_payment()
    tx_hash = rippled.include(blob, ledger_index=None, validated=False)
    result = transport(rippled).lookup(tx_hash)
    assert result.disposition is LookupDisposition.FOUND
    assert result.payment is not None and not result.payment.validated


def test_lookup_validated_payment_builds_x0s_evidence() -> None:
    rippled = standard_ledger()
    blob = signed_payment()
    tx_hash = rippled.include(blob, ledger_index=VALIDATED_LEDGER - 1, delivered=delivered())
    result = transport(rippled).lookup(tx_hash)
    assert result.disposition is LookupDisposition.FOUND and result.payment is not None
    payment = result.payment
    assert payment.validated and payment.result == "tesSUCCESS"
    assert payment.delivered_amount_micro_units == AMOUNT
    assert payment.ledger_hash == ledger_hash_for(VALIDATED_LEDGER - 1)
    envelope = json.loads(payment.evidence)
    assert envelope["schema"] == EVIDENCE_SCHEMA
    assert envelope["signed_transaction"] == blob.hex().upper()
    assert envelope["transaction_hash"] == tx_hash == transaction_hash(blob)
    assert [item["method"] for item in envelope["raw"]] == [
        "server_state",
        "ledger",
        "tx",
        "tx",
        "ledger",
    ]
    assert envelope["validated_identity"]["network_id"] == NETWORK_ID
    assert envelope["delivered_amount_observations"]["binary"]["source"] == "DeliveredAmount"
    assert envelope["delivered_amount_observations"]["json"] == delivered()
    assert payment.asset == ASSET


@pytest.mark.parametrize(
    ("method", "override", "detail"),
    [
        (
            "tx",
            lambda r: {"hash": "CD" * 32, "tx_blob": "", "validated": True},
            "MALFORMED_RESPONSE",
        ),
        (
            "ledger",
            lambda r: (
                {"ledger_hash": "B2" * 32, "ledger_index": 99, "validated": True}
                if r["ledger_index"] == 99
                else None
            ),
            "LEDGER_HASH_MISMATCH",
        ),
        (
            "ledger",
            lambda r: (
                {"ledger_hash": ledger_hash_for(99), "ledger_index": 99, "validated": False}
                if r["ledger_index"] == 99
                else None
            ),
            "LEDGER_NOT_VALIDATED",
        ),
    ],
)
def test_incorrect_validated_evidence_is_unavailable(
    method: str, override: Any, detail: str
) -> None:
    rippled = standard_ledger()
    tx_hash = rippled.include(signed_payment(), ledger_index=99, delivered=delivered())
    rippled.faults[method] = override
    result = transport(rippled).lookup(tx_hash)
    assert result.disposition is LookupDisposition.UNAVAILABLE
    assert result.detail == detail


def test_lookup_refuses_a_blob_that_does_not_hash_to_the_request() -> None:
    rippled = standard_ledger()
    blob = signed_payment()
    tx_hash = rippled.include(blob, ledger_index=99, delivered=delivered())
    other = unsigned_payment(fee_drops=31)
    swapped = assemble_multisigned(
        other, [researchbot_signature(other), direct_policy_signature(other)]
    )
    rippled.transactions[tx_hash].blob = swapped
    result = transport(rippled).lookup(tx_hash)
    assert (result.disposition, result.detail) == (
        LookupDisposition.UNAVAILABLE,
        "EVIDENCE_HASH_MISMATCH",
    )


def test_binary_and_json_lookups_must_agree() -> None:
    rippled = standard_ledger()
    tx_hash = rippled.include(signed_payment(), ledger_index=99, delivered=delivered())
    real = rippled._tx

    def disagreeing(request: dict[str, Any]) -> dict[str, Any] | None:
        if request["binary"]:
            return None
        result = real(request)
        result["meta"]["TransactionResult"] = "tecPATH_DRY"
        return result

    rippled.faults["tx"] = disagreeing
    result = transport(rippled).lookup(tx_hash)
    assert (result.disposition, result.detail) == (
        LookupDisposition.UNAVAILABLE,
        "EVIDENCE_INCONSISTENT",
    )


def test_lookup_timeout_and_errors_are_unavailable() -> None:
    rippled = standard_ledger()

    def timeout(request: dict[str, Any]) -> dict[str, Any]:
        raise RpcTimeout("tx")

    rippled.faults["tx"] = timeout
    assert transport(rippled).lookup("AB" * 32).disposition is LookupDisposition.UNAVAILABLE
    rippled.faults["tx"] = lambda request: {"status": "error", "error": "noNetwork"}
    result = transport(rippled).lookup("AB" * 32)
    assert (result.disposition, result.detail) == (
        LookupDisposition.UNAVAILABLE,
        "RPC_ERROR:noNetwork",
    )


# --- submission ------------------------------------------------------------------------------


def test_submission_is_refused_unless_enabled() -> None:
    rippled = standard_ledger()
    blob = signed_payment()
    with pytest.raises(SubmissionNotAuthorized):
        transport(rippled).submit(blob, transaction_hash(blob))
    assert not any(method == "submit" for method, _ in rippled.calls)


def test_x0t_payment_service_refuses_the_live_transport(tmp_path: Path) -> None:
    rippled_transport = transport(standard_ledger(), submission_enabled=True)
    with pytest.raises(ValueError, match="offline"):
        PaymentService(
            journal=PaymentJournal(tmp_path / "journal.sqlite3"),
            preparer=OfflinePaymentPreparer(maximum_fee_drops=100),
            inspector=XrplSignedPaymentInspector(),
            transport=rippled_transport,
            lookup=rippled_transport,
            ledger_observer=rippled_transport,
        )
    # Lookup and observation alone may be wired to the trusted node; submission stays offline.
    offline = DeterministicMockTransport(ledger=rippled_transport.validated_ledger(OWNER.account))
    PaymentService(
        journal=PaymentJournal(tmp_path / "journal.sqlite3"),
        preparer=OfflinePaymentPreparer(maximum_fee_drops=100),
        inspector=XrplSignedPaymentInspector(),
        transport=offline,
        lookup=rippled_transport,
        ledger_observer=rippled_transport,
    )


def test_enabled_submission_maps_timeouts_and_duplicates() -> None:
    blob = signed_payment()
    tx_hash = transaction_hash(blob)
    rippled = standard_ledger()
    enabled = transport(rippled, submission_enabled=True)
    rippled.submit_results.append(RpcTimeout("submit"))
    with pytest.raises(SubmissionTimeout):
        enabled.submit(blob, tx_hash)
    rippled.submit_results.append(RpcUnavailable("submit"))
    with pytest.raises(SubmissionTransportError):
        enabled.submit(blob, tx_hash)
    rippled.submit_results.append(
        {"engine_result": "tesSUCCESS", "accepted": True, "tx_json": {"hash": tx_hash}}
    )
    first = enabled.submit(blob, tx_hash)
    assert first.accepted and first.engine_result == "tesSUCCESS"
    rippled.submit_results.append(
        {"engine_result": "tefPAST_SEQ", "accepted": False, "tx_json": {"hash": tx_hash}}
    )
    duplicate = enabled.submit(blob, tx_hash)
    assert not duplicate.accepted and duplicate.engine_result == "tefPAST_SEQ"
    submitted = [params["tx_blob"] for method, params in rippled.calls if method == "submit"]
    assert set(submitted) == {blob.hex().upper()}


def test_submission_hash_disagreement_is_a_transport_error() -> None:
    blob = signed_payment()
    rippled = standard_ledger()
    enabled = transport(rippled, submission_enabled=True)
    with pytest.raises(SubmissionTransportError):
        enabled.submit(blob, "AB" * 32)
    rippled.submit_results.append(
        {"engine_result": "tesSUCCESS", "accepted": True, "tx_json": {"hash": "CD" * 32}}
    )
    with pytest.raises(SubmissionTransportError):
        enabled.submit(blob, transaction_hash(blob))


# --- loopback client -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.5:5005",
        "https://s.altnet.rippletest.net:51234",
        "ws://127.0.0.1:6006",
        "http://user:pass@127.0.0.1:5005",
        "http://127.0.0.1.example.com:5005",
    ],
)
def test_client_accepts_only_loopback_http(url: str) -> None:
    with pytest.raises(ValueError):
        LoopbackJsonRpcClient(url)


def test_client_and_transport_refuse_secret_parameters() -> None:
    client = LoopbackJsonRpcClient("http://127.0.0.1:5005")
    with pytest.raises(ValueError):
        client.call("submit", {"tx_json": {}, "secret": "x"})
    with pytest.raises(ValueError):
        transport(standard_ledger())._call("sign", {"tx_json": {}, "seed": "x"})


def test_transport_requires_a_source_identity() -> None:
    with pytest.raises(ValueError):
        RippledTransport(
            standard_ledger(),
            RippledTransportConfig(
                source_id=" ", expected_network_id=NETWORK_ID, expected_build_version="3.4.0"
            ),
        )
