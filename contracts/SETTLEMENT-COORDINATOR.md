# SETTLEMENT-COORDINATOR

Owner: Credence Core (Claude Code). Status: checkpoint C3B, amended in Integration I1c (`contracts/requests/0001-core-settlement-delivery-attempts.md`) and I1e (`contracts/requests/0003-core-attempt-enforcement.md`), and I1g (`contracts/requests/0005-core-registration-boundary.md`), authorized by Maura on 2026-10-19 and 2026-10-20; Core review pending. Only the owner edits this file. Other workstreams file a request in `contracts/requests/`.

This contract describes `credence/core/settlement_coordinator.py` as implemented. The code is authoritative. `tests/unit/test_core_settlement_contract.py` checks the lists below against the code. Scenario fixtures are in `vectors/c3b/`. The decisions are `docs/decisions/0007-credence-core-c3b-settlement-coordinator.md`, `docs/decisions/0008-integration-i1c-inference-accounting.md` and `docs/decisions/0009-integration-i1e-security-remediation.md`.

## Scope: accounting settlement only

Settlement has three stages (`SettlementState`); only the second is reached:

1. `delivery_verified`: the real C2 verifier accepted the delivery (transient, inside the settlement transaction).
2. `accounting_settled`: the facility's drawn amount includes the verified charge and the reservation is settled.
3. `xrpl_settled`: the provider payment is confirmed on XRPL. Not implemented.

What the coordinator does not do:
- **No XRPL.** No transaction is built, signed or submitted, and no XRPL hash is produced.
- **No SettlementRecord.** `credence.settlement_record` stays reserved for `xrpl_settled`, because it requires a real XRPL transaction hash. An accounting settlement is not an XRPL-confirmed payment.
- **Drawn is not loan principal.** The accounting drawn amount is Credence's record of verified spending, not confirmed XRPL loan principal.
- **No new canonical objects.** Everything added in I1c and I1e is an operational table.

## What each record means

These are distinct, and none implies the next:

| Record | Means | Does not mean |
| --- | --- | --- |
| Recorded attempt (`begin_delivery`) | Core authorized a provider call for this reservation | That any provider was called |
| Claimed execution (`claim_execution`) | The supported gateway committed its one call, immediately before calling the provider | That the provider executed or delivered |
| Provider execution | The provider ran the request. Core has no record of this; it is inferred only from evidence | — |
| Captured evidence | The gateway built and signed a receipt (and the provider signed an attestation at Tier 2) | That Core verified it |
| Verified delivery (`delivery_verified`) | Core's verifier accepted the evidence, inside the settlement transaction | That anything was charged, if the transaction then refused or rolled back |
| Accounting settlement (`accounting_settled`) | Drawn includes the verified charge; the reservation is settled; the chain advanced | That any payment moved |
| Confirmed XRPL settlement (`xrpl_settled`) | The provider payment is confirmed on XRPL | Not implemented |

## Storage

Everything uses the reservation ledger's SQLite file. These are operational tables, not canonical objects:

| Table | Holds |
| --- | --- |
| `facility_accounts` | Per facility: the authoritative `limit_micro_trlusd` and `drawn_micro_trlusd` |
| `accounting_settlements` | Per settled receipt: `receipt_digest` (unique), `reservation_id` (unique), facility, verifier decision digest, actual amount, `settled_at`, `state` (always `accounting_settled`) |
| `facility_chain_heads` | Per facility: the digest of its last accounting-settled receipt |
| `settlement_disputes` | Each verifier rejection: kind, reservation, facility, receipt digest, verifier decision digest, reason code, time |
| `dispute_evidence` | Per authenticated rejection: the exact canonical bytes of the receipt, gateway signature, attestation and provider signature, and the delivered digest |
| `delivery_attempts` | Per reservation: Core's record that a provider may be called (request digest, `begun_at`), when the supported gateway claimed its one call (`execution_claimed_at`), and an unknown-outcome cause if one was recorded |
| `no_delivery_releases` | Per reservation released with no delivery attempt: request digest, policy decision digest, reason, time |
| `late_delivery_disputes` | Per authenticated receipt submitted at or after `expires_at`: reservation, facility, `expires_at`, submission time and the exact evidence bytes |

All amounts are integer micro-tRLUSD.

## Keys the coordinator trusts

The coordinator verifies signatures through the `KeyRegistry` it was constructed with; C3A semantics are unchanged. In the integrated flow that registry is the integration's `DurableKeyRegistry` (`contracts/INTEGRATION-FLOW.md`, "Key revocation"). A key revoked there is persisted, survives restarts and overrides any registry supplied later, so a revoked agent key cannot be used to reserve again.

