# INTERFACE CHANGE REQUEST 0003: enforce authorized execution and close the bare reserve

- **Contracts:** `contracts/SETTLEMENT-COORDINATOR.md` and `contracts/POLICY-VERIFIER.md` (owner: Credence Core).
- **Requested by:** Integration (San Antonio), Integration I1e, remediating Core findings C-1, C-2 and C-4 and Inference findings I-1 and I-2 against `cfc6224`.
- **Status:** authorized by Maura on 2026-10-19 for `maurathat/credence-integration-i0`. Core review of the remediation commit is required before acceptance.
- **Blocked:** acceptance of the integration branch.

## Changes

1. **`settle` requires the attempt.** It requires Core's persisted delivery attempt for the reservation and request, checked after `WRONG_PURCHASE` and before the verifier. Refusals: `NO_DELIVERY_ATTEMPT`, `ATTEMPT_MISMATCH`.
2. **Late deliveries too.** A late delivery with no matching attempt commits the expiry release, records nothing, and is refused `NO_DELIVERY_ATTEMPT` or `ATTEMPT_MISMATCH`.
3. **`claim_execution(DeliveryAttempt, now)`.** A one-shot claim on the persisted attempt.
   - Refusals: `NO_DELIVERY_ATTEMPT`, `ATTEMPT_MISMATCH`, `DELIVERY_OUTCOME_UNKNOWN`, `EXECUTION_ALREADY_CLAIMED`, `NOT_HELD`, `EXPIRED`.
   - New operational column: `delivery_attempts.execution_claimed_at`.
4. **The bare reserve is closed.** `ReservationLedger.reserve` and `reserve_in_transaction` refuse registered facilities (`USE_SETTLEMENT_COORDINATOR`). The coordinator uses the internal `_reserve_accounted` within its own transaction.
5. **Documentation.**
   - The contract states the enforced boundary instead of claiming that absence of an attempt proves no provider is owed.
   - Decision 0007 receives supersession notes.

Unchanged: canonical objects, C3A signatures, policy, billing, verifier precedence, settlement atomicity and fault points, and the C3B vectors (byte-identical).
