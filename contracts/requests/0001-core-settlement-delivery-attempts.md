# INTERFACE CHANGE REQUEST 0001: settlement coordinator delivery attempts, no-delivery release and dispute evidence

- **Contract:** `contracts/SETTLEMENT-COORDINATOR.md` (owner: Credence Core). The ledger refusal list in `contracts/POLICY-VERIFIER.md` is also affected.
- **Requested by:** Integration (San Antonio), Integration I1c.
- **Status:** authorized by Maura on 2026-10-19 for the integration branch `maurathat/credence-integration-i0` (I1c authorization §2, "Core"). Core review of the merged result is still required.
- **Blocked:** the integrated purchase lifecycle (`contracts/INTEGRATION-FLOW.md`).

## Constraints kept

- No canonical object schema changes. No new canonical object, member, enum value or reason code.
- The verifier and its reason-code precedence are unchanged.
- The deterministic fixtures in `vectors/c3b/` must regenerate byte for byte.
- No XRPL.

## Changes

1. **Delivery attempts (durable intent before chargeable execution).** Add `begin_delivery(PurchaseReference, now) -> DeliveryAttempt`. In one transaction it checks:
   - the reservation is held and live;
   - the facility is registered;
   - the approved purchase matches;
   - no attempt has been begun for this reservation.

   It then records the attempt in an operational table, `delivery_attempts`. A provider may be called only after this commits.
2. **Coordinator-controlled no-delivery release.** Add `release_undelivered(PurchaseReference, NoDeliveryReason, now)`.
   - **Trusted evidence:** Core's own record that no delivery attempt was ever begun for the reservation. That is an attempt Core never authorized, so no provider can be owed for it. A caller's claim that delivery failed is not accepted as evidence.
   - Once an attempt exists, release is refused (`DELIVERY_ATTEMPTED`). Once a release exists, an attempt is refused (`NOT_HELD`).
   - Every release is audited in `no_delivery_releases`.
   - Reasons form a closed set: `provider_not_connected`, `abandoned_before_execution`.
3. **The bare ledger `release` is refused for registered facilities** (`USE_SETTLEMENT_COORDINATOR`), as the bare `settle` already is. No unaudited release remains for an accounted facility.
4. **Unknown delivery outcome.** Add `record_unknown_delivery(reservation_id, UnknownDeliveryCause, now)`. It is used once an attempt was begun but no delivery evidence was captured.
   - It records the outcome on the attempt.
   - It does not release, does not change drawn, does not settle and fabricates nothing.
   - The hold lapses only through the existing 600-second expiry.
5. **`CHAIN_BROKEN` is no longer terminal.** It is removed from `TERMINAL_REASONS`, so a `CHAIN_BROKEN` rejection is a `rejected_submission`:
   - the reservation stays held (expiry still bounds it);
   - drawn and the chain head are unchanged;
   - the dispute is recorded.
6. **Dispute evidence.** Every authenticated rejection keeps the exact signed evidence in `dispute_evidence`, keyed by dispute: receipt, gateway signature, attestation, provider signature and delivered digest, as canonical bytes. Unauthenticated submissions keep the digest row only, as before.
7. **Authenticated late delivery.** A settle at or after `expires_at` still releases the hold as expired and refuses `EXPIRED`; it never settles and drawn is unchanged.
   - If the submission is authenticated (the same rule as terminal failures), its exact evidence is recorded once per receipt in `late_delivery_disputes`.
   - This also applies when the hold had already been swept as expired.
   - These are separate from the verifier-rejection rows that `disputes()` returns, so the C3B transcripts are unchanged.
8. **Read accessors** for reconciliation: `delivery_attempt`, `no_delivery_releases`, `late_delivery_disputes`, `dispute_evidence`.

## Not requested

- A gateway-signed failure statement. It would need a new canonical object; out of scope.
- Any change to `settle`'s acceptance path, drawn accounting, the replay rules or the fault points.
