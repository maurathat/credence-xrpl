"""Payment preparation and signed-blob inspection without key custody."""

from __future__ import annotations

import hashlib
from decimal import Decimal, InvalidOperation

from xrpl.core.addresscodec import decode_classic_address, is_valid_classic_address
from xrpl.core.binarycodec import decode

from credence.xrpl_adapter.types import (
    Asset,
    ParsedSignedPayment,
    PaymentRequest,
    PreparationError,
    PreparedPayment,
    SignedTransactionError,
)

_TRANSACTION_HASH_PREFIX = bytes.fromhex("54584E00")
TRLUSD_WIRE_CODE = b"tRLUSD".hex().upper().ljust(40, "0")
MICRO_UNITS_PER_TRLUSD = 1_000_000
_ALLOWED_PAYMENT_FIELDS = frozenset(
    {
        "Account",
        "Amount",
        "Destination",
        "Fee",
        "Flags",
        "LastLedgerSequence",
        "NetworkID",
        "Sequence",
        "Signers",
        "SigningPubKey",
        "TicketSequence",
        "TransactionType",
        "TxnSignature",
    }
)


def transaction_hash(signed_transaction: bytes) -> str:
    """Return the XRPL transaction ID derived from the actual signed bytes."""
    if not signed_transaction:
        raise SignedTransactionError("signed transaction bytes must not be empty")
    return hashlib.sha512(_TRANSACTION_HASH_PREFIX + signed_transaction).hexdigest()[:64].upper()


