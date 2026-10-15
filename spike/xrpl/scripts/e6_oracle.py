"""Frozen semantic and signing oracle for the Checkpoint 1E E6 design."""

from __future__ import annotations

import hashlib
import struct
from typing import Any

from harness import StateAssertion, predicate_assertion, state_assertion
from xrpl.core.binarycodec import (
    encode,
    encode_for_multisigning,
    encode_for_signing_counterparty,
)
from xrpl.core.keypairs import is_valid_message

BORROWER_MULTISIGN_PREFIX_HEX = "534D5400"  # SMT\0
COUNTERPARTY_SIGN_PREFIX_HEX = "43505400"  # CPT\0
LOAN_NAMESPACE = ord("L")

LOAN_TERM_FIELDS = (
    "LoanBrokerID",
    "Counterparty",
    "PrincipalRequested",
    "LoanOriginationFee",
    "LoanServiceFee",
    "LatePaymentFee",
    "ClosePaymentFee",
    "OverpaymentFee",
    "InterestRate",
    "LateInterestRate",
    "CloseInterestRate",
    "OverpaymentInterestRate",
    "PaymentTotal",
    "PaymentInterval",
    "GracePeriod",
)

POSITIVE_ORACLE_REQUIRED_FIELDS = (
    "Account",
    "VaultID",
    *LOAN_TERM_FIELDS,
)

REQUIRED_LIVE_BINDINGS = frozenset(
    {
        "amendments",
        "assertion_spec_hash",
        "borrower_account",
        "broker_owner_account",
        "credence_policy_account",
        "evidence_destination",
        "exact_loan_set_body",
        "expected_loan_index",
        "git_commit",
        "loan_broker_id",
        "network_id",
        "pre_loan_sequence",
        "positive_oracle_terms",
        "research_bot_account",
        "rippled_source_commit",
        "rippled_version",
        "runner_hash",
        "tests_hash",
        "vault_id",
    }
)


class E6BindingError(RuntimeError):
    """A live E6 path is missing one or more frozen provenance bindings."""


class E6ProjectionError(RuntimeError):
    """The runner-to-oracle terms projection is absent, incomplete, or inconsistent."""


def sha512_half(data: bytes) -> str:
    """Return the uppercase SHA-512Half digest used by XRPL indexes."""

    return hashlib.sha512(data).digest()[:32].hex().upper()


def loan_index(loan_broker_id: str, loan_sequence: int) -> str:
    """Derive keylet::loan(LoanBrokerID, LoanSequence) from pinned source."""

    if len(loan_broker_id) != 64:
        raise ValueError("LoanBrokerID must be 32 bytes of hexadecimal")
    if not 0 <= loan_sequence <= 0xFFFFFFFF:
        raise ValueError("LoanSequence must fit UINT32")
    try:
        broker_bytes = bytes.fromhex(loan_broker_id)
    except ValueError as exc:
        raise ValueError("LoanBrokerID must be hexadecimal") from exc
    serialized = struct.pack(">H", LOAN_NAMESPACE) + broker_bytes + struct.pack(">I", loan_sequence)
    return sha512_half(serialized)


def positive_oracle_projection_assertions(
    projection: dict[str, Any],
    *,
    intended_borrower: str,
) -> list[StateAssertion]:
    """Require every field consumed by the frozen positive semantic oracle."""

    missing = sorted(
        field_name
        for field_name in POSITIVE_ORACLE_REQUIRED_FIELDS
        if field_name not in projection or projection[field_name] is None
    )
    return [
        state_assertion("positive oracle projection required fields", missing, []),
        state_assertion(
            "positive oracle projection Account",
            projection.get("Account"),
            intended_borrower,
        ),
    ]


def require_positive_oracle_projection(
    projection: dict[str, Any],
    *,
    intended_borrower: str,
) -> None:
    """Fail before signing or submission if the positive oracle input is incomplete."""

    checks = positive_oracle_projection_assertions(
        projection,
        intended_borrower=intended_borrower,
    )
    if not all(check.passed for check in checks):
        failures = "; ".join(
            f"{check.name}: actual={check.actual!r}, expected={check.expected!r}"
            for check in checks
            if not check.passed
        )
        raise E6ProjectionError(failures)


def project_positive_oracle_terms(
    finalized_envelope: dict[str, Any],
    *,
    vault_id: str,
    intended_borrower: str,
) -> dict[str, Any]:
    """Build and validate the exact runner input consumed by the positive oracle."""

    projection = {
        field_name: finalized_envelope.get(field_name)
        for field_name in POSITIVE_ORACLE_REQUIRED_FIELDS
        if field_name != "VaultID"
    }
    projection["VaultID"] = vault_id
    require_positive_oracle_projection(
        projection,
        intended_borrower=intended_borrower,
    )
    return projection


