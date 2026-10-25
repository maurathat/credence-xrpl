# 0008. Integration I1c: inference delivery with atomic facility accounting

- Status: Accepted for the integration branch, amended by decision 0009 (Integration I1e security remediation). Core and Inference owner review of their contract changes is pending.
- Date: 2026-10-19
- Owner: Integration (San Antonio)
- Source: Maura's I1c authorization and approved architecture (I0 decisions D0–D6) of 2026-10-19. Requests: `contracts/requests/0001-core-settlement-delivery-attempts.md`, `contracts/requests/0002-inference-gateway-deliver.md`. Contract: `contracts/INTEGRATION-FLOW.md`.

## Decisions

1. **Core's persisted, accounting-settled chain is authoritative.**
   - The integrated gateway step (`Gateway.deliver`) chains each receipt to `SettlementCoordinator.chain_head`.
   - It never reads or moves the gateway's in-memory chain, and gateway-side verification never advances anything.
2. **Serialization.**
   - There is one in-flight purchase per facility, from reservation through accounting settlement or terminal reconciliation. An in-process per-facility lock enforces it, backed by the journal's in-flight check.
   - One process serves a database, enforced by an OS `flock` on `<database>.owner`, which the OS drops when its owner dies.
   - **Amended by decision 0009 §12 (I1k):** the lock is derived from the database's canonical, verified identity (`<canonical database>.owner`), not the caller's path spelling, so aliases cannot create a second owner.
3. **Durable intent before chargeable execution, as Core state.**
   - Core records a delivery attempt (`begin_delivery`) before any provider call.
   - The no-delivery release is permitted only when Core has no attempt for the reservation. The trusted evidence is Core's own record, not a caller's claim.
   - **Corrected in decision 0009:** in I1c this did not hold through every exposed path. A fabricated `DeliveryAttempt` passed to `Gateway.deliver`, or the standalone `Gateway.purchase` reached through the service, could call a provider with no Core attempt, after which a no-delivery release was falsely accepted. Decision 0009 enforces the boundary.
   - A signed gateway failure statement was not used, because it would need a new canonical object.
4. **Unknown outcomes fail closed.**
   - A provider failure or interruption after the attempt is `unknown`.
   - It is never retried, never charged and never released as failed. The hold lapses by the unchanged 600-second expiry.
5. **`CHAIN_BROKEN` is not terminal.**
   - It reflects Credence's sequencing, not the provider's delivery. The hold stays, and the dispute and exact evidence are kept.
   - This supersedes decision 0007 §4 for that reason only.
6. **Evidence is kept.**
   - Every authenticated rejection keeps its exact canonical evidence (`dispute_evidence`).
   - Authenticated late deliveries keep it in `late_delivery_disputes`, which is separate from the verifier-rejection rows. The C3B transcripts in `vectors/c3b/` therefore regenerate unchanged.
7. **The bare ledger `release` is refused for registered facilities**, like the bare `settle`.
8. **The response is released only after accounting settlement.** (In I1c, retrieval and acknowledgment were authorized by public key ID alone. Decision 0009 replaces that with an agent-signed authorization.)
   - It is retained in the journal, in the same database, until the authorized agent acknowledges it.
   - This is hackathon scope only: synthetic content, a `0600` file, and not production-grade confidentiality.
9. **The journal is in the same SQLite file as Core.**
   - Recovery reads one file, but journal and Core writes are separate transactions. No cross-transaction atomicity is claimed: each window is reconciled from Core's records on recovery.
10. **Decision numbering.**
    - Core's C3B decision is renumbered 0005 → 0007, and Inference 0005 and 0006 keep their numbers.
    - `scripts/check_decision_numbers.py` now fails CI on a duplicate number or a title that disagrees with its filename.
11. **CI.** `make test` now includes the offline integration tests (`tests/integration -m "not integration"`). The real-model tests stay opt-in.

## Consequences

- No canonical object schema, enum value or verifier reason-code precedence changed.
- No XRPL is involved. Accounting settlement is not XRPL settlement.
- Provider-side idempotency is unavailable, so a crash after the attempt can leave a paid provider execution outside Credence's accounting until reconciled by hand. It is reported as `unknown`; it is not hidden.
