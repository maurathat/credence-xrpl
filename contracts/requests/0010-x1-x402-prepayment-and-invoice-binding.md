# INTERFACE CHANGE REQUEST 0010: x402 prepayment and exact invoice binding in X1

- **Contract:** `contracts/XRPL-ADAPTER-CONTRACT.md` X1a/X1c (owner: XRPL Integration), the X1a `credence.signers.payment_authorization` statement and `credence.signers.policy_signer` (owner: Credence Core signers, X1a as authorized by Maura).
- **Requested by:** X402-P0, revised in X402-P2 (`maurathat/credence-x402-adapter`, `credence/x402/`).
- **Status:** open. Nothing in X1 was changed. X1 is not in this branch's base (`726f643`). The X1 facts below were read, without modification, from the X1 workspace at `99ca646`.
- **Blocks:** turning a `PaymentAuthorizationProposal` into anything signable, and any trusted payment outcome.

## What the adapter produces (X402-P2)

`credence.x402.proposal.PaymentAuthorizationProposal` (schema in `contracts/X402-ADAPTER-CONTRACT.md`):

- It is unsigned and has `submission_authorized = False`.
- Since P2 it is built only from `prepare.proposal_terms`. Those terms are *consistency-checked* against the caller-supplied Core-evaluated purchase, parsed challenge, pinned endpoint, approved execution and ledger context. None of these is authenticated Core state (request 0009 D1–D12).
- It carries its ledger context, the unsigned Payment and its `xrpl-stx-sha512half-v1` digest (X1a's scheme).
- `verify_binding(proposal, terms)` rebuilds it completely.
- `x1_template_gaps` lists every way the Payment departs from X1's template.

## What X1 must not treat as payment authority (X402-P3D)

X1 cannot treat any of these, alone or together, as authority to sign or pay:

- a `PaymentAuthorizationProposal`;
- an `inference.market.Qualification`;
- a Core `DecisionRecord` or `PolicyDecision`;
- a `PaymentAuthorityBook` admission.

They show internal consistency with caller-supplied inputs, nothing more.

Before signing, X1 (the settlement authority and the policy signer) must verify, from its own trusted sources:
- a **durable Core reservation** for this purchase (request 0009 C3/C4);
- the **authenticated facility terms** or their binding digest (0009 C13);
- the **nonce** and **spending headroom** as Core holds them (0009 C14);
- the **exact amount** in the reservation;
- the relevant **authorization state**: agent key current and not revoked, facility active and unexpired, and no other payment for the reservation.

It must then recompute the proposal from those facts (X2), using ledger state it read itself (X12).

**A durable reservation is not sufficient authorization (X402-P3E).** A Core reservation (0009 C3/C4) records that Core accepted a purchase. It does not by itself cure:

- substituted facility terms;
- a forged or stale key registry;
- stale authorization state (a key revoked, a facility frozen, matured or defaulted after the reservation);
- manipulated procurement constraints.

The reservation may have been made on such inputs, or the facts may have changed since. Before signing, X1 must authenticate the relevant Core state independently, at signing time. That state is:

- the facility's registered authoritative terms or binding digest (0009 C13), including the provider allow-list and allowed procurement classes;
- the procurement-class configuration those digests name;
- the key registry and the agent key's current revocation status;
- the facility's current status and maturity;
- Core-sourced nonce and headroom (0009 C14).

A reservation that does not match the authenticated state is refused, not repaired.

## Requested X1 changes (X402-P2 revision)

| # | Item | Requested |
| --- | --- | --- |
| X1 | **Verified Core reservation input** | X1a's `PaymentAuthorization` binds post-delivery facts (receipt digest, receipt-chain head, verifier decision, accounting settlement). A prepaid statement type with its own domain string should bind the Core **reservation ID** (request 0009 C3) and the proposal's terms as X1 itself recomputes them (X2), never the proposal's stated values: purchase request digest, request digest, requirement digest, invoice ID, amount/currency/issuer, destination, network, expiry, unsigned digest. Depends on 0009 C1/C3. |
| X2 | **Independent policy-signer recomputation** | Before signing, the policy signer recomputes the whole proposal from Core's reservation and the stored challenge terms (as `verify_binding` does). It must not trust the proposal's digest or ID, and it must refuse any difference. |
| X3 | **Network-specific `NetworkID`** | X1a's builder always sets `NetworkID`. rippled refuses that field on networks with ID 1024 or lower and requires it above. The template must follow the network. Irrelevant while only a local standalone network can be approved (`ApprovedExecution` now refuses IDs ≤ 1024), but required before any public network is. |
| X4 | **Exact transaction and invoice matching** | Authorize exactly one invoice-binding field, `InvoiceID = SHA256(UTF-8(extra.invoiceId))`, recomputed by X1 from the statement. Arbitrary `Memos` stay forbidden. If t54 requires the memo, a separate request will name exactly one memo derived from the same invoice ID. `SendMax = Amount` (issued currency), `SourceTag` and `DestinationTag` only as the merchant states them. The validated transaction must match the authorized unsigned digest with its signatures removed, and its InvoiceID, memo and tags. |
| X5 | **Durable uniqueness** | Journal uniqueness on: reservation ID (one payment per reservation), purchase request digest, (network, destination, invoice ID), and proposal idempotency key. These must survive restart, which the in-memory `PaymentAuthorityBook` cannot. |
| X6 | **Transaction-hash replay protection** | One signed transaction hash per reservation, and one reservation per hash, durably. A validated hash may confirm one purchase only. The hash is persisted before the signed blob leaves Credence. |
| X7 | **`LastLedgerSequence` bounds** | `LastLedgerSequence` must lie within the merchant's `maxTimeoutSeconds` (t54 SDK: `latest_validated + ceil(t/5) + 2`) and within X1's configured window. t54 does not support Tickets. |
| X8 | **Facilitator submission** | With t54, the merchant's facilitator, not Credence, submits the signed blob. Requested: a journal state for "signed blob released to a third party". From it, only validated lookup by the blob's own hash resolves the payment. The merchant's `PAYMENT-RESPONSE` and a Trustline deny or review are claims; a deny leaves the payment unknown until validated non-inclusion. |
| X9 | **Verified terminal outcomes** | Provide the `ValidatedEvidenceVerifier` the adapter's lifecycle now requires (X402-P2 default `NoTrustedEvidenceSource` accepts nothing). Required properties: authenticated validated-ledger evidence (ledger index and hash); success only as `tesSUCCESS` with the delivered amount from metadata; terminal failure only as a validated `tec…`; `ter`/`tel`/`tef`/`tem` and timeouts never terminal; and a new verified outcome for validated non-inclusion past `LastLedgerSequence` (proposed `PAYMENT_EXPIRED_UNAPPLIED`). |
| X10 | **Multisignature** | Whether t54's hosted `exact` verifier accepts a 2-of-2 multisigned blob is unverified. X1 must not assume it. **Offline limitations (X402-P3E):** the adapter assembles no multisignature. It builds only the unsigned, multisign-ready Payment (empty `SigningPubKey`, no `Signers`) and its digest. It verifies no signer, `SignerList`, weights, quorum, `RegularKey` or master-key state. Its `FacilitatorProfile.multisig` is an assertion: UNKNOWN for t54, and SUPPORTED only as test configuration. All multisignature assembly and verification, and any facilitator acceptance evidence, belong to X1a. A compatibility decision without a multisignature reason is not evidence that a 2-of-2 payment would be accepted. An offline proposal is prepared even when multisig support is UNKNOWN or UNSUPPORTED. **X10 must be satisfied independently, with verified facilitator acceptance of Credence's 2-of-2 multisigned Payment, before any Credence-authorized x402 payment is signed or released.** |
| X11 | **Builder reuse** | On integration, X1a's `build_unsigned_payment` replaces `credence.x402.proposal.unsigned_payment`. |
| X12 | **Authoritative ledger reads and fee bounds (X402-P3E)** | Before signing, X1 reads from its own trusted, pinned validated-ledger source, never from the proposal's `LedgerContext` or any caller: (a) the paying account's state, including existence, balance and trust line for the asset, flags, `RegularKey`, `SignerList` and current `Sequence` (or usable Ticket); (b) network identity and ledger context, meaning network ID, the validated ledger index and hash, and server identity as X1c pins it; (c) the current network fee, checked against an **independently authorized maximum fee** that comes from X1 or operator configuration, not from the proposal or merchant (t54 also rejects fees above 0.01 XRP); (d) `LastLedgerSequence` and ledger expiry, so that the transaction's `LastLedgerSequence` is beyond the validated ledger and within both the merchant's `maxTimeoutSeconds` and X1's window (X7); (e) any further state needed to validate the payment before signing, such as destination existence, a destination tag requirement and the issuer's trust line or authorization. Any proposal field that disagrees with these reads is refused. The adapter implements none of these reads or bounds. |

## Conditions the offline adapter cannot fulfil without changing X1 or the signers

- **Authenticated validated-ledger evidence (X9).** The adapter defines the verifier interface, and tests use a mock that authenticates only what it issued. No real verifier exists in this branch, so no payment can be confirmed or rejected outside tests.
- **Durable uniqueness and hash replay protection across restarts (X5, X6).**
- **A signable prepaid authorization statement, and policy-signer recomputation (X1, X2).**
- **Facilitator-submitted payment states (X8).**
- **Authoritative ledger reads and an independently authorized maximum fee (X12).** The adapter's `LedgerContext` (sequence, fee, network ID, `LastLedgerSequence`) is caller-supplied. Only its network ID and source account are cross-checked, against caller-supplied objects.
- **Multisignature assembly and verification, and verified facilitator acceptance (X10).**
- **Runtime terminology (next authorized code change).** The adapter's `verify_binding` refusal detail says "differs from verified terms", but the check is consistency against supplied terms, not authentication. It is recorded as a known defect in the x402 contract, to be corrected in the next separately authorized executable-code change. X1 must not read it as verification.
- **Independent authentication of Core state at signing time, beyond a reservation (see above).**
