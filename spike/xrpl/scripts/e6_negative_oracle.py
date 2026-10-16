"""Frozen prospective assertions for Checkpoint 1E-N E6 negatives."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

from e6_oracle import (
    BORROWER_MULTISIGN_PREFIX_HEX,
    COUNTERPARTY_SIGN_PREFIX_HEX,
    LOAN_TERM_FIELDS,
)
from harness import StateAssertion, predicate_assertion, state_assertion
from xrpl.core.binarycodec import encode_for_signing_counterparty
from xrpl.core.keypairs import is_valid_message

BrokerMode = Literal["valid", "missing", "invalid", "valid_for_x_only"]


@dataclass(frozen=True)
class NegativeCase:
    case_id: str
    borrower_signers: tuple[str, ...]
    broker_mode: BrokerMode
    principal: str
    broker_signed_principal: str | None
    expected_result: str
    expect_validated: bool
    phase: Literal["Investment", "Subscription"]


NEGATIVE_CASES = (
    NegativeCase(
        "N1", ("research_bot",), "valid", "1000", None, "tefBAD_QUORUM", False, "Investment"
    ),
    NegativeCase(
        "N2", ("credence_policy",), "valid", "1000", None, "tefBAD_QUORUM", False, "Investment"
    ),
    NegativeCase(
        "N3",
        ("research_bot", "credence_policy"),
        "missing",
        "1000",
        None,
        "temBAD_SIGNER",
        False,
        "Investment",
    ),
    NegativeCase(
        "N4",
        ("research_bot", "credence_policy"),
        "invalid",
        "1000",
        None,
        "local invalidTransaction",
        False,
        "Investment",
    ),
    NegativeCase(
        "N5",
        ("research_bot", "credence_policy"),
        "valid_for_x_only",
        "1001",
        "1000",
        "local invalidTransaction",
        False,
        "Investment",
    ),
    NegativeCase(
        "N6",
        ("research_bot", "credence_policy"),
        "valid",
        "1000",
        None,
        "tecTOO_SOON",
        True,
        "Subscription",
    ),
)

PROSPECTIVE_TIMING = {
    "StartDate": "applying view header closeTime read during LoanSet::doApply",
    "NextPaymentDueDate": "StartDate + PaymentInterval",
    "PaymentInterval": 60,
    "GracePeriod": 60,
    "forbidden_anchor": "transaction lookup date",
}

REQUIRED_LIVE_BINDINGS = frozenset(
    {
        "actors",
        "amendments",
        "assertion_spec_hash",
        "case_constructions",
        "evidence_destination",
        "git_commit",
        "main_fixture",
        "negative_oracle_hash",
        "network_id",
        "preflight_hash",
        "rippled_source_commit",
        "rippled_version",
        "runner_hash",
        "tests_hash",
        "timing_spec_hash",
        "wrong_phase_fixture",
    }
)


class NegativePreflightError(RuntimeError):
    """The negative design or live binding is incomplete."""


def normalize_unordered_strings(values: Iterable[str]) -> list[str]:
    """Normalize an unordered semantic collection as a deterministic JSON array."""

    normalized = list(values)
    if any(not isinstance(value, str) for value in normalized):
        raise TypeError("unordered signing-domain values must all be strings")
    return sorted(set(normalized))


def case_definitions() -> list[dict[str, Any]]:
    return [
        {
            "case_id": case.case_id,
            "borrower_signers": list(case.borrower_signers),
            "broker_mode": case.broker_mode,
            "principal": case.principal,
            "broker_signed_principal": case.broker_signed_principal,
            "expected_result": case.expected_result,
            "expect_validated": case.expect_validated,
            "phase": case.phase,
        }
        for case in NEGATIVE_CASES
    ]


def static_preflight_assertions() -> list[StateAssertion]:
    definitions = case_definitions()
    ids = [row["case_id"] for row in definitions]
    return [
        state_assertion("exact N1-N6 order", ids, ["N1", "N2", "N3", "N4", "N5", "N6"]),
        state_assertion(
            "exact expected results",
            {row["case_id"]: row["expected_result"] for row in definitions},
            {
                "N1": "tefBAD_QUORUM",
                "N2": "tefBAD_QUORUM",
                "N3": "temBAD_SIGNER",
                "N4": "local invalidTransaction",
                "N5": "local invalidTransaction",
                "N6": "tecTOO_SOON",
            },
        ),
        state_assertion(
            "only N6 expects validation",
            [row["case_id"] for row in definitions if row["expect_validated"]],
            ["N6"],
        ),
        state_assertion("N5 submitted principal", definitions[4]["principal"], "1001"),
        state_assertion(
            "N5 broker-signed principal", definitions[4]["broker_signed_principal"], "1000"
        ),
        state_assertion("N6 phase", definitions[5]["phase"], "Subscription"),
        state_assertion("prospective PaymentInterval", PROSPECTIVE_TIMING["PaymentInterval"], 60),
        state_assertion("prospective GracePeriod", PROSPECTIVE_TIMING["GracePeriod"], 60),
        predicate_assertion(
            "prospective StartDate excludes transaction lookup date",
            PROSPECTIVE_TIMING["forbidden_anchor"] not in PROSPECTIVE_TIMING["StartDate"],
            True,
            PROSPECTIVE_TIMING["forbidden_anchor"] not in PROSPECTIVE_TIMING["StartDate"],
        ),
    ]


def require_static_preflight() -> None:
    failed = [check for check in static_preflight_assertions() if not check.passed]
    if failed:
        raise NegativePreflightError(
            "; ".join(f"{check.name}: {check.actual!r}" for check in failed)
        )


def live_binding_assertions(bindings: dict[str, Any]) -> list[StateAssertion]:
    missing = sorted(name for name in REQUIRED_LIVE_BINDINGS if not bindings.get(name))
    constructions = bindings.get("case_constructions")
    constructions = constructions if isinstance(constructions, dict) else {}
    cases = sorted(constructions)
    no_existing_loan = all(
        isinstance(constructions.get(case_id), dict)
        and constructions[case_id].get("expected_loan_absent_before") is True
        for case_id in ("N1", "N2", "N3", "N4", "N5", "N6")
    )
    positive_absent = all("positive" not in str(value).lower() for value in constructions.values())
    expected_results = {case.case_id: case.expected_result for case in NEGATIVE_CASES}
    observed_results = {
        case_id: constructions.get(case_id, {}).get("expected_result")
        for case_id in ("N1", "N2", "N3", "N4", "N5", "N6")
    }
    authorization_complete = all(
        isinstance(constructions.get(case_id), dict)
        and constructions[case_id].get("authorization_assertions_pass") is True
        for case_id in ("N1", "N2", "N3", "N4", "N5", "N6")
    )
    return [
        state_assertion("required negative live bindings", missing, []),
        state_assertion(
            "exact live N1-N6 constructions", cases, ["N1", "N2", "N3", "N4", "N5", "N6"]
        ),
        predicate_assertion(
            "no negative depends on an existing Loan", no_existing_loan, True, no_existing_loan
        ),
        predicate_assertion(
            "no positive LoanSet construction", positive_absent, True, positive_absent
        ),
        state_assertion("live expected results", observed_results, expected_results),
        predicate_assertion(
            "all authorization constructions pass preflight",
            authorization_complete,
            True,
            authorization_complete,
        ),
    ]


def require_live_bindings(bindings: dict[str, Any]) -> None:
    failed = [check for check in live_binding_assertions(bindings) if not check.passed]
    if failed:
        raise NegativePreflightError(
            "; ".join(f"{check.name}: {check.actual!r}" for check in failed)
        )


def _signature_valid(payload_hex: Any, signature_hex: Any, public_key_hex: Any) -> bool:
    if not all(
        isinstance(item, str) and item for item in (payload_hex, signature_hex, public_key_hex)
    ):
        return False
    try:
        return is_valid_message(
            bytes.fromhex(payload_hex), bytes.fromhex(signature_hex), public_key_hex
        )
    except Exception:  # noqa: BLE001 - malformed public signatures fail closed
        return False


def negative_authorization_assertions(
    evidence: dict[str, Any],
    *,
    borrower_account: str,
    expected_borrower_signers: dict[str, str],
    broker_owner_account: str,
    broker_owner_public_key: str,
    expected_terms: dict[str, Any],
    broker_mode: BrokerMode,
) -> list[StateAssertion]:
    borrower = evidence.get("borrower_authorization") or {}
    broker = evidence.get("broker_counterparty_authorization") or {}
    rows = borrower.get("signers") or []
    observed_signers = {
        row.get("account"): row.get("public_key_hex") for row in rows if isinstance(row, dict)
    }
    checks = [
        state_assertion("borrower transaction Account", borrower.get("account"), borrower_account),
        state_assertion(
            "borrower top-level SigningPubKey", borrower.get("top_level_signing_public_key"), ""
        ),
        state_assertion(
            "borrower master signature absent",
            borrower.get("top_level_transaction_signature"),
            None,
        ),
        state_assertion("borrower signer identities", observed_signers, expected_borrower_signers),
        state_assertion(
            "borrower SMT-NUL domains",
            normalize_unordered_strings(
                row.get("signing_prefix_hex")
                for row in rows
                if isinstance(row.get("signing_prefix_hex"), str)
            ),
            [BORROWER_MULTISIGN_PREFIX_HEX],
        ),
        state_assertion("broker Counterparty", broker.get("counterparty"), broker_owner_account),
        state_assertion(
            "broker CPT-NUL domain", broker.get("signing_prefix_hex"), COUNTERPARTY_SIGN_PREFIX_HEX
        ),
        state_assertion(
            "exact submitted loan terms", evidence.get("bound_loan_terms"), expected_terms
        ),
    ]
    checks.extend(
        predicate_assertion(
            f"borrower signer {row.get('account')} signature valid",
            _signature_valid(
                row.get("signing_payload_hex"),
                row.get("txn_signature_hex"),
                row.get("public_key_hex"),
            ),
            True,
            _signature_valid(
                row.get("signing_payload_hex"),
                row.get("txn_signature_hex"),
                row.get("public_key_hex"),
            ),
        )
        for row in rows
        if isinstance(row, dict)
    )
    broker_body = broker.get("serialized_body_hex")
    checks.extend(
        state_assertion(
            f"borrower signer {row.get('account')} binds broker body",
            row.get("serialized_body_hex"),
            broker_body,
        )
        for row in rows
        if isinstance(row, dict)
    )
    broker_valid = _signature_valid(
        broker.get("signing_payload_hex"),
        broker.get("txn_signature_hex"),
        broker.get("public_key_hex"),
    )
    if broker_mode == "missing":
        checks.extend(
            [
                state_assertion(
                    "broker signature public key absent", broker.get("public_key_hex"), None
                ),
                state_assertion(
                    "broker transaction signature absent", broker.get("txn_signature_hex"), None
                ),
            ]
        )
    elif broker_mode in {"invalid", "valid_for_x_only"}:
        checks.extend(
            [
                state_assertion(
                    "broker signature public key",
                    broker.get("public_key_hex"),
                    broker_owner_public_key,
                ),
                predicate_assertion(
                    "broker signature present",
                    bool(broker.get("txn_signature_hex")),
                    True,
                    bool(broker.get("txn_signature_hex")),
                ),
                predicate_assertion(
                    "broker signature invalid for submitted body",
                    not broker_valid,
                    True,
                    not broker_valid,
                ),
            ]
        )
    else:
        checks.extend(
            [
                state_assertion(
                    "broker signature public key",
                    broker.get("public_key_hex"),
                    broker_owner_public_key,
                ),
                predicate_assertion("broker signature valid", broker_valid, True, broker_valid),
            ]
        )
    return checks


def n5_mutation_assertions(
    *,
    broker_body_x: dict[str, Any],
    submitted_body_y: dict[str, Any],
    broker_public_key: str,
    broker_signature: str,
) -> list[StateAssertion]:
    differing = sorted(
        key
        for key in set(broker_body_x) | set(submitted_body_y)
        if broker_body_x.get(key) != submitted_body_y.get(key)
    )
    payload_x = encode_for_signing_counterparty(broker_body_x)
    signature_valid_x = _signature_valid(payload_x, broker_signature, broker_public_key)
    payload_y = encode_for_signing_counterparty(submitted_body_y)
    signature_valid_y = _signature_valid(payload_y, broker_signature, broker_public_key)
    return [
        state_assertion("N5 only principal differs", differing, ["PrincipalRequested"]),
        state_assertion(
            "N5 broker body X principal", broker_body_x.get("PrincipalRequested"), "1000"
        ),
        state_assertion(
            "N5 submitted body Y principal", submitted_body_y.get("PrincipalRequested"), "1001"
        ),
        predicate_assertion(
            "N5 broker signature valid for X", signature_valid_x, True, signature_valid_x
        ),
        predicate_assertion(
            "N5 broker signature invalid for Y", not signature_valid_y, True, not signature_valid_y
        ),
    ]


def expected_terms(transaction: dict[str, Any]) -> dict[str, Any]:
    return {field: transaction.get(field) for field in LOAN_TERM_FIELDS}