def _require_int(name: str, value: object, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise PreparationError(f"{name} must be an integer >= {minimum}")
    return value


def _micro_units(value: object) -> int:
    if not isinstance(value, str):
        raise SignedTransactionError("issued payment amount must be a decimal string")
    try:
        scaled = Decimal(value) * MICRO_UNITS_PER_TRLUSD
    except InvalidOperation as exc:
        raise SignedTransactionError("issued payment amount is not a decimal") from exc
    if not scaled.is_finite() or scaled != scaled.to_integral_value() or scaled <= 0:
        raise SignedTransactionError("issued payment amount is not positive integer micro-units")
    return int(scaled)


class OfflinePaymentPreparer:
    """Validate a Payment intent before any signer or transport is invoked."""

    def __init__(self, *, maximum_fee_drops: int) -> None:
        self._maximum_fee_drops = _require_int("maximum_fee_drops", maximum_fee_drops, minimum=1)

    def prepare_payment(self, request: PaymentRequest) -> PreparedPayment:
        if not request.operation_id.strip():
            raise PreparationError("operation_id must not be empty")
        if not request.payment_authorization_id.strip():
            raise PreparationError("payment authorization identifier is required")
        if not is_valid_classic_address(request.account):
            raise PreparationError("account must be a valid XRPL classic address")
        if not is_valid_classic_address(request.destination):
            raise PreparationError("destination must be a valid XRPL classic address")
        if request.account == request.destination:
            raise PreparationError("payment destination must differ from account")
        if request.asset.label != "tRLUSD":
            raise PreparationError("X0 supports only tRLUSD provider payments")
        if request.asset.currency_code != TRLUSD_WIRE_CODE:
            raise PreparationError("tRLUSD XRPL wire currency code is invalid")
        if not is_valid_classic_address(request.asset.issuer):
            raise PreparationError("asset issuer must be a valid XRPL classic address")
        _require_int("amount_micro_units", request.amount_micro_units, minimum=1)
        fee = _require_int("fee_drops", request.fee_drops, minimum=1)
        if fee > self._maximum_fee_drops:
            raise PreparationError("fee exceeds the configured payment ceiling")
        _require_int("network_id", request.network_id, minimum=1)
        earliest = _require_int(
            "earliest_ledger_sequence", request.earliest_ledger_sequence, minimum=1
        )
        last = _require_int("last_ledger_sequence", request.last_ledger_sequence, minimum=1)
        if last < earliest:
            raise PreparationError("LastLedgerSequence precedes the history evidence window")
        if (request.sequence is None) == (request.ticket_sequence is None):
            raise PreparationError("exactly one of Sequence or TicketSequence is required")
        if request.sequence is not None:
            _require_int("sequence", request.sequence, minimum=1)
        if request.ticket_sequence is not None:
            _require_int("ticket_sequence", request.ticket_sequence, minimum=1)
        return PreparedPayment(request=request)


class XrplSignedPaymentInspector:
    """Structurally inspect Payment bytes; this does not verify signatures."""

    def inspect_payment(self, signed_transaction: bytes) -> ParsedSignedPayment:
        try:
            decoded = decode(signed_transaction.hex().upper())
        except Exception as exc:  # xrpl-py exposes multiple codec error types
            raise SignedTransactionError("signed transaction bytes do not decode") from exc
        if decoded.get("TransactionType") != "Payment":
            raise SignedTransactionError("signed transaction is not a Payment")
        unexpected_fields = sorted(set(decoded) - _ALLOWED_PAYMENT_FIELDS)
        if unexpected_fields:
            raise SignedTransactionError(
                "signed Payment contains unauthorized fields: " + ", ".join(unexpected_fields)
            )
        if decoded.get("Flags", 0) not in (0, "0"):
            raise SignedTransactionError("signed Payment contains unauthorized flags")
        signing_public_key = decoded.get("SigningPubKey")
        transaction_signature = decoded.get("TxnSignature")
        has_single_signature = bool(signing_public_key) and bool(transaction_signature)
        signers = decoded.get("Signers")
        has_multisignatures = isinstance(signers, list) and bool(signers)
        if has_single_signature and has_multisignatures:
            raise SignedTransactionError(
                "single-signature and multi-signature forms cannot be mixed"
            )
        if has_multisignatures:
            assert isinstance(signers, list)
            if signing_public_key != "" or transaction_signature is not None:
                raise SignedTransactionError(
                    "multi-signature Payment requires empty SigningPubKey and no TxnSignature"
                )
            _validate_multisignature_structure(signers)
        elif has_single_signature:
            if not isinstance(signing_public_key, str) or not isinstance(
                transaction_signature, str
            ):
                raise SignedTransactionError("single-signature fields are malformed")
        else:
            raise SignedTransactionError("transaction has no structural signing fields")

        amount = decoded.get("Amount")
        if not isinstance(amount, dict):
            raise SignedTransactionError("Payment Amount must be an issued asset")
        currency = amount.get("currency")
        issuer = amount.get("issuer")
        if not isinstance(currency, str) or not isinstance(issuer, str):
            raise SignedTransactionError("issued Payment Amount is incomplete")

        sequence_value = decoded.get("Sequence")
        ticket_value = decoded.get("TicketSequence")
        sequence = None if ticket_value is not None else _decoded_int("Sequence", sequence_value)
        ticket = None if ticket_value is None else _decoded_int("TicketSequence", ticket_value)
        if ticket is not None and sequence_value not in (0, "0"):
            raise SignedTransactionError("ticketed Payment must carry Sequence 0")

        return ParsedSignedPayment(
            account=_decoded_text(decoded, "Account"),
            destination=_decoded_text(decoded, "Destination"),
            asset=Asset(label="tRLUSD", currency_code=currency, issuer=issuer),
            amount_micro_units=_micro_units(amount.get("value")),
            fee_drops=_decoded_int("Fee", decoded.get("Fee")),
            network_id=_decoded_int("NetworkID", decoded.get("NetworkID")),
            last_ledger_sequence=_decoded_int(
                "LastLedgerSequence", decoded.get("LastLedgerSequence")
            ),
            sequence=sequence,
            ticket_sequence=ticket,
        )


def _validate_multisignature_structure(signers: list[object]) -> None:
    accounts: list[str] = []
    for entry in signers:
        if not isinstance(entry, dict) or set(entry) != {"Signer"}:
            raise SignedTransactionError("multi-signature Payment contains a malformed signer")
        signer = entry["Signer"]
        if not isinstance(signer, dict):
            raise SignedTransactionError("multi-signature Payment contains a malformed signer")
        account = signer.get("Account")
        signing_public_key = signer.get("SigningPubKey")
        signature = signer.get("TxnSignature")
        if (
            not isinstance(account, str)
            or not account
            or not isinstance(signing_public_key, str)
            or not signing_public_key
            or not isinstance(signature, str)
            or not signature
        ):
            raise SignedTransactionError("multi-signature Payment contains empty signer fields")
        if not is_valid_classic_address(account):
            raise SignedTransactionError(
                "multi-signature Payment contains an invalid signer account"
            )
        accounts.append(account)
    if len(set(accounts)) != len(accounts):
        raise SignedTransactionError("multi-signature Payment contains duplicate signer accounts")
    if accounts != sorted(accounts, key=decode_classic_address):
        raise SignedTransactionError("multi-signature Payment signers are not canonically sorted")


def _decoded_text(decoded: dict[str, object], name: str) -> str:
    value = decoded.get(name)
    if not isinstance(value, str) or not value:
        raise SignedTransactionError(f"signed Payment has no {name}")
    return value


def _decoded_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise SignedTransactionError(f"signed Payment has invalid {name}")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise SignedTransactionError(f"signed Payment has invalid {name}") from exc
    if result < 0:
        raise SignedTransactionError(f"signed Payment has invalid {name}")
    return result


def assert_signed_payment_matches(prepared: PreparedPayment, parsed: ParsedSignedPayment) -> None:
    """Fail before persistence if signed bytes differ from the authorized intent."""
    request = prepared.request
    expected = (
        request.account,
        request.destination,
        request.asset,
        request.amount_micro_units,
        request.fee_drops,
        request.network_id,
        request.last_ledger_sequence,
        request.sequence,
        request.ticket_sequence,
    )
    actual = (
        parsed.account,
        parsed.destination,
        parsed.asset,
        parsed.amount_micro_units,
        parsed.fee_drops,
        parsed.network_id,
        parsed.last_ledger_sequence,
        parsed.sequence,
        parsed.ticket_sequence,
    )
    if actual != expected:
        raise SignedTransactionError("signed Payment fields differ from authorized intent")
