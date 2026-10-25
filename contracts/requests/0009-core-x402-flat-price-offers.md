# INTERFACE CHANGE REQUEST 0009: prepaid x402 procurement in Core

- **Contract:** `contracts/CORE-OBJECTS.md`, the policy engine, the reservation ledger and settlement (owner: Credence Core).
- **Requested by:** X402-P0, revised in X402-P2 (`maurathat/credence-x402-adapter`, `credence/x402/`).
- **Status:** open. Nothing in Core was changed. X402-P0/P2 work around the gaps below with an isolated, labelled projection, recomputation and refusals. The adapter remains non-paying.
- **Blocks:** any x402 purchase leaving the read-only/offline state, billing or verifying an x402 delivery, and any restart-safe payment authority.

## Mismatches found (X402-P0)

1. **Per-request price.** `credence.quote` prices per million input and output tokens with token caps. An x402 merchant charges one fixed amount per request.
   - *Workaround:* `procurement.project` builds a *policy projection*: input price = the flat amount per 1,000,000 units, input cap 1,000,000, output 0/0.
   - Core's `max_total_cost_value` of the projection equals the flat amount exactly. Since P2, `prepare_proposal` re-projects the quote and requires Core's approved maximum, its reservation amount, the projected price and the merchant amount to be one integer.
   - The projection is marked `billing_faithful = False` and must never reach billing or the verifier.
2. **Payment before delivery.** Core reserves on accept and settles after the verifier passes ("Providers are paid after verification"). An x402 merchant is paid before it serves.
   - *Claims note for Maura:* that build-plan line does not hold for an x402 merchant. Nothing violates it today because nothing is paid.
3. **Money unit.** `MicroTrlusd` means tRLUSD, the test token. RLUSD on Mainnet, or XRP, is never converted. It is refused before Core is asked.
4. **Location facts.** These are operator assertions only. Without them Core correctly returns `PROFILE_NOT_ALLOWED`.
5. **Evidence.** An x402 payment adds no evidence. An x402 offer is at most Tier 1 (`WITNESSED`).

## Requested Core changes (X402-P2 revision)

| # | Item | Requested |
| --- | --- | --- |
| C1 | **Separate prepaid procurement contract** | A distinct procurement mode for "pay before delivery". The postpaid contract stays as it is. A prepaid purchase must be an explicit facility and procurement-class permission, never a reinterpretation of a postpaid quote. |
| C2 | **Exact flat-price quote and accounting** | A quote form `price_model: per_request` with `price_micro_trlusd`. Billing charges exactly that amount. The verifier checks `BILLED_AMOUNT_MISMATCH` against it and applies no token-cap check unless the merchant states caps. This retires the projection. |
| C3 | **Durable reservation identity** | A reservation ID derived from (facility, purchase request digest), persisted at accept. Every downstream payment authorization must name it. Today `qualify_and_select` is a dry run and nothing durable exists. |
| C4 | **One payment per reservation** | At most one payment authorization per reservation, ever, enforced durably in Core's store. A merchant re-challenge, a timeout or an unknown outcome must not open a second one. X402-P2's `PaymentAuthorityBook` enforces this only in memory. |
| C5 | **Exposure at validated payment** | Facility exposure (`drawn`) increases when the payment is validated on ledger, not at delivery. A reserved-but-unpaid amount stays reserved until validated success, validated `tec` failure, or validated non-inclusion past `LastLedgerSequence`. |
| C6 | **Outstanding delivery obligation** | A state recording that a merchant was paid and owes delivery. It counts against the facility until delivered, refunded or written off by an operator. It is never released automatically and never triggers repayment. |
| C7 | **Receipt-chain semantics** | How a prepaid purchase enters the facility's receipt chain: the payment fact first, then the delivery receipt referencing it. A paid-but-undelivered purchase must be visible in the chain without a delivery receipt. |
| C8 | **Facility concurrency** | Concurrent prepaid reservations against one facility must be serialized as postpaid ones are (headroom and nonce in one transaction). The adapter cannot do this: it receives headroom and nonce facts from the caller. |
| C9 | **Network fee treatment** | Whether the XRPL fee (drops, paid by the agent account) counts against the facility, and in which unit. It is not tRLUSD, and today it is outside Core accounting. |
| C10 | **Merchant namespace** | Reserve the provider-ID prefix `x402.` for x402 merchants. No non-x402 provider may use it, and an x402 provider ID must be the endpoint-bound `x402.<host>.<digest(url, method, network, payTo)>` that X402-P2 derives. Core does not reserve the namespace today. |
| C11 | **Tier-1 receipt shape for x402** | A delivery receipt for a merchant Credence witnessed after prepayment. It binds the purchase request, the payment transaction hash, the request digest and the response digest. There is no provider attestation (Tier 1). |
| C12 | **Signed or recomputable decisions** | `DecisionRecord` is unsigned and does not bind headroom or nonce facts. X402-P2 makes it authoritative only by recomputing it with Core's pure `evaluate`. Requested: either a Core-signed decision bound to the reservation ID, or Core-held reservation state the adapter can read. |

## Conditions the offline adapter cannot fulfil without changing Core

- **Trusted headroom and nonce facts.** `prepare_proposal` re-runs `evaluate` with the caller's `currently_reserved` and `nonce_already_used`. A caller can be caught when a supplied fact would make Core reject, because the recomputed record then differs. A caller that understates headroom to turn a rejection into an acceptance is not detectable, because the decision record does not bind those facts. Requires C3/C8/C12.
- **Restart-safe one-payment-per-purchase.** Requires C3/C4. The in-memory book is lost on restart.
- **Exposure, obligation and receipt accounting for prepayment.** Requires C5/C6/C7/C11.
- **Namespace reservation across the whole system.** Requires C10.