def _signing_body(transaction: dict[str, Any]) -> dict[str, Any]:
    """Return a reviewable JSON projection of the fields included when signing."""

    excluded = {"CounterpartySignature", "Signers", "SponsorSignature", "TxnSignature"}
    return {key: value for key, value in transaction.items() if key not in excluded}


def _sha512_half_hex(hex_value: str) -> str:
    return sha512_half(bytes.fromhex(hex_value))


def _signature_valid(payload_hex: Any, signature_hex: Any, public_key_hex: Any) -> bool:
    if not all(
        isinstance(item, str) and item for item in (payload_hex, signature_hex, public_key_hex)
    ):
        return False
    try:
        return is_valid_message(
            bytes.fromhex(payload_hex),
            bytes.fromhex(signature_hex),
            public_key_hex,
        )
    except Exception:  # noqa: BLE001 - malformed public signatures must fail closed
        return False


def public_signing_evidence(transaction: dict[str, Any], transaction_hash: str) -> dict[str, Any]:
    """Capture both public authorization domains and their reproducible payloads."""

    counterparty = transaction.get("CounterpartySignature")
    counterparty = counterparty if isinstance(counterparty, dict) else {}
    counterparty_payload = encode_for_signing_counterparty(transaction)
    counterparty_body = counterparty_payload[len(COUNTERPARTY_SIGN_PREFIX_HEX) :]

    borrower_signers: list[dict[str, Any]] = []
    for wrapper in transaction.get("Signers", []):
        if not isinstance(wrapper, dict) or not isinstance(wrapper.get("Signer"), dict):
            continue
        signer = wrapper["Signer"]
        account = signer.get("Account")
        payload = encode_for_multisigning(transaction, account) if isinstance(account, str) else ""
        body_without_signer = (
            payload[len(BORROWER_MULTISIGN_PREFIX_HEX) : -40] if len(payload) >= 48 else ""
        )
        borrower_signers.append(
            {
                "account": account,
                "public_key_hex": signer.get("SigningPubKey"),
                "txn_signature_hex": signer.get("TxnSignature"),
                "signing_prefix_hex": BORROWER_MULTISIGN_PREFIX_HEX,
                "signing_payload_hex": payload,
                "signing_payload_sha512_half": _sha512_half_hex(payload) if payload else None,
                "serialized_body_hex": body_without_signer,
            }
        )

    return {
        "transaction_hash": transaction_hash,
        "signed_transaction_blob": encode(transaction),
        "exact_loan_set_body": _signing_body(transaction),
        "bound_loan_terms": {field: transaction.get(field) for field in LOAN_TERM_FIELDS},
        "borrower_authorization": {
            "account": transaction.get("Account"),
            "top_level_signing_public_key": transaction.get("SigningPubKey"),
            "top_level_transaction_signature": transaction.get("TxnSignature"),
            "signers": borrower_signers,
        },
        "broker_counterparty_authorization": {
            "counterparty": transaction.get("Counterparty"),
            "public_key_hex": counterparty.get("SigningPubKey"),
            "txn_signature_hex": counterparty.get("TxnSignature"),
            "counterparty_signers": counterparty.get("Signers"),
            "signing_prefix_hex": COUNTERPARTY_SIGN_PREFIX_HEX,
            "signing_payload_hex": counterparty_payload,
            "signing_payload_sha512_half": _sha512_half_hex(counterparty_payload),
            "serialized_body_hex": counterparty_body,
        },
    }


