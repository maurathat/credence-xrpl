# POLICY-VERIFIER

Owner: Credence Core (Claude Code). Status: checkpoint C2, amended in C2A (unconstrained precision and quantization), C3B (facility accounting guards on the ledger) and Integration I1c (the bare release is refused for registered facilities). Only the owner edits this file. Other workstreams file a request in `contracts/requests/`.

This contract describes the decision layer implemented in `credence/core/`:
- the policy engine (`policy_engine.py`);
- the procurement matcher (`procurement.py`);
- the verifier (`verifier.py`);
- the SQLite reservation ledger (`reservation_ledger.py`).

The code is authoritative. `tests/unit/test_core_policy_verifier_contract.py` checks every list, field and code below against the code. The object types are in `contracts/CORE-OBJECTS.md`. The decision is `docs/decisions/0003-credence-core-c2-policy-verifier.md`.

## Pure engines

`policy_engine.evaluate` and `verifier.verify` are pure and deterministic. They read no database, XRPL, network or clock, and they do no cryptography.
- **Inputs.** Everything arrives in the input record, including the evaluation time and the signature facts. The signature layer (`contracts/SIGNATURES.md`, C3A) establishes those facts; the engines themselves do no cryptography.
- **Output.** Each returns a `credence.decision_record`:
  - `decision_stage` is the engine's stage;
  - `reason_code` is null on accept, otherwise one code from that stage;
  - `engine_version` is `credence-core-policy-c2` or `credence-core-verifier-c2`;
  - `decided_at` is the evaluation time;
  - `input_digests` are in the order given below;
  - `authorized_transaction_digest` and `authorized_transaction_digest_scheme` are null.
- **Rule order.** The first failing rule wins.
- **Inconsistent inputs.** If the input objects do not refer to each other, the engine raises (`PolicyInputError` or `VerifierInputError`) and makes no decision. That is a caller error, not an attack.

## Reason-code ownership

The policy engine emits only the policy codes, and the verifier only the verifier codes (`POLICY_REASON_CODES` and `VERIFIER_REASON_CODES`). The two sets are disjoint, and together they are the whole `ReasonCode` vocabulary. `credence.decision_record` itself does not check stage against code; the engines guarantee it.

## Policy input

| Field | Type | Meaning |
| --- | --- | --- |
| `request` | `PurchaseRequest` | The request being decided |
| `request_signature_valid` | `bool` | Fact: the request is signed by the agent's current working key |
| `working_key_current` | `bool` | Fact: that key is still on the borrower account's signer list |
| `facility` | `Facility` | The facility the request names |
| `quote` | `Quote` | The quote the request names, from the requested provider |
| `service_class` | `ServiceClass` | The quoted service class |
| `execution_profile` | `ExecutionProfile` | The service class's profile |
| `procurement_classes` | `tuple[ProcurementClass, ...]` | The facility's allowed procurement classes, in the facility's order |
| `currently_reserved` | `MicroTrlusd` | Sum of live held reservations on the facility |
| `nonce_already_used` | `bool` | Fact: the nonce was already used on this facility |
| `evaluated_at` | `Timestamp` | Evaluation time |

Consistency (otherwise `PolicyInputError`):
- request facility, quote ID and provider equal the facility's ID and the quote's ID and provider;
- the quote's service class digest equals the service class digest;
- the service class's profile digest equals the profile digest;
- the procurement class digests equal `facility.allowed_procurement_classes`;
- the facts are exactly `bool`.

Input digests, in order: request, facility, quote, service class, execution profile, then each procurement class.

## Policy precedence

