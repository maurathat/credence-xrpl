# INTERFACE CHANGE REQUEST 0009: x402 flat-price offers in Core procurement

- **Contract:** `contracts/CORE-OBJECTS.md` and the policy engine (owner: Credence Core).
- **Requested by:** X402-P0 (`maurathat/credence-x402-adapter`, `credence/x402/`).
- **Status:** open. Nothing in Core was changed. X402-P0 works around the gaps below with an isolated, labelled projection and refusals.
- **Blocks:** any x402 purchase leaving the read-only/offline state; billing or verifying an x402 delivery.

## Mismatches found

1. **Per-request price.** `credence.quote` prices per million input and output tokens with token caps. An x402 merchant charges one fixed amount per request, whatever the token counts. There is no faithful `Quote` for it.
   - *X402-P0 workaround:* `credence.x402.procurement.project` builds a **policy projection**: input price = the flat amount per 1,000,000 units, input cap 1,000,000, output price 0, output cap 0. Core's `max_total_cost_value` of that quote equals the flat amount exactly (asserted in code), so `PRICE_ABOVE_MAX` and `OVER_LIMIT` are decided on the true price. The projected caps are not the merchant's token limits; the projection is marked `billing_faithful = False` and is never passed to the gateway, billing or the verifier.
   - *Requested:* a quote form for a fixed per-request price (for example `price_model: per_request` with `price_micro_trlusd`), with matching billing and verifier rules (`BILLED_AMOUNT_MISMATCH` against the fixed price, no token-cap check unless the merchant states caps).
2. **Payment before delivery.** Core reserves on accept and settles after the verifier passes ("Providers are paid after verification"). An x402 merchant must be paid before it serves. Core has no state for "paid, not yet delivered", and no rule for a paid purchase whose delivery fails or is inadequate.
   - *X402-P0 workaround:* the separate offline model `credence.x402.lifecycle` (not Core's settlement lifecycle).
   - *Requested:* Core's decision on whether a facility may pay before verification at all, and if so the accounting states (reserved → prepaid → delivered/verified or prepaid-undelivered), and how a prepaid-undelivered amount counts against the facility limit.
   - *Claims note for Maura:* the build plan's line "Providers are paid after verification" does not hold for an x402 merchant. X402-P0 makes no payment, so nothing violates it today. Any later x402 payment needs this claim changed or scoped. This checkpoint does not change it.
3. **Money unit.** `MicroTrlusd` is the only money type and means tRLUSD, the test token. A merchant asking for RLUSD on XRPL Mainnet, or XRP, cannot be evaluated by the policy engine without silently treating another asset as tRLUSD.
   - *X402-P0:* never converted. Such offers are refused before Core is asked (`ASSET_NOT_APPROVED`, `ISSUER_NOT_APPROVED`, `MAINNET_READ_ONLY`) and shown read-only.
   - *Requested:* nothing now. This is noted so that no later change introduces an implicit conversion.
4. **Location facts.** The procurement matcher requires an asserted jurisdiction and a non-empty data residency. x402 listings do not state them. X402-P0 accepts them only as operator assertions (`OperatorAssertions`); without them Core correctly returns `PROFILE_NOT_ALLOWED`. No change requested; recorded so nobody fills these from directory metadata.
5. **Evidence.** An x402 payment adds no evidence. With no merchant attestation that Credence can verify, an x402 offer is at most Tier 1 (`WITNESSED`). No change requested.
