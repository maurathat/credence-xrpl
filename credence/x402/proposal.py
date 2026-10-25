"""Typed, immutable x402 payment-authorization proposal. Not a signed or approved transaction.

A proposal is what Credence would ask its separately reviewed X1 payment path to authorize for one
x402 purchase: who is buying (agent and Credence purchase intent), from whom (merchant, resource,
method, original request digest), against which payment requirement (its digest and any invoice
binding), exactly what (amount, asset, issuer, destination, network, expiry), as which unsigned
XRPL Payment (its digest), under which idempotency identity, and with which evidence and delivery
terms.

It is NOT:
- signed by anyone, approved by Core, or a Core `PaymentAuthorization` (X1a statement);
- submittable: `submission_authorized` is always False (X402-P0, P2);
- evidence that any payment exists.

The unsigned Payment is built in the multisign-ready form X1a uses (empty SigningPubKey, no
signatures, Flags 0) so its digest uses X1a's scheme `xrpl-stx-sha512half-v1`. It is built here
because X1 is not part of this branch's base; on integration X1a's `build_unsigned_payment` replaces
it (contracts/requests/0010). Every way the Payment the merchant's terms require departs
from X1's restricted template (InvoiceID, Memos, SendMax, SourceTag, DestinationTag, an XRP amount,
or NetworkID on a network that refuses it) is listed in `x1_template_gaps`. Credence does not add
those fields to X1's template.

Owner: x402 (X402-P0).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, fields
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from xrpl.core.binarycodec import encode_for_signing

from credence.core.kessai import JsonValue, canonicalize
from credence.core.primitives import EvidenceTier, Timestamp
from credence.x402.errors import X402Refusal, X402Refused

PROPOSAL_PROTOCOL = "credence.x402.payment_authorization_proposal"
PROPOSAL_VERSION = "1"
UNSIGNED_DIGEST_SCHEME = "xrpl-stx-sha512half-v1"  # same scheme as X1a's multisign module

# X1's currently restricted Payment template admits exactly these fields (X1a/X1c,
# credence.xrpl_adapter.preparation._ALLOWED_PAYMENT_FIELDS at 99ca646). Recorded here, not imported:
# X1 is not in this branch's base.
X1_ALLOWED_PAYMENT_FIELDS = frozenset(
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

# rippled refuses a NetworkID field on networks whose ID is 1024 or lower (mainnet 0, testnet 1,
# devnet 2) and requires it above 1024.
NETWORK_ID_FIELD_THRESHOLD = 1024


@dataclass(frozen=True)
class XrplAmount:
    """An exact XRPL wire amount as the merchant required it. Never rescaled or renamed.

    currency "XRP": value is integer drops. Otherwise currency is the wire code (3 characters or
    40 hex digits) and issuer is the issuing account; value is the XRPL decimal value string.
    """

    currency: str
    issuer: str | None
    value: str

    def wire(self) -> str | dict[str, str]:
        if self.currency == "XRP":
            return self.value
        assert self.issuer is not None
        return {"currency": self.currency, "issuer": self.issuer, "value": self.value}

    def canonical(self) -> dict[str, JsonValue]:
        return {"currency": self.currency, "issuer": self.issuer, "value": self.value}


@dataclass(frozen=True)
class InvoiceBinding:
    """The merchant's invoice binding, as t54's `exact` XRPL scheme defines it.

    `invoice_id` is the merchant's opaque `extra.invoiceId`. t54's scheme page: the signed
    transaction MUST commit to it, by Memos (`MemoData = HEX(UTF-8(invoiceId))`) and/or InvoiceID
    (`InvoiceID = SHA256(UTF-8(invoiceId))`); t54's SDK default and both validated Mainnet payments
    observed use both. X402-P0 proposes both. `source_tag` / `destination_tag` are the merchant's
    `extra.sourceTag` / `extra.destinationTag`, carried only when the merchant states them.
    """

    invoice_id: str | None
    source_tag: int | None
    destination_tag: int | None

    @property
    def invoice_id_field(self) -> str | None:
        if self.invoice_id is None:
            return None
        return hashlib.sha256(self.invoice_id.encode("utf-8")).hexdigest().upper()

    @property
    def memo_data(self) -> str | None:
        if self.invoice_id is None:
            return None
        return self.invoice_id.encode("utf-8").hex().upper()

    def canonical(self) -> dict[str, JsonValue]:
        return {
            "invoice_id": self.invoice_id,
            "invoice_id_field": self.invoice_id_field,
            "memo_data": self.memo_data,
            "source_tag": self.source_tag,
            "destination_tag": self.destination_tag,
        }


@dataclass(frozen=True)
class LedgerContext:
    """Values the unsigned Payment needs that only a validated ledger can supply. In X402-P0 these
    come from configuration or mocks; X1's trusted transport supplies them on integration."""

    source_account: str
    network_id: int
    fee_drops: int
    sequence: int
    last_ledger_sequence: int