## Facility accounting

- **Registration (amended in I1g, Core finding B-1).** `register_facility(facility)` starts accounting from the facility's limit and drawn amount, once per facility (`FACILITY_ALREADY_REGISTERED`).
  - **Accounted registration requires a history-free facility identity.** The identity is the `facility_id`, the key every ledger and settlement table uses. In the same `BEGIN IMMEDIATE` transaction as the insert, registration refuses (`FACILITY_HAS_HISTORY`) if that identity appears in any of `reservations`, `used_nonces`, `accounting_settlements`, `facility_chain_heads`, `settlement_disputes`, `delivery_attempts`, `no_delivery_releases` or `late_delivery_disputes`. This covers held, released, expired and settled reservations, and consumed nonces.
  - Two tables are not checked. `dispute_evidence` is reached through `settlement_disputes`. `decision_records` carries no facility identity, and a policy rejection alone creates no exposure, nonce, attempt or settlement state.
  - **Existing unaccounted history is not silently adopted.** A refused registration writes nothing: no accounted row, no nonce, no change to the history. The facility stays unaccounted unless a separately authorized migration protocol establishes safe adoption. Past standalone provider executions are never reinterpreted as accounted delivery attempts.
  - **A refused registration does not make historical delivery evidence safe to release.** The facility has no accounted `release_undelivered`, `begin_delivery` or `settle` path (`UNACCOUNTED_FACILITY`).
  - **Ordering.** The ledger's bare reserve also runs in `BEGIN IMMEDIATE` and checks registration, so a reservation and a registration on separate connections serialize. Either the reservation commits first and registration refuses, or registration commits first and the bare reserve refuses (`USE_SETTLEMENT_COORDINATOR`).
  - **The registered-facility invariants in this contract apply only after valid registration through this boundary.**
  - Deferred hardening (decision 0009, B1-D2): settlement requires a persisted attempt but not a claimed execution. Through the supported gateway every receipt follows a claim, and the facility-adoption path is closed above. Requiring `execution_claimed_at` at settlement is recorded as possible further hardening, not added in I1g.
- **Exposure.** Effective exposure is the stored drawn amount plus live held reservations. Reserving keeps it at or below the limit (C2 `OVER_LIMIT`).
- **After settlement.** A settled reservation stops counting as held, and its actual charge stays in drawn. The unused part of the hold is simply no longer held. It is never counted as spending, and settled spending is never forgotten.
- **Stored drawn is authoritative.**
  - `reserve` refuses a `Facility` whose limit or drawn differs from the stored row. The ledger reports `STALE_FACILITY_ACCOUNTING`, and the caller re-reads `exposure()` and retries.
  - The C2 ledger's public `reserve`, `reserve_in_transaction`, `settle` and `release` refuse a registered facility (`USE_SETTLEMENT_COORDINATOR`). The public reserve paths take caller-supplied signature facts, so only the coordinator may reserve on an accounted facility (I1e). No reservation on an accounted facility is made without Core's own signature check, settled without the drawn update, or released without an audited coordinator path.

## Reserve

`reserve(SignedPurchase, now)`:
1. Computes the policy signature facts itself from the signed request and the key registry (`contracts/SIGNATURES.md`). Callers cannot supply facts.
2. Runs the C2 reservation step inside one `BEGIN IMMEDIATE` transaction, against the stored facility accounting, through the ledger's internal accounted path (`_reserve_accounted`). That path is not a public interface. An unregistered facility is refused (`UNACCOUNTED_FACILITY`).

A forged or invalid request signature is a policy rejection (`SIG_INVALID`). It holds no headroom and consumes no nonce.

## Delivery attempts

`begin_delivery(PurchaseReference, now)` records durable intent before chargeable execution. A provider may be called for a reservation only after it commits. In one transaction it requires:
- the reservation exists and is held (`NOT_FOUND`, `NOT_HELD`);
- the facility is registered (`UNACCOUNTED_FACILITY`);
- the reservation is live; at or after `expires_at` it is released as expired and refused (`EXPIRED`);
- the purchase objects are exactly the ones the policy decision approved (`POLICY_DECISION_MISSING`, `POLICY_DECISION_NOT_ACCEPT`, `WRONG_PURCHASE`);
- no attempt exists yet (`DELIVERY_ALREADY_ATTEMPTED`).

At most one attempt exists per reservation.

## Claimed execution

