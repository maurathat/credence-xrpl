# INTERFACE CHANGE REQUEST 0010: x402 prepayment and exact invoice binding in X1

- **Contract:** `contracts/XRPL-ADAPTER-CONTRACT.md` X1a/X1c (owner: XRPL Integration), the X1a `credence.signers.payment_authorization` statement and `credence.signers.policy_signer` (owner: Credence Core signers, X1a as authorized by Maura).
- **Requested by:** X402-P0 (`maurathat/credence-x402-adapter`, `credence/x402/`).
- **Status:** open. Nothing in X1 was changed. X1 is not in this branch's base (`726f643`); the X1 facts below were read, without modification, from the X1 workspace at `99ca646`.
- **Blocks:** turning an X402-P0 `PaymentAuthorizationProposal` into anything signable.

## What X402-P0 produces

`credence.x402.proposal.PaymentAuthorizationProposal` (schema in `contracts/X402-ADAPTER-CONTRACT.md`). It is unsigned, has `submission_authorized = False`, and carries the unsigned Payment and its `xrpl-stx-sha512half-v1` digest (X1a's scheme). `x1_template_gaps` lists every way that Payment departs from X1's template.

## Mismatches and requested changes

1. **The authorization statement is post-delivery.** X1a's `PaymentAuthorization` binds `receipt_digest`, `receipt_chain_head`, `verifier_decision_digest` and `accounting_settled_at`, which are facts that exist only after delivery and verification. An x402 merchant must be paid before it delivers, so none of these exist when the payment would be authorized.
   - *Requested:* a decision (Core and Maura) on whether a pre-delivery payment statement may exist at all. If it may, a separate statement type with its own domain string. It would bind the x402 fields: purchase request digest, request digest, requirement digest, invoice ID, amount/asset/issuer, destination, network, expiry and unsigned digest. Its policy-signer re-checks would run against Core's *reservation* rather than a settled receipt.
   - Depends on request 0009 (Core payment-before-delivery).
2. **Exact invoice binding.** X1's restricted Payment template (`_ALLOWED_PAYMENT_FIELDS`: Account, Amount, Destination, Fee, Flags, LastLedgerSequence, NetworkID, Sequence, Signers, SigningPubKey, TicketSequence, TransactionType, TxnSignature) cannot carry the invoice binding that t54's `exact` scheme requires. t54's scheme page: "the signed transaction MUST commit to the invoice": Memos (`MemoData = HEX(UTF-8(invoiceId))`) and/or `InvoiceID = SHA256(UTF-8(invoiceId))`. t54's SDK default and both validated Mainnet payments checked use both.
   - *Requested:* authorize exactly one field, `InvoiceID`, whose value must equal `SHA256(UTF-8(extra.invoiceId))` of the challenge bound in the statement. X1 would recompute it from the statement and refuse any other value.
   - Not requested: arbitrary `Memos`. Whether t54 accepts InvoiceID-only is open question Q4 (the generic x402 XRPL spec requires InvoiceID-only and forbids Memos; t54's SDK sends both). If t54 requires the memo, a second request will name exactly one memo whose `MemoData` is derived from the same invoice ID, and nothing else.
3. **Other fields an x402 Payment carries.** `SendMax` equal to `Amount` for an issued currency (t54 SDK always; whether mandatory is open question Q6), `SourceTag` when the merchant's `extra.sourceTag` states one, `DestinationTag` when `extra.destinationTag` states one. Not requested now; listed so the template change is decided with full knowledge.
4. **NetworkID.** X1a's builder always sets `NetworkID`. rippled refuses that field on networks with ID 1024 or lower (Mainnet 0, Testnet 1, Devnet 2), and the observed Mainnet payments carry none. This is irrelevant while only the local standalone network is approved, but must be fixed before any such network is.
5. **Who submits.** X1's journal assumes Credence submits through its own transport. With t54, the buyer hands a signed blob to the merchant, the merchant's facilitator submits it, and the merchant returns a `PAYMENT-RESPONSE` *claim*.
   - *Requested:* a journal state for "signed blob released to a third party". From that state, only a validated lookup by the blob's own hash may resolve the payment. A timeout or a missing response leaves it unknown, never failed, and never re-paid. `credence.x402.lifecycle` models this offline.
6. **Durable duplicate-invoice and replay guards.** `credence.x402.proposal.ProposalBook` is in memory. *Requested:* uniqueness on (network, destination, invoice ID) and on the proposal idempotency key in the journal.
7. **Multisignature.** X1a pays with the 2-of-2 SignerList (ResearchBot + policy signer). Whether t54's hosted `exact` verifier accepts a multisigned blob (`SigningPubKey: ""` plus `Signers`) is unverified (open question Q5). X402-P0 assumes nothing: `MULTISIGNATURE_UNKNOWN` keeps every t54 merchant non-executable.
8. **Validity window.** t54's SDK sets `LastLedgerSequence = latest_validated + ceil(maxTimeoutSeconds/5) + 2`. X1's configured window must fit inside the merchant's `maxTimeoutSeconds`. t54 does not support Tickets; X1 permits TicketSequence.
9. **Builder reuse.** On integration, X1a's `build_unsigned_payment` should replace `credence.x402.proposal.unsigned_payment`. X402-P0 built its own only because X1 is not in its base.