1. `SIG_INVALID`: `request_signature_valid` is false.
2. `KEY_REVOKED`: `working_key_current` is false.
3. `FACILITY_NOT_ACTIVE`: facility status is not `active`.
4. `FACILITY_EXPIRED`: `evaluated_at > maturity_at`, or `evaluated_at > request.expires_at`. Equality is valid.
5. `QUOTE_EXPIRED`: `evaluated_at > quote.valid_until`. Equality is valid.
6. `PURPOSE_MISMATCH`: request or facility purpose is not `inference`.
7. `PROVIDER_NOT_ALLOWED`: the requested provider is not in `facility.allowed_providers` (exact ID).
8. `PROFILE_NOT_ALLOWED`: no allowed procurement class matches structurally (see Procurement matching).
9. `TIER_TOO_LOW`: some class matches structurally, but for every such class the service tier is below the stricter of `facility.minimum_evidence_tier` and the class's `minimum_evidence_tier`.
10. `PRICE_ABOVE_MAX`: the maximum cost exceeds `request.max_total_cost_micro_trlusd`. Equality is allowed.
11. `OVER_LIMIT`: `drawn + currently_reserved + maximum cost > limit`. Equality is allowed.
12. `NONCE_REUSED`: `nonce_already_used` is true.

If none fires, the decision is accept.

**Maximum cost.** It is computed before purchase as the frozen billing formula at the quote's caps: `ceil(max_input_tokens × input price / 1,000,000) + ceil(max_output_tokens × output price / 1,000,000)`, in integers. Values beyond 2**53 − 1 are compared as integers and reject with `PRICE_ABOVE_MAX`. On accept, this amount is the reservation's `maximum_amount_micro_trlusd`.

### Unreachable under C1A typing

`PURPOSE_MISMATCH`: `Purpose` has exactly one value, so a typed request and facility always agree. The check exists for future vocabularies.

## Procurement matching

A service class, with its execution profile and the quoting provider, matches a procurement class structurally when every requirement holds:

1. `model_family`: equal.
2. `precision`: `allowed_precisions` is null (no buyer constraint), or the profile's precision is asserted and in it. There is no ranking.
3. `quantization`: `allowed_quantizations` is null (no buyer constraint), or the profile's quantization is asserted and in it.
4. `delivery_mode`: equal.
5. `jurisdiction`: asserted and in `allowed_jurisdictions`.
6. `data_residency`: non-empty and a subset of `allowed_data_residencies`. Overlap alone is not enough.
7. `provider`: the quoting provider is in `allowed_providers`.

For precision and quantization:

| Procurement class | Profile | Result |
| --- | --- | --- |
| null (unconstrained) | null (not asserted) | match |
| null (unconstrained) | asserted value | match |
| constrained set | null (not asserted) | no match |
| constrained set | value in the set | match |
| constrained set | value not in the set | no match |

Every other requirement is always constrained. A null or empty (not asserted) service or profile value never satisfies a constraint. There is no "any" value or wildcard string. Evidence tier is not part of the structural match; it is judged at `TIER_TOO_LOW`.

## Verifier input

| Field | Type | Meaning |
| --- | --- | --- |
| `receipt` | `DeliveryReceiptBody` | The receipt being verified |
| `gateway_signature_valid` | `bool` | Fact: the gateway's signature over the receipt is valid |
| `provider_signature` | `ProviderSignatureFact` | Fact about the provider's attestation signature |
| `provider_attestation` | `ProviderAttestationBody` | Required at Tier 2; must be absent at Tier 1 (nullable) |
| `request` | `PurchaseRequest` | The approved request |
| `quote` | `Quote` | The approved quote |
| `service_class` | `ServiceClass` | The approved service class |
| `execution_profile` | `ExecutionProfile` | The approved profile |
| `delivered_response_digest` | `Sha256Digest` | Digest of the delivered response, computed independently |
| `expected_previous_receipt_digest` | `Sha256Digest` | The facility's last receipt digest; null for the first receipt (nullable) |
| `receipt_already_settled` | `bool` | Fact: this receipt was already settled |
| `evaluated_at` | `Timestamp` | Evaluation time |

`ProviderSignatureFact` values: `valid`, `invalid`, `not_present`.

Consistency (otherwise `VerifierInputError`):
- the request's quote ID and provider match the quote;
- quote, service class and profile digests chain;
- the facts are exactly `bool`;
- at Tier 1, there is no attestation and the provider fact is `not_present`;
- at Tier 2, the attestation is present.

Input digests, in order: receipt, request, quote, service class, execution profile, delivered response digest, then the attestation (Tier 2 only).