`claim_execution(DeliveryAttempt, now)` authorizes exactly one provider call. The supported gateway (`Gateway.deliver`) calls it immediately before calling the provider. The supplied attempt is only an identifier. In one transaction:
- the persisted attempt must exist (`NO_DELIVERY_ATTEMPT`) and equal the supplied one in reservation, facility, request digest and `begun_at` (`ATTEMPT_MISMATCH`), so a caller-constructed attempt is refused;
- it must not be marked unknown (`DELIVERY_OUTCOME_UNKNOWN`) or already claimed (`EXECUTION_ALREADY_CLAIMED`);
- the reservation must be held (`NOT_HELD`) and live; at or after `expires_at` it is released as expired and refused (`EXPIRED`).

It then records `execution_claimed_at`.

## No-delivery release

`release_undelivered(PurchaseReference, NoDeliveryReason, now)` is the only release for an accounted facility other than an authenticated terminal verifier failure.

- **What it rests on.** The release rests on Core's own record, not on a caller's claim. It is permitted only if no delivery attempt was ever begun for the reservation (otherwise `DELIVERY_ATTEMPTED`).
- **What that proves, and what it does not.** Through Credence's supported, enforced interfaces, no provider is called for a reservation without an attempt:
  - `Gateway.deliver` calls a provider only after Core's `claim_execution` on a persisted attempt;
  - the standalone `Gateway.purchase` refuses accounted facilities;
  - the integration service exposes no gateway, ledger or writable coordinator.

  The absence of an attempt therefore means no supported path called a provider for the reservation. It does not prove that no provider executed through code that bypasses these interfaces, for example code that calls a provider adapter or the SQLite file directly. Such a delivery could not settle (see Settle step 6).
- **Exclusive.** After the release the reservation is no longer held, so no attempt can begin (`NOT_HELD`).
- **Checks.** It makes the same checks as `begin_delivery`, and the reason must be a `NoDeliveryReason` (`INVALID_RELEASE_REASON`).
- **Audit.** It writes one `no_delivery_releases` row and sets release reason `released`.

`NoDeliveryReason` values: `provider_not_connected`, `abandoned_before_execution`.

## Unknown delivery outcome

`record_unknown_delivery(reservation_id, UnknownDeliveryCause, now)` marks a begun attempt for which no delivery evidence was captured. The provider may have executed a chargeable request.

- It requires an attempt (`NO_DELIVERY_ATTEMPT`) on a reservation that is not settled (`NOT_HELD`). The cause must be an `UnknownDeliveryCause` (`INVALID_UNKNOWN_CAUSE`).
- It does not release, settle, change drawn or create a receipt. The hold lapses only by the 600-second expiry.
- The first recorded cause is kept; repeating is a no-op.

`UnknownDeliveryCause` values: `provider_unavailable`, `provider_execution_error`, `provider_malformed_response`, `interrupted`.

## Settle

`settle(DeliverySubmission, now)` runs as one `BEGIN IMMEDIATE` transaction:

1. Load the reservation. It must exist (`NOT_FOUND`).
2. **Late delivery.** If `now` is at or after `expires_at`, never settle. The reservation is released as expired if it was still held, and drawn is unchanged. Then:
   - on an accounted facility with no persisted attempt for this request, refuse `NO_DELIVERY_ATTEMPT` (or `ATTEMPT_MISMATCH`) and record nothing: Core never authorized this delivery;
   - otherwise refuse `EXPIRED`. An authenticated submission (see "Rejected deliveries") for the approved purchase keeps its exact evidence once per receipt in `late_delivery_disputes`.

   This also applies when the hold had already lapsed as expired.
3. The reservation must be `held` (`NOT_HELD`).
4. Load the facility account (`UNACCOUNTED_FACILITY`).
5. Load the policy decision that created the reservation; it must be a policy-stage accept. The submitted request, quote, service class and execution profile must be exactly the ones it approved, and the request must be for this facility (`WRONG_PURCHASE`).
6. **Authorized delivery (I1e).** Core's persisted delivery attempt for this reservation must exist (`NO_DELIVERY_ATTEMPT`) and name this facility and request (`ATTEMPT_MISMATCH`). A gateway-signed delivery without one is refused before the verifier runs, so it is neither settled nor disputed. An attempt marked unknown may still settle if valid evidence arrives, because the evidence resolves the uncertainty.
7. Compute the verifier signature facts here, from the gateway signature over the receipt and the provider signature over the attestation.
8. Run the real verifier. The expected predecessor is the facility's chain head; "already settled" comes from `accounting_settlements`. Inconsistent inputs are refused (`INCONSISTENT_SUBMISSION`).
9. On accept: require `0 <= actual <= reserved` (`AMOUNT_EXCEEDS_RESERVATION`) and `drawn + actual <= limit` (`LIMIT_EXCEEDED`). `actual` is the receipt's billed amount, which the verifier has checked equals the billing formula. Then, in order:
   - add `actual` to drawn;
   - mark the reservation `settled`;
   - record the receipt's settlement identity;
   - advance the chain head;
   - commit.

