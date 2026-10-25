# INTERFACE CHANGE REQUEST 0009: prepaid x402 procurement in Core

- **Contract:** `contracts/CORE-OBJECTS.md`, the policy engine, the reservation ledger and settlement (owner: Credence Core).
- **Requested by:** X402-P0, revised in X402-P2 (`maurathat/credence-x402-adapter`, `credence/x402/`).
- **Status:** open. Nothing in Core was changed. X402-P0/P2/P3 work around the gaps below with an isolated, labelled projection, consistency recomputation and refusals. None of that authenticates Core state. The adapter remains non-paying. X402-P3D added C13 and C14 and the full dependency list below.
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
| C10 | **Merchant namespace** | Reserve the provider-ID prefix `x402.` for x402 merchants. No non-x402 provider may use it. An x402 provider ID must be endpoint-bound, `x402.<host>.<digest(url, method, network, payTo)>`, with a digest of **at least 128 bits** of collision resistance (at least 32 hex digits of SHA-256). Core does not reserve the namespace today. **The adapter does not yet meet the 128-bit floor:** `credence.x402.prepare.x402_provider_id` uses 12 hex digits (48 bits). Raising it changes provider identities and needs a separately authorized x402 code change. |
| C11 | **Tier-1 receipt shape for x402** | A delivery receipt for a merchant Credence witnessed after prepayment. It binds the purchase request, the payment transaction hash, the request digest and the response digest. There is no provider attestation (Tier 1). |
| C12 | **Signed or recomputable decisions** | `DecisionRecord` is unsigned and does not bind headroom, nonce or evaluation-time sourcing. X402-P2 only checks it for consistency by recomputing it with Core's pure `evaluate` on caller-supplied inputs. That is not authentication of Core state. Requested: either a Core-signed decision bound to the reservation ID, or Core-held reservation state the adapter can read. |
| C13 | **Authenticated facility terms at registration** | Authenticate the full facility terms, or a binding digest of them, when the facility is registered. The terms are limit, drawn balance, status, provider allow-list, borrower account, minimum evidence tier, maturity and allowed procurement classes. Every purchase decision and reservation must bind that authenticated facility digest. Today the agent's signed purchase request binds only the facility ID; the facility object reaching the policy engine is whatever the caller passes. |
| C14 | **Authoritative sourcing of decision inputs** | Core, not the caller, must source every input of a prepaid decision: nonce-use status, spending headroom (reserved and drawn), the key registry and revocation state, facility terms (C13), procurement-class configuration, and evaluation time. Downstream consumers (the x402 adapter, X1) must be able to verify that a decision was made on those Core-sourced inputs. |

## Unresolved dependencies: what the adapter cannot authenticate without changing Core

`prepare_proposal` only checks these for consistency. Each comes from the caller or an external party, and none is verified against an authoritative Core source:

| # | Dependency | Current adapter behaviour | Requires |
| --- | --- | --- | --- |
| D1 | **Facility and procurement-class terms** (limit, drawn balance, status, provider allow-list, borrower account, minimum tier, maturity, allowed procurement classes) | Taken from the caller's `Purchase`. Core's recomputed decision is consistent with them; the request signature binds only the facility ID. | C13, C14 |
| D2 | **Time**: evaluation time, current time, every expiry and validity boundary (quote, purchase request, facility maturity, challenge window) | The evaluation time is the supplied record's own `decided_at`; `now` is a parameter. | C14 |
| D3 | **Spending headroom and nonce use** | The caller's `currently_reserved` and `nonce_already_used`. A fact that would cause rejection is caught; an understated one that turns a rejection into an acceptance is not, because the decision record does not bind these facts. | C3, C8, C12, C14 |
| D4 | **Key registry and revocation** | The caller's `KeyRegistry`. The signature check is only as good as that registry. | C14 |
| D5 | **Procurement configuration** | The procurement classes travel with the caller's `Purchase` and are checked only against the supplied facility's digests. | C13, C14 |
| D6 | **Approved execution and facilitator profile** (including multisig support) | Caller configuration. `ApprovedExecution` refuses public network IDs; the facilitator's multisig claim is asserted (UNKNOWN for t54); no supported 2-of-2 multisignature is assumed. | 0010 X10, operator configuration authority |
| D7 | **Ledger context** (sequence, fee, network ID, `LastLedgerSequence`, account state) | Caller-supplied. Only the network ID (against the approved execution) and the source account (against the supplied facility) are checked. | 0010 (X1 trusted ledger reads) |
| D8 | **Pinned merchant endpoint and merchant challenge** | Caller objects. The x402 identity is derived from the pin, and challenge terms are merchant claims. | C10 (namespace), discovery provenance |
| D9 | **Location, model and service-class claims** (jurisdiction, residency, model, family, context) | Rebuilt from the purchase's own service class and profile: self-consistency, not operator configuration. | Operator-asserted configuration source (new; not yet a Core object) |
| D10 | **Durable one-payment-per-purchase** | In-memory `PaymentAuthorityBook`, lost on restart. | C3, C4 |
| D11 | **Prepaid exposure, obligation and receipt accounting** | Not modelled in Core. | C5, C6, C7, C11 |
| D12 | **Namespace reservation and collision resistance** | Adapter-only prefix check; 48-bit digest (below C10's 128-bit floor). | C10, plus an x402 code change |

A proposal prepared on these inputs is not evidence of an authentic Core decision, reservation or payment authority.
