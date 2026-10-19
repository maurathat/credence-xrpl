# SETTLEMENT-COORDINATOR

Owner: Credence Core (Claude Code). Status: checkpoint C3B. Only the owner edits this file. Other workstreams file a request in `contracts/requests/`.

This contract describes `credence/core/settlement_coordinator.py` as implemented. The code is authoritative. `tests/unit/test_core_settlement_contract.py` checks the lists below against the code. Scenario fixtures are in `vectors/c3b/`. The decision is `docs/decisions/0005-credence-core-c3b-settlement-coordinator.md`.

## Scope: accounting settlement only

Settlement has three stages (`SettlementState`); C3B reaches only the second:

1. `delivery_verified`: the real C2 verifier accepted the delivery (transient, inside the settlement transaction).
2. `accounting_settled`: the facility's drawn amount includes the verified charge and the reservation is settled.
3. `xrpl_settled`: the provider payment is confirmed on XRPL. Not implemented.

What C3B does not do:
- **No XRPL.** No transaction is built, signed or submitted, and no XRPL hash is produced.
- **No SettlementRecord.** `credence.settlement_record` stays reserved for `xrpl_settled`, because it requires a real XRPL transaction hash. An accounting settlement is not an XRPL-confirmed payment.
- **Drawn is not loan principal.** The accounting drawn amount is Credence's record of verified spending, not confirmed XRPL loan principal.

## Storage

Everything uses the reservation ledger's SQLite file. These are operational tables, not canonical objects:

| Table | Holds |
| --- | --- |
| `facility_accounts` | Per facility: the authoritative `limit_micro_trlusd` and `drawn_micro_trlusd` |
| `accounting_settlements` | Per settled receipt: `receipt_digest` (unique), `reservation_id` (unique), facility, verifier decision digest, actual amount, `settled_at`, `state` (always `accounting_settled`) |
| `facility_chain_heads` | Per facility: the digest of its last accounting-settled receipt |
| `settlement_disputes` | Each rejected delivery: kind, reservation, facility, receipt digest, verifier decision digest, reason code, time |

All amounts are integer micro-tRLUSD.

## Facility accounting

- **Registration.** `register_facility(facility)` starts accounting from the facility's limit and drawn amount, once per facility.
- **Exposure.** Effective exposure is the stored drawn amount plus live held reservations. Reserving keeps it at or below the limit (C2 `OVER_LIMIT`).
- **After settlement.** A settled reservation stops counting as held, and its actual charge stays in drawn. The unused part of the hold is simply no longer held. It is never counted as spending, and settled spending is never forgotten.
- **Stored drawn is authoritative.**
  - `reserve` refuses a `Facility` whose limit or drawn differs from the stored row. The ledger reports `STALE_FACILITY_ACCOUNTING`, and the caller re-reads `exposure()` and retries.
  - The C2 ledger's bare `settle` refuses for a registered facility (`USE_SETTLEMENT_COORDINATOR`), so no reservation can be settled without the drawn update.

## Reserve

`reserve(SignedPurchase, now)`:
1. Computes the policy signature facts itself from the signed request and the key registry (`contracts/SIGNATURES.md`). Callers cannot supply facts.
2. Runs the C2 reservation step inside one `BEGIN IMMEDIATE` transaction, against the stored facility accounting.

## Settle

`settle(DeliverySubmission, now)` runs as one `BEGIN IMMEDIATE` transaction:

1. Load the reservation. It must exist (`NOT_FOUND`) and be `held` (`NOT_HELD`).
2. If `now` is at or after `expires_at`, release it as expired and refuse (`EXPIRED`). An expired reservation never settles later.
3. Load the facility account (`UNACCOUNTED_FACILITY`).
4. Load the policy decision that created the reservation; it must be a policy-stage accept. The submitted request, quote, service class and execution profile must be exactly the ones it approved, and the request must be for this facility (`WRONG_PURCHASE`).
5. Compute the verifier signature facts here, from the gateway signature over the receipt and the provider signature over the attestation.
6. Run the real verifier. The expected predecessor is the facility's chain head; "already settled" comes from `accounting_settlements`. Inconsistent inputs are refused (`INCONSISTENT_SUBMISSION`).
7. On accept: require `0 <= actual <= reserved` (`AMOUNT_EXCEEDS_RESERVATION`) and `drawn + actual <= limit` (`LIMIT_EXCEEDED`). `actual` is the receipt's billed amount, which the verifier has checked equals the billing formula. Then, in order:
   - add `actual` to drawn;
   - mark the reservation `settled`;
   - record the receipt's settlement identity;
   - advance the chain head;
   - commit.

Any exception rolls the whole transaction back. Failure injection at each of these points leaves drawn, the reservation and the receipt untouched:

`before_drawn_update`, `after_drawn_update`, `after_reservation_settled`, `after_receipt_recorded`

## Rejected deliveries

When the verifier rejects, drawn is never increased, and a row is written to `settlement_disputes`.

- **`delivery_failure`: authenticated terminal failure.** All of the following hold:
  - the gateway signature is valid;
  - at Tier 2, the provider signature is valid;
  - the receipt is for this reservation's own request;
  - the reason is one of the terminal reasons below.

  The reservation is released.
- **`rejected_submission`: any other rejection.** The reservation stays held until it settles or expires, so an invalid or unauthenticated submission cannot free a legitimate pending purchase.

Terminal reasons: `BINDING_MISMATCH`, `TOKEN_COUNT_EXCEEDS_QUOTE`, `BILLED_AMOUNT_MISMATCH`, `CHAIN_BROKEN`

These are decided by signed content against Core state. `RECEIPT_SIG_INVALID` means the submission is not authenticated. `RESPONSE_DIGEST_MISMATCH` depends on the caller-supplied delivered digest. Neither releases.

## Receipt chain

The facility's last receipt is its last accounting-settled receipt (`facility_chain_heads`; none before the first). Rejected, disputed, expired and unverified receipts never advance it. The verifier's frozen precedence is unchanged: a settled receipt submitted against another reservation is reported as `BINDING_MISMATCH`, not rewritten as `REPLAYED_RECEIPT`.

**Requirement on the inference gateway.** Issue at most one in-flight receipt per facility. Set its `previous_receipt_digest` to `chain_head(facility)`. Issue the next receipt only after the previous one has settled or been finally rejected. Two receipts issued against the same head conflict: the second to settle fails `CHAIN_BROKEN`.

## Tiers

- **Tier 1:** a valid gateway signature is required.
- **Tier 2:** valid gateway and provider signatures are required.
- **Tier 3:** not implemented.

## Replay protection

Replay protection is persisted in SQLite and survives restarts:
- a reservation settles once (status check plus `reservation_id` unique);
- a receipt settles once (`receipt_digest` primary key);
- a nonce authorizes one reservation (C2 `used_nonces`).

Concurrent settlements are serialized by `BEGIN IMMEDIATE`.

## Refusal codes

`SettlementRefused.reason`: `AMOUNT_EXCEEDS_RESERVATION`, `EXPIRED`, `FACILITY_ALREADY_REGISTERED`, `INCONSISTENT_SUBMISSION`, `LIMIT_EXCEEDED`, `NOT_FOUND`, `NOT_HELD`, `POLICY_DECISION_MISSING`, `POLICY_DECISION_NOT_ACCEPT`, `UNACCOUNTED_FACILITY`, `WRONG_PURCHASE`