Any exception rolls the whole transaction back. Failure injection at each of these points leaves drawn, the reservation and the receipt untouched:

`before_drawn_update`, `after_drawn_update`, `after_reservation_settled`, `after_receipt_recorded`

## Rejected deliveries

When the verifier rejects, drawn is never increased, the chain head never moves, and a row is written to `settlement_disputes`.

- **Authenticated** means all of the following hold:
  - the gateway signature is valid;
  - at Tier 2, the provider signature is valid;
  - the receipt is for this reservation's own request.

  Every authenticated rejection also keeps its exact evidence in `dispute_evidence`.
- **`delivery_failure`: authenticated terminal failure.** The submission is authenticated and the reason is one of the terminal reasons below. The reservation is released.
- **`rejected_submission`: any other rejection.** The reservation stays held until it settles or expires, so an invalid or unauthenticated submission cannot free a legitimate pending purchase.

Terminal reasons: `BINDING_MISMATCH`, `TOKEN_COUNT_EXCEEDS_QUOTE`, `BILLED_AMOUNT_MISMATCH`

These are decided by signed content against Core state.
- `RECEIPT_SIG_INVALID` means the submission is not authenticated.
- `RESPONSE_DIGEST_MISMATCH` depends on the caller-supplied delivered digest.
- `CHAIN_BROKEN` depends on Credence's own receipt sequencing, not on the provider's delivery. Since I1c it does not release on its own; its evidence is kept and the hold lapses by expiry.

None of these three releases.

## Receipt chain

The facility's last receipt is its last accounting-settled receipt (`facility_chain_heads`; none before the first). Rejected, disputed, expired, late, unknown and unverified receipts never advance it. The verifier's frozen precedence is unchanged: a settled receipt submitted against another reservation is reported as `BINDING_MISMATCH`, not rewritten as `REPLAYED_RECEIPT`.

**Requirement on the inference gateway.** Issue at most one in-flight receipt per facility. Set its `previous_receipt_digest` to `chain_head(facility)`. Issue the next receipt only after the previous one has settled or been finally rejected. Two receipts issued against the same head conflict: the second to settle fails `CHAIN_BROKEN`. The integrated flow (`contracts/INTEGRATION-FLOW.md`) meets this with one in-flight purchase per facility.

## Tiers

- **Tier 1:** a valid gateway signature is required.
- **Tier 2:** valid gateway and provider signatures are required.
- **Tier 3:** not implemented.

## Replay protection

Replay protection is persisted in SQLite and survives restarts:
- a reservation settles once (status check plus `reservation_id` unique);
- a receipt settles once (`receipt_digest` primary key);
- a nonce authorizes one reservation (C2 `used_nonces`);
- a reservation has at most one delivery attempt (`reservation_id` primary key), and an attempt authorizes at most one claimed provider call (`execution_claimed_at`);
- a late receipt is recorded once (`receipt_digest` primary key).

Concurrent operations are serialized by `BEGIN IMMEDIATE`.

## Refusal codes

`SettlementRefused.reason`: `AMOUNT_EXCEEDS_RESERVATION`, `ATTEMPT_MISMATCH`, `DELIVERY_ALREADY_ATTEMPTED`, `DELIVERY_ATTEMPTED`, `DELIVERY_OUTCOME_UNKNOWN`, `EXECUTION_ALREADY_CLAIMED`, `EXPIRED`, `FACILITY_ALREADY_REGISTERED`, `FACILITY_HAS_HISTORY`, `INCONSISTENT_SUBMISSION`, `INVALID_RELEASE_REASON`, `INVALID_UNKNOWN_CAUSE`, `LIMIT_EXCEEDED`, `NOT_FOUND`, `NOT_HELD`, `NO_DELIVERY_ATTEMPT`, `POLICY_DECISION_MISSING`, `POLICY_DECISION_NOT_ACCEPT`, `UNACCOUNTED_FACILITY`, `WRONG_PURCHASE`
