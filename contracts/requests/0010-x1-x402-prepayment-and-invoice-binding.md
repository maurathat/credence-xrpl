# INTERFACE CHANGE REQUEST 0010: x402 prepayment and exact invoice binding in X1

- **Contract:** `contracts/XRPL-ADAPTER-CONTRACT.md` X1a/X1c (owner: XRPL Integration), the X1a `credence.signers.payment_authorization` statement and `credence.signers.policy_signer` (owner: Credence Core signers, X1a as authorized by Maura).
- **Requested by:** X402-P0, revised in X402-P2 (`maurathat/credence-x402-adapter`, `credence/x402/`).
- **Status:** open. Nothing in X1 was changed. X1 is not in this branch's base (`726f643`). The X1 facts below were read, without modification, from the X1 workspace at `99ca646`.
- **Blocks:** turning a `PaymentAuthorizationProposal` into anything signable, and any trusted payment outcome.

## What the adapter produces (X402-P2)

`credence.x402.proposal.PaymentAuthorizationProposal` (schema in `contracts/X402-ADAPTER-CONTRACT.md`):

- It is unsigned and has `submission_authorized = False`.
- Since P2 it is built only from `prepare.proposal_terms`, which are verified against the Core-evaluated purchase, the parsed challenge, the pinned endpoint and the approved execution.
- It carries its ledger context, the unsigned Payment and its `xrpl-stx-sha512half-v1` digest (X1a's scheme).
- `verify_binding(proposal, terms)` rebuilds it completely.
- `x1_template_gaps` lists every way the Payment departs from X1's template.

## Requested X1 changes (X402-P2 revision)

| # | Item | Requested |
| --- | --- | --- |
| X1 | **Verified Core reservation input** | X1a's `PaymentAuthorization` binds post-delivery facts (receipt digest, receipt-chain head, verifier decision, accounting settlement). A prepaid statement type with its own domain string should bind the Core **reservation ID** (request 0009 C3) and the proposal's verified terms: purchase request digest, request digest, requirement digest, invoice ID, amount/currency/issuer, destination, network, expiry, unsigned digest. Depends on 0009 C1/C3. |
| X2 | **Independent policy-signer recomputation** | Before signing, the policy signer recomputes the whole proposal from Core's reservation and the stored challenge terms (as `verify_binding` does). It must not trust the proposal's digest or ID, and it must refuse any difference. |
| X3 | **Network-specific `NetworkID`** | X1a's builder always sets `NetworkID`. rippled refuses that field on networks with ID 1024 or lower and requires it above. The template must follow the network. Irrelevant while only a local standalone network can be approved (`ApprovedExecution` now refuses IDs ≤ 1024), but required before any public network is. |
| X4 | **Exact transaction and invoice matching** | Authorize exactly one invoice-binding field, `InvoiceID = SHA256(UTF-8(extra.invoiceId))`, recomputed by X1 from the statement. Arbitrary `Memos` stay forbidden. If t54 requires the memo, a separate request will name exactly one memo derived from the same invoice ID. `SendMax = Amount` (issued currency), `SourceTag` and `DestinationTag` only as the merchant states them. The validated transaction must match the authorized unsigned digest with its signatures removed, and its InvoiceID, memo and tags. |
| X5 | **Durable uniqueness** | Journal uniqueness on: reservation ID (one payment per reservation), purchase request digest, (network, destination, invoice ID), and proposal idempotency key. These must survive restart, which the in-memory `PaymentAuthorityBook` cannot. |
| X6 | **Transaction-hash replay protection** | One signed transaction hash per reservation, and one reservation per hash, durably. A validated hash may confirm one purchase only. The hash is persisted before the signed blob leaves Credence. |
| X7 | **`LastLedgerSequence` bounds** | `LastLedgerSequence` must lie within the merchant's `maxTimeoutSeconds` (t54 SDK: `latest_validated + ceil(t/5) + 2`) and within X1's configured window. t54 does not support Tickets. |
| X8 | **Facilitator submission** | With t54, the merchant's facilitator, not Credence, submits the signed blob. Requested: a journal state for "signed blob released to a third party". From it, only validated lookup by the blob's own hash resolves the payment. The merchant's `PAYMENT-RESPONSE` and a Trustline deny or review are claims; a deny leaves the payment unknown until validated non-inclusion. |
| X9 | **Verified terminal outcomes** | Provide the `ValidatedEvidenceVerifier` the adapter's lifecycle now requires (X402-P2 default `NoTrustedEvidenceSource` accepts nothing). Required properties: authenticated validated-ledger evidence (ledger index and hash); success only as `tesSUCCESS` with the delivered amount from metadata; terminal failure only as a validated `tec…`; `ter`/`tel`/`tef`/`tem` and timeouts never terminal; and a new verified outcome for validated non-inclusion past `LastLedgerSequence` (proposed `PAYMENT_EXPIRED_UNAPPLIED`). |
| X10 | **Multisignature** | Whether t54's hosted `exact` verifier accepts a 2-of-2 multisigned blob is unverified. X1 must not assume it. |
| X11 | **Builder reuse** | On integration, X1a's `build_unsigned_payment` replaces `credence.x402.proposal.unsigned_payment`. |

## Conditions the offline adapter cannot fulfil without changing X1 or the signers

- **Authenticated validated-ledger evidence (X9).** The adapter defines the verifier interface, and tests use a mock that authenticates only what it issued. No real verifier exists in this branch, so no payment can be confirmed or rejected outside tests.
- **Durable uniqueness and hash replay protection across restarts (X5, X6).**
- **A signable prepaid authorization statement, and policy-signer recomputation (X1, X2).**
- **Facilitator-submitted payment states (X8).**