@dataclass(frozen=True)
class PurchaseIntent:
    """The Credence side of the purchase: which agent, facility and signed purchase request."""

    agent_id: str
    facility_id: str
    purchase_request_digest: str
    prompt_digest: str


@dataclass(frozen=True)
class ProposalTerms:
    """Everything a proposal is derived from. `credence.x402.prepare` builds these only from
    verified inputs (the Core-evaluated purchase, the parsed challenge, the pinned endpoint, the
    approved execution); a proposal is valid only if it is exactly what these terms produce."""

    intent: PurchaseIntent
    merchant_id: str
    resource_url: str
    request_method: str
    request_digest: str
    requirement_digest: str
    invoice: InvoiceBinding
    amount: XrplAmount
    destination: str
    network: str
    expires_at: Timestamp
    ledger: LedgerContext
    required_evidence_tier: EvidenceTier


@dataclass(frozen=True)
class PaymentAuthorizationProposal:
    proposal_id: str
    intent: PurchaseIntent
    merchant_id: str
    resource_url: str
    request_method: str
    request_digest: str
    requirement_digest: str
    invoice: InvoiceBinding
    amount: XrplAmount
    destination: str
    network: str
    ledger: LedgerContext
    expires_at: Timestamp
    unsigned_transaction: tuple[tuple[str, Any], ...]
    unsigned_transaction_digest: str
    unsigned_transaction_digest_scheme: str
    idempotency_key: str
    required_evidence_tier: EvidenceTier
    payment_before_delivery: bool
    x1_template_gaps: tuple[str, ...]
    submission_authorized: Literal[False] = False

    @property
    def agent_id(self) -> str:
        return self.intent.agent_id

    @property
    def network_id(self) -> int:
        return self.ledger.network_id

    @property
    def source_account(self) -> str:
        return self.ledger.source_account

    @property
    def x1_template_compatible(self) -> bool:
        return not self.x1_template_gaps

    def transaction(self) -> dict[str, Any]:
        return dict(self.unsigned_transaction)

    def terms(self) -> ProposalTerms:
        """The terms as this proposal states them (not trusted: see verify_binding)."""
        return ProposalTerms(
            intent=self.intent,
            merchant_id=self.merchant_id,
            resource_url=self.resource_url,
            request_method=self.request_method,
            request_digest=self.request_digest,
            requirement_digest=self.requirement_digest,
            invoice=self.invoice,
            amount=self.amount,
            destination=self.destination,
            network=self.network,
            expires_at=self.expires_at,
            ledger=self.ledger,
            required_evidence_tier=self.required_evidence_tier,
        )