## Verifier precedence

1. `RECEIPT_SIG_INVALID`: the gateway signature is invalid, or (Tier 2) the provider fact is not `valid`.
2. `BINDING_MISMATCH`: the receipt does not bind to the approved purchase (see Bindings).
3. `RESPONSE_DIGEST_MISMATCH`: `delivered_response_digest` differs from `receipt.response_digest`.
4. `TOKEN_COUNT_EXCEEDS_QUOTE`: input or output tokens exceed the quote's caps. Equality is allowed.
5. `BILLED_AMOUNT_MISMATCH`: `receipt.price_micro_trlusd` differs from the billing formula on the receipt's token counts and the quote's rates.
6. `CHAIN_BROKEN`: `receipt.previous_receipt_digest` differs from `expected_previous_receipt_digest`. Both null (the first receipt) passes; exactly one null fails.
7. `REPLAYED_RECEIPT`: `receipt_already_settled` is true.

If none fires, the decision is accept.

### Bindings

`BINDING_MISMATCH` fires unless all of the following hold:
- the receipt's `purchase_request_digest` equals the request's digest;
- its `execution_profile_digest` equals the profile's digest;
- its `provider_id` and `nonce` equal the request's;
- its `evidence_tier` equals the approved service class's tier;
- at Tier 2, `provider_attestation_digest` equals the attestation's digest, and the attestation's `purchase_request_digest`, `execution_profile_digest`, `response_digest`, `input_token_count` and `output_token_count` equal the receipt's.

Only digests are compared. No prompt or response text is involved.

## Reservation ledger

`ReservationLedger(path, ttl_seconds=600)` runs on SQLite. Each operation opens its own connection to the file at `path`.

- **Reserve atomically.** `reserve(context, now)` runs in one `BEGIN IMMEDIATE` transaction, which takes the write lock before reading. Inside it:
  1. it releases expired holds;
  2. it reads the live held total and whether the nonce was used;
  3. it calls `policy_engine.evaluate`;
  4. it stores the decision record;
  5. on accept only, it inserts the reservation (`held`, the maximum cost, `created_at = now`, `expires_at = now + ttl`) and marks the nonce used.

  So two concurrent requests cannot both take the last headroom.
- **Reservation ID.** It is `reservation-` followed by the first 32 hex digits of the request digest.
- **Live window.** A reservation is live while `now < expires_at`. Headroom counts only live `held` reservations.
- **Expire.** `expire(now)` sets `released` with release reason `expired`. That reason is stored outside the canonical object.
- **Settle.** `settle(id, now)` moves a live `held` reservation to `settled`. At or after `expires_at`, it releases the hold as expired and refuses. For a facility registered in `facility_accounts` (C3B) it refuses outright (`USE_SETTLEMENT_COORDINATOR`): only the settlement coordinator may settle, because it updates drawn in the same transaction.
- **Release.** `release(id, now)` moves `held` to `released`, with release reason `released`. For a registered facility it refuses outright (`USE_SETTLEMENT_COORDINATOR`, Integration I1c): the settlement coordinator releases those, only through its audited paths.
- **Drawn amount.** For a registered facility the stored drawn amount is authoritative, and the settlement coordinator adds each settled charge to it (`contracts/SETTLEMENT-COORDINATOR.md`). `reserve` refuses a `Facility` whose limit or drawn differs from the stored row (`STALE_FACILITY_ACCOUNTING`). Unregistered facilities keep the C2 behaviour. The ledger does not move money and is not escrow.
- **In-transaction use.** `write_transaction()` and `reserve_in_transaction(conn, context, now)` let the settlement coordinator run the same reserve step inside its own `BEGIN IMMEDIATE` transaction.

### Ledger refusal codes

`ReservationStateError.reason`: `EXPIRED`, `NOT_FOUND`, `NOT_HELD`, `STALE_FACILITY_ACCOUNTING`, `USE_SETTLEMENT_COORDINATOR`

## Not in this contract

Signature verification, XRPL settlement and transaction signing, LoanSet and LoanPay, facility persistence and the drawn-amount update, the inference gateway and providers, API and UI.
