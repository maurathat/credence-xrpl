# 0016. XRPL X1n: benign late completion after finalization

- Status: X1n blocked by Salvador finding X1o-E1; X1o remediation awaits independent review.
- Date: 2026-10-25
- Scope: XRPL X1n, R1 only. Base `4fa226a` (X1m). Amends decision 0015.
- Source: Maura's X1n brief; Salvador's X1m ACCEPT with non-blocking R1.

## Decisions

1. **The benign case.** A live lease owner may finish after a trusted finalizer has closed its attempt. If the attempt was closed by terminal validated-ledger finalization and the caller's persisted transaction hash, lease identity and terminal outcome all agree, completion is a no-write, no-exception observation. The caller then reads the existing terminal record.
2. **Fencing.** The check is read-only and runs only after the owner-fenced write has matched no open attempt. Every other mismatch still raises `JournalConflict`. Fencing of attempts not closed by finalization is unchanged.
3. **Lease liveness.** Liveness is judged at the moment finalization closed the attempt. An attempt closed by finalization only after its lease expired, or abandoned by lease expiry, stays fenced. Once the closure happened under a live lease, a late call after expiry is still benign, because nothing is written.
4. **Scope of terminal states.** The benign path covers only terminal states that rest on validated inclusion evidence: `VALIDATED_SUCCESS`, `VALIDATED_FAILURE` and `VALIDATED_UNEXPECTED_DELIVERY`. `EXPIRED_NOT_INCLUDED` keeps the conflict. That is the conservative choice for R1.
5. **Transaction identity is explicit.** `finish_submission` takes the caller's `transaction_hash`. `PaymentService` always passes the persisted hash. Callers that omit it keep the previous conflict behaviour.
6. **The verification marker is not evidence.** X1o re-runs the authoritative `PaymentResultVerifier` over the persisted evidence and requires its exact disposition, result, detail, ledger facts and delivered amount, plus `classify_payment`, to agree with the terminal record. Empty, malformed, differently bound or contradictory evidence keeps the late owner fenced.

## Consequences

No schema, outbox, event-identity, lease or Core interface changes. Journal schema version 4 is unchanged.