def unsigned_payment(
    amount: XrplAmount, destination: str, invoice: InvoiceBinding, ctx: LedgerContext
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """The unsigned, multisign-ready Payment the merchant's terms require, and every way it departs
    from X1's restricted template. X1's template is not changed; the gaps are reported.

    Shape follows t54's buyer SDK (x402-xrpl 0.3.4, presigned_payment_payer.py): no Flags (so never
    tfPartialPayment), no Paths or DeliverMin, and for an issued currency SendMax equal to Amount.
    """
    tx: dict[str, Any] = {
        "TransactionType": "Payment",
        "Account": ctx.source_account,
        "Destination": destination,
        "Amount": amount.wire(),
        "Fee": str(ctx.fee_drops),
        "Flags": 0,
        "Sequence": ctx.sequence,
        "LastLedgerSequence": ctx.last_ledger_sequence,
        "SigningPubKey": "",
    }
    gaps: list[str] = []
    if amount.currency != "XRP":
        tx["SendMax"] = amount.wire()
    else:
        gaps.append("X1 template carries only the tRLUSD issued asset, not XRP")
    if ctx.network_id > NETWORK_ID_FIELD_THRESHOLD:
        tx["NetworkID"] = ctx.network_id
    else:
        gaps.append("X1 template always sets NetworkID; this network refuses it")
    if invoice.invoice_id_field is not None:
        tx["InvoiceID"] = invoice.invoice_id_field
    if invoice.memo_data is not None:
        tx["Memos"] = [{"Memo": {"MemoData": invoice.memo_data}}]
    if invoice.source_tag is not None:
        tx["SourceTag"] = invoice.source_tag
    if invoice.destination_tag is not None:
        tx["DestinationTag"] = invoice.destination_tag
    gaps.extend(
        f"{name} not in X1 template" for name in sorted(set(tx) - X1_ALLOWED_PAYMENT_FIELDS)
    )
    return tx, tuple(gaps)


def _first_difference(
    actual: PaymentAuthorizationProposal, expected: PaymentAuthorizationProposal
) -> str | None:
    for item in fields(actual):
        if getattr(actual, item.name) != getattr(expected, item.name):
            return item.name
    return None


def verify_binding(proposal: PaymentAuthorizationProposal, terms: ProposalTerms) -> None:
    """Fail closed unless `proposal` is exactly the proposal `terms` produce.

    Two independent reconstructions, neither trusting a digest the proposal carries:

    1. From the proposal's own stated terms, rebuild the unsigned Payment, its digest, the
       idempotency key and the proposal ID. Any stored transaction, digest or identity that does not
       follow from the stated terms (a destination changed with the transaction bytes left alone, a
       mutated transaction with a recomputed digest, a replaced proposal ID) is refused.
    2. From the trusted `terms` (built from verified inputs), rebuild the whole proposal and require
       every field to be equal: agent, facility, purchase request, prompt, merchant, resource,
       method, request digest, requirement digest, invoice binding (ID, InvoiceID, memo, source and
       destination tags), amount, currency, issuer, destination, network, ledger context, expiry,
       evidence tier, transaction and digest, idempotency key and proposal ID.

    The first differing field is named in BINDING_ALTERED.
    """
    stated = _build(proposal.terms())
    field_name = _first_difference(proposal, stated)
    if field_name is not None:
        raise X402Refused(X402Refusal.BINDING_ALTERED, f"{field_name} does not follow from terms")
    expected = _build(terms)
    field_name = _first_difference(proposal, expected)
    if field_name is not None:
        raise X402Refused(X402Refusal.BINDING_ALTERED, f"{field_name} differs from verified terms")


def unsigned_digest(tx: dict[str, Any]) -> str:
    """SHA-512Half over the XRPL single-signing serialization of the unsigned transaction."""
    if tx.get("SigningPubKey") != "" or "TxnSignature" in tx or "Signers" in tx:
        raise X402Refused(X402Refusal.SUBMISSION_NOT_AUTHORIZED, "transaction is not unsigned")
    try:
        encoded = encode_for_signing(dict(tx))
    except Exception as exc:  # xrpl-py raises several codec error types
        raise X402Refused(X402Refusal.MALFORMED_CHALLENGE, "payment does not encode") from exc
    return hashlib.sha512(bytes.fromhex(encoded)).hexdigest()[:64].upper()


def _proposal_body(fields: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return {"protocol": PROPOSAL_PROTOCOL, "version": PROPOSAL_VERSION, **fields}


def digest_json(value: JsonValue) -> str:
    """sha256 over the Kessai canonical form (JCS (RFC 8785) + NFC)."""
    return "sha256:" + hashlib.sha256(canonicalize(value).data).hexdigest()


def build_proposal(
    *,
    intent: PurchaseIntent,
    merchant_id: str,
    resource_url: str,
    request_method: str,
    request_digest: str,
    requirement_digest: str,
    invoice: InvoiceBinding,
    amount: XrplAmount,
    destination: str,
    network: str,
    expires_at: Timestamp,
    ledger: LedgerContext,
    required_evidence_tier: EvidenceTier,
) -> PaymentAuthorizationProposal:
    """Build the proposal from its terms (see ProposalTerms). Callers outside tests should go through
    `credence.x402.prepare.prepare_proposal`, which derives the terms from verified inputs."""
    return _build(
        ProposalTerms(
            intent=intent,
            merchant_id=merchant_id,
            resource_url=resource_url,
            request_method=request_method,
            request_digest=request_digest,
            requirement_digest=requirement_digest,
            invoice=invoice,
            amount=amount,
            destination=destination,
            network=network,
            expires_at=expires_at,
            ledger=ledger,
            required_evidence_tier=required_evidence_tier,
        )
    )


def _build(t: ProposalTerms) -> PaymentAuthorizationProposal:
    """The idempotency key binds agent, purchase request, merchant, resource, request digest and
    requirement digest; the proposal ID binds every term, the ledger context and the transaction
    digest."""
    try:
        positive = Decimal(t.amount.value) > 0
    except InvalidOperation:
        positive = False
    if not positive:
        raise X402Refused(X402Refusal.MALFORMED_AMOUNT, "amount must be positive")
    tx, gaps = unsigned_payment(t.amount, t.destination, t.invoice, t.ledger)
    tx_digest = unsigned_digest(tx)
    idempotency_key = digest_json(
        {
            "agent_id": t.intent.agent_id,
            "purchase_request_digest": t.intent.purchase_request_digest,
            "merchant_id": t.merchant_id,
            "resource_url": t.resource_url,
            "request_digest": t.request_digest,
            "requirement_digest": t.requirement_digest,
        }
    )
    body = _proposal_body(
        {
            "agent_id": t.intent.agent_id,
            "facility_id": t.intent.facility_id,
            "purchase_request_digest": t.intent.purchase_request_digest,
            "prompt_digest": t.intent.prompt_digest,
            "merchant_id": t.merchant_id,
            "resource_url": t.resource_url,
            "request_method": t.request_method,
            "request_digest": t.request_digest,
            "requirement_digest": t.requirement_digest,
            "invoice": t.invoice.canonical(),
            "amount": t.amount.canonical(),
            "destination": t.destination,
            "network": t.network,
            "ledger": {
                "source_account": t.ledger.source_account,
                "network_id": t.ledger.network_id,
                "fee_drops": t.ledger.fee_drops,
                "sequence": t.ledger.sequence,
                "last_ledger_sequence": t.ledger.last_ledger_sequence,
            },
            "expires_at": t.expires_at.value,
            "unsigned_transaction_digest": tx_digest,
            "unsigned_transaction_digest_scheme": UNSIGNED_DIGEST_SCHEME,
            "idempotency_key": idempotency_key,
            "required_evidence_tier": t.required_evidence_tier.value,
            "payment_before_delivery": True,
            "x1_template_gaps": list(gaps),
            "submission_authorized": False,
        }
    )
    return PaymentAuthorizationProposal(
        proposal_id=digest_json(body),
        intent=t.intent,
        merchant_id=t.merchant_id,
        resource_url=t.resource_url,
        request_method=t.request_method,
        request_digest=t.request_digest,
        requirement_digest=t.requirement_digest,
        invoice=t.invoice,
        amount=t.amount,
        destination=t.destination,
        network=t.network,
        ledger=t.ledger,
        expires_at=t.expires_at,
        unsigned_transaction=tuple(sorted(tx.items())),
        unsigned_transaction_digest=tx_digest,
        unsigned_transaction_digest_scheme=UNSIGNED_DIGEST_SCHEME,
        idempotency_key=idempotency_key,
        required_evidence_tier=t.required_evidence_tier,
        payment_before_delivery=True,
        x1_template_gaps=gaps,
    )


class PaymentAuthorityBook:
    """One purchase, one payment authority, across all agents: offline and IN MEMORY ONLY.

    - A purchase request (its digest) may back at most one proposal, ever. A merchant re-challenge
      with a new invoice, a timeout, an unknown outcome or a rejection never reopens it:
      PURCHASE_ALREADY_AUTHORIZED. A new attempt needs a new signed purchase request and a new Core
      decision.
    - An invoice (network, destination, invoice ID) may back at most one proposal, by any agent:
      DUPLICATE_INVOICE.
    - Re-admitting a proposal already admitted is PROPOSAL_REPLAYED: look up the existing record.
    - A proposal is bound to exactly one expected signed-transaction hash, and a hash to exactly one
      proposal: TRANSACTION_ALREADY_BOUND. A validated hash confirms at most one purchase.

    Nothing is ever removed. This is not restart-safe and is not replay protection across processes:
    the state is lost on restart, and two books do not see each other. Durable uniqueness belongs to
    Core's reservation identity and the X1 payment journal (contracts/requests/0009, 0010), which
    X402-P2 does not change. Payment execution stays disabled.
    """

    def __init__(self) -> None:
        self._by_key: dict[str, PaymentAuthorizationProposal] = {}
        self._by_purchase: dict[str, str] = {}
        self._by_invoice: dict[str, str] = {}
        self._tx_by_proposal: dict[str, str] = {}
        self._proposal_by_tx: dict[str, str] = {}
        self._confirmed: dict[str, str] = {}

    @staticmethod
    def _invoice_key(proposal: PaymentAuthorizationProposal) -> str:
        if proposal.invoice.invoice_id is not None:
            return (
                f"invoice:{proposal.network}:{proposal.destination}:{proposal.invoice.invoice_id}"
            )
        return f"requirement:{proposal.requirement_digest}"

    def admit(self, proposal: PaymentAuthorizationProposal) -> None:
        if proposal.idempotency_key in self._by_key:
            raise X402Refused(X402Refusal.PROPOSAL_REPLAYED, proposal.idempotency_key)
        purchase = proposal.intent.purchase_request_digest
        if purchase in self._by_purchase:
            raise X402Refused(X402Refusal.PURCHASE_ALREADY_AUTHORIZED, purchase)
        invoice_key = self._invoice_key(proposal)
        if invoice_key in self._by_invoice:
            raise X402Refused(X402Refusal.DUPLICATE_INVOICE, "invoice already proposed")
        self._by_key[proposal.idempotency_key] = proposal
        self._by_purchase[purchase] = proposal.proposal_id
        self._by_invoice[invoice_key] = proposal.proposal_id

    def get(self, idempotency_key: str) -> PaymentAuthorizationProposal | None:
        return self._by_key.get(idempotency_key)

    def admitted(self, proposal: PaymentAuthorizationProposal) -> bool:
        return self._by_key.get(proposal.idempotency_key) == proposal

    def bind_transaction(
        self, proposal: PaymentAuthorizationProposal, transaction_hash: str
    ) -> None:
        if not self.admitted(proposal):
            raise X402Refused(X402Refusal.BINDING_ALTERED, "proposal was not admitted")
        bound = self._tx_by_proposal.get(proposal.proposal_id)
        if bound is not None and bound != transaction_hash:
            raise X402Refused(X402Refusal.TRANSACTION_ALREADY_BOUND, "proposal has another hash")
        owner = self._proposal_by_tx.get(transaction_hash)
        if owner is not None and owner != proposal.proposal_id:
            raise X402Refused(X402Refusal.TRANSACTION_ALREADY_BOUND, "hash bound to another")
        self._tx_by_proposal[proposal.proposal_id] = transaction_hash
        self._proposal_by_tx[transaction_hash] = proposal.proposal_id

    def confirm(self, proposal: PaymentAuthorizationProposal, transaction_hash: str) -> None:
        if self._tx_by_proposal.get(proposal.proposal_id) != transaction_hash:
            raise X402Refused(X402Refusal.TRANSACTION_ALREADY_BOUND, "not the bound hash")
        owner = self._confirmed.get(transaction_hash)
        if owner is not None and owner != proposal.proposal_id:
            raise X402Refused(X402Refusal.TRANSACTION_ALREADY_BOUND, "hash confirmed another")
        self._confirmed[transaction_hash] = proposal.proposal_id
