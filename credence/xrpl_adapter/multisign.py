"""X1a: offline 2-of-2 XRPL multisignature assembly and verification.

The agent payment account is controlled for routine spends by a validated SignerList of exactly
two signers, ResearchBot and the Credence policy signer, each with weight 1 and quorum 2. This
module builds the unsigned provider Payment, computes its authorized digest, produces one
signer's multisignature, assembles the canonical ``Signers`` array, and verifies a multisigned
Payment cryptographically against validated ledger state supplied by a trusted transport.

What verification establishes, in order:

1. the bytes decode as a multisigned tRLUSD ``Payment`` with no single-signature fields;
2. the transaction, with its ``Signers`` removed, hashes to the authorized unsigned digest, and
   its account, destination, asset, amount and network match the expected payment;
3. the SignerList was read from a validated ledger and is exactly the two expected signers with
   weight 1 each and quorum 2;
4. the account has no RegularKey, and its master-key state is the one Credence configured;
5. the ``Signers`` array holds exactly the two expected accounts, in XRPL canonical order, each
   signing with its own master key (not disabled on ledger), each signature valid over the XRPL
   multisigning message for that account, and their weights meet the quorum;
6. the fee covers the multisign fee ``base_fee * (1 + signer_count)`` and is within the ceiling;
   Every signature must also be canonical as rippled requires (``RequireFullyCanonicalSig``):
   strict DER with low S for secp256k1, ``S < L`` for Ed25519 (``SIGNATURE_NOT_CANONICAL``, X1c C2);
7. Sequence (or TicketSequence with Sequence 0) is the next usable value on the validated ledger;
8. LastLedgerSequence is beyond the validated ledger and within the configured window.

Limitations, stated explicitly:

- XRPL distinguishes two layers. The agent payment account's SignerList names which *accounts*
  may sign for it and their weights. Each signer account's own signing authority is decided
  separately by that account: rippled accepts a multisignature for a funded signer account from
  its master key (unless disabled) or from its RegularKey. So a RegularKey on a signer account is
  an additional key able to act as that signer. Credence requires each signer to sign with the
  master key whose address is the signer account, refuses a funded signer account that holds a
  RegularKey (``SIGNER_REGULAR_KEY_PRESENT``, X1c B4), and refuses one whose master key is
  disabled. The two-signature quorum is unchanged.
- A RegularKey on the agent payment account is always refused.
- The agent payment account's master key is not something multisigning can restrict, and
  nothing here makes a master key incapable of acting outside the routine two-signature flow. With
  ``MasterKeyPolicy.OWNER_RETAINED`` (the build plan's setup: the owner's Ledger holds it for
  revocation and the emergency sweep) the owner can authorize any transaction alone. With
  ``MasterKeyPolicy.DISABLED`` only the SignerList can authorize transactions.
- The ledger state is a statement by the configured trusted ledger source. It is not
  independently verified consensus proof.

No private key is stored. ``sign_as_signer`` takes a key for one call and keeps nothing. Tests use
deterministic TEST keys derived at runtime; no key material is committed.

Owner: XRPL Integration (X1a, authorized by Maura).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any

from xrpl.core.addresscodec import decode_classic_address, is_valid_classic_address
from xrpl.core.binarycodec import decode, encode, encode_for_multisigning, encode_for_signing
from xrpl.core.keypairs import derive_classic_address, is_valid_message
from xrpl.core.keypairs import sign as xrpl_sign

from credence.xrpl_adapter.preparation import (
    TRLUSD_WIRE_CODE,
    XrplSignedPaymentInspector,
    transaction_hash,
)
from credence.xrpl_adapter.types import Asset, ParsedSignedPayment, SignedTransactionError

UNSIGNED_DIGEST_SCHEME = "xrpl-stx-sha512half-v1"
REQUIRED_QUORUM = 2
SIGNER_WEIGHT = 1
LSF_DISABLE_MASTER = 0x00100000
_SIGNING_FIELDS = ("Signers", "TxnSignature")


class MasterKeyPolicy(Enum):
    """What Credence requires of the agent payment account's master key."""

    OWNER_RETAINED = "owner_retained"
    DISABLED = "disabled"


