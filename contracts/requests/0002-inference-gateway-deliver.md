# INTERFACE CHANGE REQUEST 0002: gateway delivery step separated from accounting settlement

- **Contract:** `contracts/INFERENCE-GATEWAY-CONTRACT.md` (owner: Inference).
- **Requested by:** Integration (San Antonio), Integration I1c.
- **Status:** authorized by Maura on 2026-10-19 for the integration branch `maurathat/credence-integration-i0` (I1c authorization §2, "Inference"). Inference review of the merged result is still required.
- **Blocked:** the integrated purchase lifecycle (`contracts/INTEGRATION-FLOW.md`).

## Constraints kept

- No canonical object schema changes, and no change to verifier precedence.
- `Gateway.purchase`, `select`, `purchase_best` and `verify_delivery`, the `GatewayOutcome` union and every existing provider adapter keep their current behaviour. These are the standalone interfaces.
- No live Nebius or Ollama call in any test.

## Changes

1. **`Gateway.deliver(purchase, reserved, attempt, *, previous_receipt_digest)`.** This is a delivery step with no reservation and no settlement. It runs only for a reservation Core has already approved and for which Core recorded a `DeliveryAttempt`.
   - It calls the provider, then builds and signs the evidence exactly as `purchase` does.
   - It returns `ProviderFailed` or `DeliveryCaptured`.
   - It does **not** run the verifier, does **not** move any receipt chain, and its outcome does **not** mean the response may be released.
2. **The predecessor is supplied by the caller.** `previous_receipt_digest` is Core's persisted chain head (`SettlementCoordinator.chain_head`). The gateway's in-memory `ReceiptChain` is neither read nor advanced by `deliver`.
3. **`DeliveryCaptured`** is a new outcome type outside `GatewayOutcome`. It holds the reservation, the accepting policy decision, the evidence and the exact response bytes. It is not "verified".
4. **`Gateway.connected(provider_id)`** is a read-only check. It lets the caller refuse before any delivery attempt when no provider is connected.
5. **`Gateway.check_prompt(purchase)`** exposes the existing prompt check as a step of its own, returning `Refused` or `None`.