def authorization_assertions(
    evidence: dict[str, Any],
    *,
    borrower_account: str,
    expected_borrower_signers: dict[str, str],
    broker_owner_account: str,
    broker_owner_public_key: str,
    expected_terms: dict[str, Any],
) -> list[StateAssertion]:
    """Require exact, independently verifiable borrower and broker authorization."""

    borrower = evidence.get("borrower_authorization") or {}
    broker = evidence.get("broker_counterparty_authorization") or {}
    rows = borrower.get("signers") or []
    observed = {
        row.get("account"): row.get("public_key_hex") for row in rows if isinstance(row, dict)
    }
    checks = [
        state_assertion("borrower transaction Account", borrower.get("account"), borrower_account),
        state_assertion(
            "borrower multisign SigningPubKey", borrower.get("top_level_signing_public_key"), ""
        ),
        state_assertion(
            "borrower top-level TxnSignature absent",
            borrower.get("top_level_transaction_signature"),
            None,
        ),
        state_assertion("borrower signer identities and keys", observed, expected_borrower_signers),
        state_assertion("broker Counterparty", broker.get("counterparty"), broker_owner_account),
        state_assertion(
            "broker counterparty public key", broker.get("public_key_hex"), broker_owner_public_key
        ),
        state_assertion(
            "broker counterparty multisign absent", broker.get("counterparty_signers"), None
        ),
        state_assertion(
            "borrower signing prefix",
            {row.get("signing_prefix_hex") for row in rows if isinstance(row, dict)},
            {BORROWER_MULTISIGN_PREFIX_HEX},
        ),
        state_assertion(
            "broker signing prefix", broker.get("signing_prefix_hex"), COUNTERPARTY_SIGN_PREFIX_HEX
        ),
        predicate_assertion(
            "authorization domains use distinct prefixes",
            BORROWER_MULTISIGN_PREFIX_HEX != COUNTERPARTY_SIGN_PREFIX_HEX,
            True,
            BORROWER_MULTISIGN_PREFIX_HEX != COUNTERPARTY_SIGN_PREFIX_HEX,
        ),
        state_assertion(
            "exact signed loan terms", evidence.get("bound_loan_terms"), expected_terms
        ),
    ]
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
    broker_valid = _signature_valid(
        broker.get("signing_payload_hex"),
        broker.get("txn_signature_hex"),
        broker.get("public_key_hex"),
    )
    checks.append(
        predicate_assertion("broker counterparty signature valid", broker_valid, True, broker_valid)
    )
    return checks


def positive_post_state_assertions(
    *,
    validated_result: str | None,
    before: dict[str, Any],
    after: dict[str, Any],
    terms: dict[str, Any],
    expected_start_date: int,
) -> list[StateAssertion]:
    """Assert LoanSet's exact cash-basis semantic transition without object counts."""

    before_broker = before.get("loan_broker") or {}
    after_broker = after.get("loan_broker") or {}
    before_vault = before.get("vault") or {}
    after_vault = after.get("vault") or {}
    before_borrower = before.get("borrower_account") or {}
    after_borrower = after.get("borrower_account") or {}
    loan = after.get("loan") or {}

    principal = int(terms["PrincipalRequested"])
    origination_fee = int(terms.get("LoanOriginationFee") or 0)
    payment_interval = int(terms.get("PaymentInterval") or 60)
    payment_total = int(terms.get("PaymentTotal") or 1)
    loan_sequence = int(before_broker["LoanSequence"])
    expected_index = loan_index(str(terms["LoanBrokerID"]), loan_sequence)

    copied_term_fields = {
        "LoanOriginationFee": "0",
        "LoanServiceFee": "0",
        "LatePaymentFee": "0",
        "ClosePaymentFee": "0",
        "OverpaymentFee": 0,
        "InterestRate": 0,
        "LateInterestRate": 0,
        "CloseInterestRate": 0,
        "OverpaymentInterestRate": 0,
        "GracePeriod": int(terms.get("GracePeriod") or 60),
    }

    checks = [
        state_assertion("validated LoanSet result", validated_result, "tesSUCCESS"),
        state_assertion("Loan absent before", before.get("loan"), None),
        state_assertion("Loan LedgerEntryType", loan.get("LedgerEntryType"), "Loan"),
        state_assertion("Loan index", loan.get("index"), expected_index),
        state_assertion("Loan LoanBrokerID", loan.get("LoanBrokerID"), terms["LoanBrokerID"]),
        state_assertion("Loan LoanSequence", loan.get("LoanSequence"), loan_sequence),
        state_assertion("Loan Borrower", loan.get("Borrower"), terms["Account"]),
        state_assertion("Loan principal", loan.get("PrincipalOutstanding"), str(principal)),
        state_assertion("Loan StartDate", loan.get("StartDate"), expected_start_date),
        state_assertion("Loan PaymentInterval", loan.get("PaymentInterval"), payment_interval),
        state_assertion("Loan PaymentRemaining", loan.get("PaymentRemaining"), payment_total),
        state_assertion("Loan PreviousPaymentDueDate", loan.get("PreviousPaymentDueDate", 0), 0),
        state_assertion(
            "Loan NextPaymentDueDate",
            loan.get("NextPaymentDueDate"),
            expected_start_date + payment_interval,
        ),
        state_assertion("Loan PeriodicPayment", loan.get("PeriodicPayment"), str(principal)),
        state_assertion(
            "Loan TotalValueOutstanding", loan.get("TotalValueOutstanding"), str(principal)
        ),
        state_assertion(
            "Loan ManagementFeeOutstanding", loan.get("ManagementFeeOutstanding", "0"), "0"
        ),
        state_assertion("Loan Broker owner", after_broker.get("Owner"), terms["Counterparty"]),
        state_assertion("Loan Broker VaultID", after_broker.get("VaultID"), terms["VaultID"]),
        state_assertion("Vault identity", after_vault.get("index"), terms["VaultID"]),
        state_assertion("cash-basis Vault LEVersion", after_vault.get("LEVersion"), 1),
        state_assertion(
            "Vault AssetsAvailable delta",
            int(after_vault["AssetsAvailable"]) - int(before_vault["AssetsAvailable"]),
            -principal,
        ),
        state_assertion(
            "cash-basis Vault AssetsTotal unchanged",
            after_vault.get("AssetsTotal"),
            before_vault.get("AssetsTotal"),
        ),
        state_assertion(
            "Loan Broker DebtTotal delta",
            int(after_broker.get("DebtTotal", "0")) - int(before_broker.get("DebtTotal", "0")),
            principal,
        ),
        state_assertion(
            "Loan Broker CoverAvailable unchanged",
            after_broker.get("CoverAvailable"),
            before_broker.get("CoverAvailable"),
        ),
        state_assertion(
            "Loan Broker LoanSequence increment",
            after_broker.get("LoanSequence"),
            loan_sequence + 1,
        ),
        state_assertion(
            "Loan Broker OwnerCount increment",
            int(after_broker.get("OwnerCount", 0)) - int(before_broker.get("OwnerCount", 0)),
            1,
        ),
        state_assertion(
            "borrower OwnerCount increment",
            int(after_borrower.get("OwnerCount", 0)) - int(before_borrower.get("OwnerCount", 0)),
            1,
        ),
        state_assertion(
            "borrower transaction Sequence increment",
            int(after_borrower["Sequence"]) - int(before_borrower["Sequence"]),
            1,
        ),
        state_assertion(
            "borrower tRLUSD disbursement",
            int(after["borrower_balance"]) - int(before["borrower_balance"]),
            principal - origination_fee,
        ),
        state_assertion(
            "Loan Broker owner origination fee",
            int(after["broker_owner_balance"]) - int(before["broker_owner_balance"]),
            origination_fee,
        ),
        state_assertion(
            "Vault pseudo-account asset debit",
            int(after["vault_pseudo_balance"]) - int(before["vault_pseudo_balance"]),
            -principal,
        ),
        state_assertion(
            "borrower SignerList unchanged", after.get("signer_lists"), before.get("signer_lists")
        ),
    ]
    checks.extend(
        state_assertion(
            f"Loan {field}", loan.get(field, 0 if isinstance(value, int) else "0"), value
        )
        for field, value in copied_term_fields.items()
    )
    return checks


