# 0003. Credence Core C2: policy engine, verifier and reservation ledger

- Status: Accepted
- Date: 2026-10-14
- Scope: Credence Core checkpoint C2
- Source: `docs/spec/credence-build-plan.md` (Policy engine and verifier), decision 0002, Maura's C2 brief and blocker decisions of 2026-10-14. Contract: `contracts/POLICY-VERIFIER.md`.

## Decisions

1. **Pure engines, separate state.** The policy engine and the verifier are pure, deterministic functions over typed inputs. They do no database, XRPL, network or clock access, and no cryptography. All state lives in the reservation ledger, which supplies the engine's stateful inputs inside its own transaction:
   - the live held amount;
   - nonce use;
   - the evaluation time.
2. **Atomic reservations on SQLite.** Each `reserve` runs in one `BEGIN IMMEDIATE` transaction, which takes SQLite's write lock before reading. Expiring stale holds, reading headroom and nonce use, deciding, storing the decision record, inserting the reservation and marking the nonce all happen together. Two simultaneous requests therefore cannot both take the last headroom. This is shown by a real concurrent test on SQLite, and by a negative control in which removing the lock lets both through.
3. **Signature facts arrive from outside.** C2 verifies no signatures.
   - The policy engine receives `request_signature_valid` and `working_key_current`.
   - The verifier receives `gateway_signature_valid` and a `ProviderSignatureFact` (`valid`, `invalid` or `not_present`).
   - Tier 1 needs a valid gateway signature only, with no attestation or provider signature fact. Tier 2 also needs the attestation and a valid provider signature. Tier 3 is not supported.
4. **No XRPL settlement in C2.** No payment is built or signed. `authorized_transaction_digest` and `authorized_transaction_digest_scheme` stay null.
5. **Each stage owns its reason codes.** The policy engine emits only `POLICY_REASON_CODES` and the verifier only `VERIFIER_REASON_CODES`; the sets are disjoint and complete. `DecisionRecord` itself is not made to validate this. Two codes were added as a C1A amendment (decision 0002, item 21): `QUOTE_EXPIRED` and `BILLED_AMOUNT_MISMATCH`.
6. **Profile vs tier.** `PROFILE_NOT_ALLOWED` means no allowed procurement class matches structurally. `TIER_TOO_LOW` means a structural match exists, but the service's tier is below the stricter of the facility's and that class's minimum, for every such class. Null or empty (not asserted) values match nothing, residency is a subset match, and there is no precision ranking.
7. **Price and limit.** The pre-purchase maximum is the frozen billing formula at the quote's token caps. It is both the price checked against the request's maximum and the reservation amount. `OVER_LIMIT` compares drawn plus held plus that maximum against the limit; equality is allowed.
8. **Binding.**
   - `BINDING_MISMATCH` covers: request digest, profile digest, provider, nonce, tier against the approved service class, and the Tier 2 attestation (its digest and five members).
   - A billed amount that differs from the formula is `BILLED_AMOUNT_MISMATCH`, not a binding failure.
9. **Inconsistent inputs are errors, not decisions.** Objects that do not refer to each other raise `PolicyInputError` or `VerifierInputError`.
10. **Time boundaries.**
    - Facility, request and quote expiry use strict `>`, so equality is valid.
    - A reservation is live while `now < expires_at`. The default TTL is 600 seconds.
    - Settling at or after expiry releases the hold as expired and refuses.
11. **PURPOSE_MISMATCH is unreachable under C1A typing,** because `Purpose` has one value. It is documented and tested as such, not faked.

## Consequences

- **Drawn amount.** The settlement coordinator (C3) must add settled amounts to the facility's drawn amount when it settles a reservation, because headroom counts only live held reservations.
- **Provider A and procurement.** A provider whose profile leaves precision or quantization null (not asserted) cannot satisfy any procurement class, since every allowed set is non-empty. This affects the plan's Provider A (Nebius, Tier 1). It qualifies only if its profile asserts these values.