class MultisignRejected(ValueError):
    """A multisigned transaction, signer list or signer input is refused. ``code`` is stable."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class SignerEntry:
    account: str
    weight: int


@dataclass(frozen=True)
class SignerListSnapshot:
    """A SignerList as read from one ledger by the trusted transport."""

    account: str
    quorum: int
    entries: tuple[SignerEntry, ...]


@dataclass(frozen=True)
class AccountSnapshot:
    """One account's key, sequence and balance state on one ledger, from the trusted transport.

    ``exists`` is false for an account the ledger does not hold (a signer may be unfunded).
    """

    account: str
    exists: bool
    validated: bool
    ledger_index: int
    ledger_hash: str
    flags: int = 0
    regular_key: str | None = None
    sequence: int | None = None
    balance_drops: int | None = None
    owner_count: int | None = None
    transfer_rate: int | None = None
    tickets: tuple[int, ...] = ()
    signer_list: SignerListSnapshot | None = None

    @property
    def master_key_disabled(self) -> bool:
        return bool(self.flags & LSF_DISABLE_MASTER)


@dataclass(frozen=True)
class MultisignPolicy:
    """Credence's requirements for the agent payment account and its two signers."""

    account: str
    researchbot_account: str
    policy_signer_account: str
    network_id: int
    master_key: MasterKeyPolicy
    maximum_fee_drops: int
    maximum_ledger_window: int

    def __post_init__(self) -> None:
        for name in ("account", "researchbot_account", "policy_signer_account"):
            if not is_valid_classic_address(getattr(self, name)):
                raise ValueError(f"{name} must be a valid XRPL classic address")
        accounts = {self.account, self.researchbot_account, self.policy_signer_account}
        if len(accounts) != 3:
            raise ValueError("account and both signer accounts must be distinct")
        if not isinstance(self.master_key, MasterKeyPolicy):
            raise ValueError("master_key must be a MasterKeyPolicy")
        for name in ("network_id", "maximum_fee_drops", "maximum_ledger_window"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")

    @property
    def signer_accounts(self) -> frozenset[str]:
        return frozenset({self.researchbot_account, self.policy_signer_account})


@dataclass(frozen=True)
class ExpectedPayment:
    """The one payment that was authorized, including its unsigned-transaction digest."""

    account: str
    destination: str
    asset: Asset
    amount_micro_units: int
    network_id: int
    unsigned_transaction_digest: str


@dataclass(frozen=True)
class LedgerContext:
    """Validated ledger facts used to check a multisigned payment, all from one ledger."""

    validated_ledger_index: int
    validated_ledger_hash: str
    base_fee_drops: int
    network_id: int
    account: AccountSnapshot
    signers: Mapping[str, AccountSnapshot]


@dataclass(frozen=True)
class MultisignSignature:
    """One signer's contribution to the ``Signers`` array."""

    account: str
    signing_public_key: str
    signature: str


@dataclass(frozen=True)
class VerifiedMultisign:
    signed_transaction: bytes
    transaction_hash: str
    unsigned_transaction_digest: str
    payment: ParsedSignedPayment
    signer_accounts: tuple[str, ...]


# --- unsigned transaction --------------------------------------------------------------------


def trlusd_value(amount_micro_units: int) -> str:
    """Issued-amount value string for integer micro-tRLUSD (tRLUSD is a test token)."""
    if isinstance(amount_micro_units, bool) or not isinstance(amount_micro_units, int):
        raise ValueError("amount must be integer micro-units")
    if amount_micro_units < 1:
        raise ValueError("amount must be positive")
    value = Decimal(amount_micro_units).scaleb(-6).normalize()
    return format(value, "f")


def build_unsigned_payment(
    *,
    account: str,
    destination: str,
    asset: Asset,
    amount_micro_units: int,
    fee_drops: int,
    network_id: int,
    last_ledger_sequence: int,
    sequence: int | None = None,
    ticket_sequence: int | None = None,
) -> dict[str, Any]:
    """The unsigned, multisign-ready Payment: empty SigningPubKey, no signatures, Flags 0."""
    if (sequence is None) == (ticket_sequence is None):
        raise ValueError("exactly one of sequence or ticket_sequence is required")
    if asset.label != "tRLUSD" or asset.currency_code != TRLUSD_WIRE_CODE:
        raise ValueError("only the tRLUSD test token is supported")
    transaction: dict[str, Any] = {
        "TransactionType": "Payment",
        "Account": account,
        "Destination": destination,
        "Amount": {
            "currency": asset.currency_code,
            "issuer": asset.issuer,
            "value": trlusd_value(amount_micro_units),
        },
        "Fee": str(fee_drops),
        "Flags": 0,
        "NetworkID": network_id,
        "LastLedgerSequence": last_ledger_sequence,
        "SigningPubKey": "",
    }
    if sequence is not None:
        transaction["Sequence"] = sequence
    else:
        transaction["Sequence"] = 0
        transaction["TicketSequence"] = ticket_sequence
    return transaction


def unsigned_transaction_digest(transaction: Mapping[str, Any]) -> str:
    """SHA-512Half over the XRPL single-signing serialization (``STX\\0`` prefix) of the unsigned
    transaction: empty SigningPubKey, no TxnSignature, no Signers. Scheme
    ``xrpl-stx-sha512half-v1``. Upper-case hex, 64 digits."""
    if any(field in transaction for field in _SIGNING_FIELDS):
        raise MultisignRejected("NOT_UNSIGNED", "unsigned transaction carries signatures")
    if transaction.get("SigningPubKey") != "":
        raise MultisignRejected("NOT_UNSIGNED", "multisign form requires empty SigningPubKey")
    try:
        encoded = encode_for_signing(dict(transaction))
    except Exception as exc:  # xrpl-py raises several codec error types
        raise MultisignRejected("MALFORMED_TRANSACTION", "transaction does not encode") from exc
    return hashlib.sha512(bytes.fromhex(encoded)).hexdigest()[:64].upper()


def multisign_fee_drops(base_fee_drops: int, signer_count: int) -> int:
    """XRPL multisign cost: the base fee times (1 + number of signatures)."""
    if isinstance(base_fee_drops, bool) or not isinstance(base_fee_drops, int):
        raise ValueError("base fee must be integer drops")
    if base_fee_drops < 1 or signer_count < 1:
        raise ValueError("base fee and signer count must be positive")
    return base_fee_drops * (1 + signer_count)


def multisign_message(transaction: Mapping[str, Any], signer_account: str) -> bytes:
    """The bytes one signer signs: ``SMT\\0`` + signing fields + the signer's account ID."""
    return bytes.fromhex(encode_for_multisigning(dict(transaction), signer_account))


def sign_as_signer(
    transaction: Mapping[str, Any],
    *,
    account: str,
    public_key: str,
    private_key: str,
) -> MultisignSignature:
    """Produce one master-key multisignature. The private key is used once and not retained."""
    if derive_classic_address(public_key) != account:
        raise MultisignRejected(
            "SIGNER_KEY_NOT_MASTER", "only the signer account's master key is supported"
        )
    unsigned_transaction_digest(transaction)
    signature = xrpl_sign(multisign_message(transaction, account), private_key)
    return MultisignSignature(account=account, signing_public_key=public_key, signature=signature)


def _account_id(account: str) -> bytes:
    return decode_classic_address(account)


def assemble_multisigned(
    transaction: Mapping[str, Any], signatures: Sequence[MultisignSignature]
) -> bytes:
    """Combine signatures over one unsigned transaction into canonical multisigned bytes.

    Every signature must verify over this exact transaction before it is included; the
    ``Signers`` array is sorted by numeric account ID, as XRPL requires.
    """
    unsigned_transaction_digest(transaction)
    accounts = [signature.account for signature in signatures]
    if len(set(accounts)) != len(accounts):
        raise MultisignRejected("DUPLICATE_SIGNER")
    for signature in signatures:
        _verify_signature(transaction, signature)
    ordered = sorted(signatures, key=lambda item: _account_id(item.account))
    signed = dict(transaction)
    signed["Signers"] = [
        {
            "Signer": {
                "Account": item.account,
                "SigningPubKey": item.signing_public_key,
                "TxnSignature": item.signature,
            }
        }
        for item in ordered
    ]
    return bytes.fromhex(encode(signed))


def _verify_signature(transaction: Mapping[str, Any], signature: MultisignSignature) -> None:
    try:
        derived = derive_classic_address(signature.signing_public_key)
    except Exception as exc:  # malformed public key encodings raise several error types
        raise MultisignRejected("SIGNATURE_INVALID", "malformed signer public key") from exc
    if derived != signature.account:
        raise MultisignRejected(
            "SIGNER_KEY_NOT_MASTER", "signer key is not the signer account's master key"
        )
    try:
        signature_bytes = bytes.fromhex(signature.signature)
    except ValueError as exc:
        raise MultisignRejected("SIGNATURE_INVALID", "signature is not hex") from exc
    if not is_canonical_signature(signature.signing_public_key, signature_bytes):
        raise MultisignRejected(
            "SIGNATURE_NOT_CANONICAL", f"signature by {signature.account} is not canonical"
        )
    try:
        valid = is_valid_message(
            multisign_message(transaction, signature.account),
            bytes.fromhex(signature.signature),
            signature.signing_public_key,
        )
    except Exception:  # malformed signatures raise from the underlying curve libraries
        valid = False
    if not valid:
        raise MultisignRejected("SIGNATURE_INVALID", f"signature by {signature.account} fails")


# Group orders: secp256k1 n and the Ed25519 subgroup order L.
_SECP256K1_ORDER = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_ED25519_ORDER = 2**252 + 27742317777372353535851937790883648493


def _der_integer(data: bytes, offset: int) -> tuple[int, int] | None:
    """One strictly minimal, positive DER INTEGER at ``offset``: (value, next offset)."""
    if offset + 2 > len(data) or data[offset] != 0x02:
        return None
    length = data[offset + 1]
    start, end = offset + 2, offset + 2 + length
    if length == 0 or length > 33 or end > len(data):
        return None
    body = data[start:end]
    if body[0] & 0x80:
        return None  # negative
    if length > 1 and body[0] == 0x00 and not body[1] & 0x80:
        return None  # non-minimal leading zero
    return int.from_bytes(body, "big"), end


def is_canonical_signature(public_key: str, signature: bytes) -> bool:
    """rippled's fully-canonical rule: Ed25519 ``S < L``; secp256k1 strict DER, ``0 < r, s < n``
    and low ``s`` (``s <= n / 2``)."""
    if public_key.upper().startswith("ED"):
        return len(signature) == 64 and int.from_bytes(signature[32:], "little") < _ED25519_ORDER
    if len(signature) < 8 or len(signature) > 72 or signature[0] != 0x30:
        return False
    if signature[1] != len(signature) - 2:
        return False
    first = _der_integer(signature, 2)
    if first is None:
        return False
    r, offset = first
    second = _der_integer(signature, offset)
    if second is None:
        return False
    s, offset = second
    if offset != len(signature):
        return False
    return 0 < r < _SECP256K1_ORDER and 0 < s <= _SECP256K1_ORDER // 2


# --- ledger-state checks ---------------------------------------------------------------------


def verify_signer_list(policy: MultisignPolicy, account: AccountSnapshot) -> None:
    """The validated SignerList is exactly ResearchBot and the policy signer, 1 + 1, quorum 2."""
    if not account.validated:
        raise MultisignRejected(
            "LEDGER_NOT_VALIDATED", "account state is not from a validated ledger"
        )
    if account.account != policy.account or not account.exists:
        raise MultisignRejected("ACCOUNT_NOT_FOUND", "agent payment account state is missing")
    signer_list = account.signer_list
    if signer_list is None:
        raise MultisignRejected("SIGNER_LIST_MISSING")
    if signer_list.account != policy.account:
        raise MultisignRejected("SIGNER_LIST_MISMATCH", "signer list belongs to another account")
    if signer_list.quorum != REQUIRED_QUORUM:
        raise MultisignRejected("QUORUM_MISMATCH", f"quorum is {signer_list.quorum}")
    entries = {entry.account: entry.weight for entry in signer_list.entries}
    if len(entries) != len(signer_list.entries) or set(entries) != policy.signer_accounts:
        raise MultisignRejected("SIGNER_LIST_MISMATCH", "signer accounts are not the expected two")
    if any(weight != SIGNER_WEIGHT for weight in entries.values()):
        raise MultisignRejected("SIGNER_WEIGHT_MISMATCH", "each signer must have weight 1")


def verify_account_keys(policy: MultisignPolicy, account: AccountSnapshot) -> None:
    """No RegularKey, and the master-key state Credence configured."""
    if account.regular_key is not None:
        raise MultisignRejected("REGULAR_KEY_PRESENT", "agent payment account has a RegularKey")
    disabled = account.master_key_disabled
    if policy.master_key is MasterKeyPolicy.DISABLED and not disabled:
        raise MultisignRejected("MASTER_KEY_STATE_MISMATCH", "master key is enabled")
    if policy.master_key is MasterKeyPolicy.OWNER_RETAINED and disabled:
        raise MultisignRejected("MASTER_KEY_STATE_MISMATCH", "master key is disabled")


def verify_ledger_context(policy: MultisignPolicy, ledger: LedgerContext) -> None:
    """The account, its SignerList and every signer state come from one validated ledger."""
    if ledger.network_id != policy.network_id:
        raise MultisignRejected("NETWORK_MISMATCH", "ledger source is on another network")
    states = (ledger.account, *ledger.signers.values())
    for state in states:
        if not state.validated:
            raise MultisignRejected("LEDGER_NOT_VALIDATED", f"{state.account} is not validated")
        if (
            state.ledger_index != ledger.validated_ledger_index
            or state.ledger_hash != ledger.validated_ledger_hash
        ):
            raise MultisignRejected(
                "LEDGER_STATE_INCONSISTENT", "account states come from different ledgers"
            )
    verify_signer_list(policy, ledger.account)
    verify_account_keys(policy, ledger.account)
    for signer in policy.signer_accounts:
        signer_state = ledger.signers.get(signer)
        if signer_state is None or signer_state.account != signer:
            raise MultisignRejected("SIGNER_STATE_MISSING", f"no ledger state for {signer}")
        if signer_state.exists and signer_state.regular_key is not None:
            raise MultisignRejected(
                "SIGNER_REGULAR_KEY_PRESENT", f"{signer} has a RegularKey that could sign for it"
            )
        if signer_state.exists and signer_state.master_key_disabled:
            raise MultisignRejected(
                "SIGNER_MASTER_DISABLED", f"{signer} has disabled its master key"
            )


def verify_submission_window(
    policy: MultisignPolicy,
    ledger: LedgerContext,
    *,
    fee_drops: int,
    sequence: int | None,
    ticket_sequence: int | None,
    last_ledger_sequence: int,
) -> None:
    """Fee, Sequence or TicketSequence, and LastLedgerSequence against the validated ledger."""
    required_fee = multisign_fee_drops(ledger.base_fee_drops, REQUIRED_QUORUM)
    if fee_drops < required_fee:
        raise MultisignRejected("FEE_TOO_LOW", f"multisign fee is at least {required_fee} drops")
    if fee_drops > policy.maximum_fee_drops:
        raise MultisignRejected("FEE_ABOVE_CEILING")
    if ticket_sequence is not None:
        if ticket_sequence not in ledger.account.tickets:
            raise MultisignRejected("TICKET_UNAVAILABLE")
    elif sequence is None or sequence != ledger.account.sequence:
        raise MultisignRejected("SEQUENCE_MISMATCH")
    if last_ledger_sequence <= ledger.validated_ledger_index:
        raise MultisignRejected("LAST_LEDGER_SEQUENCE_EXPIRED")
    if last_ledger_sequence > ledger.validated_ledger_index + policy.maximum_ledger_window:
        raise MultisignRejected("LAST_LEDGER_SEQUENCE_TOO_FAR")


# --- full verification -----------------------------------------------------------------------


def _decode_multisigned(signed_transaction: bytes) -> dict[str, Any]:
    try:
        decoded = decode(signed_transaction.hex().upper())
    except Exception as exc:  # xrpl-py raises several codec error types
        raise MultisignRejected("MALFORMED_TRANSACTION", "bytes do not decode") from exc
    if decoded.get("TransactionType") != "Payment":
        raise MultisignRejected("WRONG_TRANSACTION_TYPE")
    if decoded.get("SigningPubKey") != "" or "TxnSignature" in decoded:
        raise MultisignRejected("NOT_MULTISIGNED", "single-signature fields are present")
    signers = decoded.get("Signers")
    if not isinstance(signers, list) or not signers:
        raise MultisignRejected("NOT_MULTISIGNED", "no Signers array")
    return decoded


def _signatures(decoded: Mapping[str, Any]) -> list[MultisignSignature]:
    result: list[MultisignSignature] = []
    for entry in decoded["Signers"]:
        signer = entry.get("Signer") if isinstance(entry, dict) else None
        if not isinstance(signer, dict) or set(signer) != {
            "Account",
            "SigningPubKey",
            "TxnSignature",
        }:
            raise MultisignRejected("MALFORMED_SIGNER")
        result.append(
            MultisignSignature(
                account=str(signer["Account"]),
                signing_public_key=str(signer["SigningPubKey"]),
                signature=str(signer["TxnSignature"]),
            )
        )
    return result


def verify_multisigned_payment(
    signed_transaction: bytes,
    *,
    policy: MultisignPolicy,
    expected: ExpectedPayment,
    ledger: LedgerContext,
) -> VerifiedMultisign:
    """Verify one multisigned provider Payment. Raises ``MultisignRejected`` with a stable code."""
    decoded = _decode_multisigned(signed_transaction)
    signatures = _signatures(decoded)
    unsigned = {key: value for key, value in decoded.items() if key != "Signers"}

    # Exact transaction identity: everything but the signatures is what was authorized.
    if unsigned_transaction_digest(unsigned) != expected.unsigned_transaction_digest:
        raise MultisignRejected("TRANSACTION_DIGEST_MISMATCH")

    accounts = [signature.account for signature in signatures]
    if len(set(accounts)) != len(accounts):
        raise MultisignRejected("DUPLICATE_SIGNER")
    if accounts != sorted(accounts, key=_account_id):
        raise MultisignRejected("SIGNER_ORDER", "Signers are not in canonical account order")

    try:
        parsed = XrplSignedPaymentInspector().inspect_payment(signed_transaction)
    except SignedTransactionError as exc:
        raise MultisignRejected("MALFORMED_TRANSACTION", str(exc)) from exc

    field_checks = (
        (parsed.account == expected.account == policy.account, "ACCOUNT_MISMATCH"),
        (parsed.destination == expected.destination, "DESTINATION_MISMATCH"),
        (parsed.asset.currency_code == expected.asset.currency_code, "CURRENCY_MISMATCH"),
        (parsed.asset.issuer == expected.asset.issuer, "ISSUER_MISMATCH"),
        (parsed.amount_micro_units == expected.amount_micro_units, "AMOUNT_MISMATCH"),
        (parsed.network_id == expected.network_id == policy.network_id, "NETWORK_MISMATCH"),
    )
    for passed, code in field_checks:
        if not passed:
            raise MultisignRejected(code)

    verify_ledger_context(policy, ledger)

    present = set(accounts)
    missing = policy.signer_accounts - present
    unexpected = present - policy.signer_accounts
    if unexpected:
        raise MultisignRejected("UNEXPECTED_SIGNER", ", ".join(sorted(unexpected)))
    if missing:
        raise MultisignRejected("SIGNER_MISSING", ", ".join(sorted(missing)))
    for signature in signatures:
        _verify_signature(unsigned, signature)
    signer_list = ledger.account.signer_list
    assert signer_list is not None  # verify_signer_list refused a missing list
    weights = {entry.account: entry.weight for entry in signer_list.entries}
    if sum(weights[account] for account in present) < REQUIRED_QUORUM:
        raise MultisignRejected("QUORUM_NOT_MET")

    verify_submission_window(
        policy,
        ledger,
        fee_drops=parsed.fee_drops,
        sequence=parsed.sequence,
        ticket_sequence=parsed.ticket_sequence,
        last_ledger_sequence=parsed.last_ledger_sequence,
    )
    return VerifiedMultisign(
        signed_transaction=signed_transaction,
        transaction_hash=transaction_hash(signed_transaction),
        unsigned_transaction_digest=expected.unsigned_transaction_digest,
        payment=parsed,
        signer_accounts=tuple(accounts),
    )
