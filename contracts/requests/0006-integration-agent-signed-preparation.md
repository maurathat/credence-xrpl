# INTERFACE CHANGE REQUEST 0006: agent-signed purchase preparation (CR-1)

- **Contract:** `contracts/INTEGRATION-FLOW.md` (owner: Integration).
- **Requested by:** I2a offline integration (`maurathat/credence-i2a-offline`), from the I2-0 preflight CR-1.
- **Status:** implemented provisionally on the I2a branch as authorized by Maura's I2a brief (2026-10-20). Integration, Core and security review required before acceptance. I1h (Inference acceptance) is still outstanding.
- **Blocked:** remote (HTTP) agents purchasing through the product API.

## Change

1. `IntegrationService.prepare(market, agent_key_id=, prompt_digest=, idempotency_key=None) -> Preparation | PurchaseRefused`: Core qualification and the unchanged routing rule over the unsigned requests (ephemeral sandbox key, real holds and nonces), then a durable, immutable `integration_preparations` row bound to the agent key and subject, facility, market digest, offer position, exact canonical request, prompt digest and a 300-second expiry. Nothing is journaled or reserved; no provider is called.
2. `IntegrationService.submit_prepared(market, preparation_id=, signature=, prompt=, agent_key_id=)`: refuses any change (`PROMPT_MISMATCH`, `REQUEST_MISMATCH`, `WRONG_AGENT`, `AGENT_NOT_AUTHORIZED`, `AGENT_REVOKED`, `BAD_SIGNATURE`, `PREPARATION_EXPIRED`, `PREPARATION_STALE`, `PREPARATION_CONSUMED`, `UNKNOWN_PREPARATION`) before any journal entry; never substitutes another offer; then the prepared request with the agent's signature enters the unchanged lifecycle (Core re-verifies, reserves, checks nonce, spending and the delivery attempt). Identical resubmission returns the journaled operation, across restarts.
3. `purchase` and `prepare` share one request builder, `purchase_requests`. The legacy `purchase` behaviour is unchanged (the P0 transcripts regenerate byte-identically).
4. New `Refusal` values listed in the contract. A `no_qualifying_offer` entry from the submit path records the verified agent key.

Unchanged: C3A canonical format and signature definitions, Kessai serialization, Core policy, reservation, settlement and verifier.