def negative_state_assertions(
    *,
    observed_result: str | None,
    expected_result: str,
    before: dict[str, Any],
    after: dict[str, Any],
) -> list[StateAssertion]:
    """Require the predeclared rejection and exact protected-state preservation."""

    return [
        state_assertion("negative authoritative result", observed_result, expected_result),
        state_assertion("negative protected state", after, before),
    ]


def live_binding_assertions(bindings: dict[str, Any]) -> list[StateAssertion]:
    """Prevent a positive live submission until every frozen binding is populated."""

    missing = sorted(
        name
        for name in REQUIRED_LIVE_BINDINGS
        if name not in bindings or bindings[name] in (None, "")
    )
    checks = [state_assertion("required E6 live bindings", missing, [])]
    envelope = bindings.get("exact_loan_set_body")
    projection = bindings.get("positive_oracle_terms")
    borrower = bindings.get("borrower_account")
    if isinstance(projection, dict) and isinstance(borrower, str):
        checks.extend(
            positive_oracle_projection_assertions(
                projection,
                intended_borrower=borrower,
            )
        )
    else:
        checks.append(
            predicate_assertion(
                "positive oracle projection is bound",
                False,
                True,
                type(projection).__name__,
            )
        )
    if isinstance(envelope, dict) and isinstance(projection, dict):
        expected_projection = {
            field_name: (
                bindings.get("vault_id") if field_name == "VaultID" else envelope.get(field_name)
            )
            for field_name in POSITIVE_ORACLE_REQUIRED_FIELDS
        }
        checks.append(
            state_assertion(
                "positive oracle projection bound to finalized envelope",
                projection,
                expected_projection,
            )
        )
    else:
        checks.append(
            predicate_assertion(
                "finalized envelope and positive oracle projection are mappings",
                False,
                True,
                {
                    "envelope": type(envelope).__name__,
                    "projection": type(projection).__name__,
                },
            )
        )
    return checks


def require_live_bindings(bindings: dict[str, Any]) -> None:
    """Fail closed before signing or submission when a frozen binding is absent."""

    checks = live_binding_assertions(bindings)
    if not all(check.passed for check in checks):
        failures = "; ".join(
            f"{check.name}: actual={check.actual!r}, expected={check.expected!r}"
            for check in checks
            if not check.passed
        )
        raise E6BindingError(failures)
