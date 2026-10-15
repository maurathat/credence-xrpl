# CORE-OBJECTS

Owner: Credence Core (Claude Code). Status: frozen at checkpoint C1A; `ReasonCode` amended in C2; `ProcurementClass` precision and quantization made nullable in C2A. Only the owner edits this file. Other workstreams file a request in `contracts/requests/`.

This contract describes the canonical objects implemented in `credence/core/` (`primitives.py`, `objects.py`, `kessai.py`, `billing.py`). The code is authoritative. `tests/unit/test_core_contract.py` checks that every object type, member, type, nullability, vocabulary and refusal code listed here matches the code. The vectors in `vectors/c1/` are worked examples of every object.

Decisions behind this contract: `docs/decisions/0001-foundational-architecture.md` and `docs/decisions/0002-credence-core-c1-domain-model.md`.

## Serialization boundary

Kessai serializer: JCS (RFC 8785) with NFC normalization, bound using the SCITT Canonical Payload Binding `jcs` construction. The short form is JCS (RFC 8785) + NFC.

- **NFC is extra to RFC 8785.** It is applied to every string value and member name before JCS serialization, so a plain JCS implementation can produce different bytes for non-normalized input.
- **One implementation.** The only canonicalization is the pinned custody `impl/canon.py` (custody `5dc15ff6fb097dc5f4a47e72969fae4a296620a5`, sha256 `48d6ad1c92a292b1979ef05bc3010007ba0120e5da0d3be5f3f4f9b77902eb8d`). `credence.core.kessai` loads it, refusing a hash or `rfc8785` version mismatch, and calls `canon._admit`, then `canon._fold_nfc`, then `canon._identifier`.
- **Canonical bytes** are `rfc8785.dumps` of the folded value. They are released only if their SHA-256 equals `canon._identifier`.
- **Digest** is SHA-256 of the canonical bytes, written `sha256:` followed by 64 lower-case hex digits.
- **Entry points:**
  - `CanonicalObject.to_canonical()` gives the explicit member map.
  - `canonical_bytes()` and `digest()` go through `kessai.canonicalize`.
  - `from_canonical(value)` parses a JSON value and refuses anything `to_canonical` would not produce.
  - `kessai.canonicalize_json_bytes(raw)` exposes the pipeline for raw JSON text.
- **Admission:** the top level must be a JSON object. Integers must be within ±(2**53 − 1). Python floats are refused before encoding. A top-level member named `address` is refused, because canon would leave it out of the digest.
- **Identity:** a digest identifies the canonical bytes of one object. It asserts nothing beyond them.

## Envelope

Every canonical object begins with `object_type` and `schema_version`; `schema_version` is `"1"`. `object_type` is the section name below. The nested `sampling` object has no envelope. Members appear in the order listed; JCS sorts them for the bytes.

## Primitive types

| Type | JSON form | Rule |
| --- | --- | --- |
| `MicroTrlusd` | integer | Money, in micro-tRLUSD (tRLUSD is a test token): 1 tRLUSD = 1,000,000 micro-tRLUSD. Non-negative, at most 2**53 − 1. No floats, bools or strings, and no conversion to or from XRPL wire amounts |
| `Sha256Digest` | string | `sha256:` followed by 64 lower-case hex digits |
| `KeyId` | string | `ed25519:` followed by 64 lower-case hex digits (the public key) |
| `Ed25519Signature` | string | 128 lower-case hex digits. Carried, not verified, in C1A |
| `XrplTransactionHash` | string | 64 upper-case hex digits |
| `XrplLedgerObjectId` | string | 64 upper-case hex digits |
| `XrplAccount` | string | XRPL classic address, checksum-validated (format only) |
| `Nonce` | string | 32 lower-case hex digits (128 bits) |
| `CountryCode` | string | ISO 3166-1 alpha-2 shape: two upper-case ASCII letters. Membership in the assigned list is not checked |
| `Timestamp` | string | RFC 3339 UTC `YYYY-MM-DDTHH:MM:SSZ`: whole seconds, a literal `Z`, and a valid calendar time. No other spelling is accepted |
| `FacilityId` | string | Identifier: ASCII `[A-Za-z0-9][A-Za-z0-9._:-]{0,127}` |
| `QuoteId` | string | Identifier, same rule |
| `ProviderId` | string | Identifier, same rule |
| `ReservationId` | string | Identifier, same rule |
| `EngineVersion` | string | Identifier, same rule |

Other types:
- `int`: a JSON integer, non-negative and at most 2**53 − 1. A bool or float is refused. `availability_bps` is also capped at 10000 (10000 = 100.00%).
- `str`: non-empty text of Unicode scalar values that is already NFC. Non-NFC text is refused, not normalized.
- `tuple[X, ...]`: a JSON array. It is a **set** where marked "set": members must be strictly ascending by string value, so there are no duplicates and only one spelling. It is an **ordered list** where marked "ordered": order is preserved and part of identity.

## Null semantics

The meaning of null depends on the object. The two meanings below are deliberately different.

- **Provider-side fields: null means NOT ASSERTED.** A null here is never a wildcard and never satisfies any constraint.
  - In `credence.execution_profile` this applies to `model_artifact_digest`, `tokenizer_digest`, `chat_template_digest`, `runtime`, `precision` and `quantization`.
  - `quantization: "none"` asserts no quantization; `null` asserts nothing.
  - `adapter_digest` is null when there is no adapter.
- **Buyer-side fields: null means NO CONSTRAINT.** In `credence.procurement_class`, a null `allowed_precisions` or `allowed_quantizations` means the buyer imposes no constraint on that dimension (amended in C2A).
  - It is not an "any" value, and there is no wildcard string.
  - It never turns a provider's unasserted value into an assertion.
  - No other procurement member may be null.

How profiles are matched against procurement classes is in `contracts/POLICY-VERIFIER.md`.

## Closed vocabularies

| Vocabulary | Serialized values (in order) |
| --- | --- |
| `EvidenceTier` | `0` Requested, `1` Witnessed, `2` BoundAndSigned, `3` Measured |
| `DeliveryMode` | `"interactive"`, `"batch"`, `"reserved"` |
| `ServiceTier` | `"fast"`, `"base"` |
| `FacilityStatus` | `"pending"`, `"active"`, `"frozen"`, `"repaid"`, `"defaulted"` |
| `ReservationStatus` | `"held"`, `"settled"`, `"released"` |
| `Purpose` | `"inference"` |
| `SeedPolicy` | `"fixed"`, `"provider"`, `"none"` |
| `ResidencyRegion` | `"EU"`, `"US"` |
| `DecisionOutcome` | `"accept"`, `"reject"` |
| `DecisionStage` | `"policy"`, `"verifier"` |
| `ReasonCode` | `"SIG_INVALID"`, `"KEY_REVOKED"`, `"FACILITY_NOT_ACTIVE"`, `"FACILITY_EXPIRED"`, `"QUOTE_EXPIRED"`, `"PURPOSE_MISMATCH"`, `"PROVIDER_NOT_ALLOWED"`, `"PROFILE_NOT_ALLOWED"`, `"TIER_TOO_LOW"`, `"PRICE_ABOVE_MAX"`, `"OVER_LIMIT"`, `"NONCE_REUSED"`, `"RECEIPT_SIG_INVALID"`, `"BINDING_MISMATCH"`, `"RESPONSE_DIGEST_MISMATCH"`, `"TOKEN_COUNT_EXCEEDS_QUOTE"`, `"BILLED_AMOUNT_MISMATCH"`, `"CHAIN_BROKEN"`, `"REPLAYED_RECEIPT"` |

`ReasonCode` was amended in C2: `QUOTE_EXPIRED` and `BILLED_AMOUNT_MISMATCH` were added (decision 0002, amendment C2). No object schema changed. Which stage may emit which code, and in what order, is in `contracts/POLICY-VERIFIER.md`.

A value must match exactly, including its JSON type: `true` is not `1`, and `"1"` is not `1`. `EvidenceTier` keeps all four values; only `credence.delivery_receipt` restricts its own tier.

## Objects

Columns: the member name, its type (the "Null" column says whether `null` is allowed), and its meaning.

### `credence.execution_profile`

Class `ExecutionProfile`. This is the technical identity of what is bought. It has no price and no delivery terms.

| Member | Type | Null | Meaning |
| --- | --- | --- | --- |
| `model_identifier` | `str` | no | Model name as declared |
| `model_artifact_digest` | `Sha256Digest` | yes (not asserted) | Weights file digest; null unless the provider asserts it |
| `tokenizer_digest` | `Sha256Digest` | yes (not asserted) | Tokenizer digest |
| `adapter_digest` | `Sha256Digest` | yes (no adapter) | Adapter digest |
| `chat_template_digest` | `Sha256Digest` | yes (not asserted) | Chat template digest |
| `runtime` | `str` | yes (not asserted) | Engine name and version |
| `precision` | `str` | yes (not asserted) | For example `BF16` |
| `quantization` | `str` | yes (not asserted) | For example `none` |
| `context_min_tokens` | `int` | no | Minimum context length |
| `sampling` | `SamplingPolicy` | no | Nested object, below |

### `sampling`

Class `SamplingPolicy`, nested in `credence.execution_profile`, with no envelope.

| Member | Type | Null | Meaning |
| --- | --- | --- | --- |
| `temperature_milli` | `int` | yes (not asserted) | The actual execution/request temperature in thousandths: 700 means 0.700. Not an allowed range |
| `seed_policy` | `SeedPolicy` | no | `fixed` requires `seed`; `provider` and `none` require `seed` null |
| `seed` | `int` | yes | The fixed seed |

### `credence.service_class`

Class `ServiceClass`. This is what a provider promises around one execution profile. It has no price.

| Member | Type | Null | Meaning |
| --- | --- | --- | --- |
| `execution_profile_digest` | `Sha256Digest` | no | The profile delivered |
| `model_family` | `str` | no | Family used for procurement matching |
| `delivery_mode` | `DeliveryMode` | no | |
| `service_tier` | `ServiceTier` | no | |
| `jurisdiction` | `CountryCode` | yes (not asserted) | |
| `data_residency` | `tuple[CountryCode, ...]` | no | Set; empty means none asserted |
| `residency_region` | `ResidencyRegion` | yes (not asserted) | C1-only field |
| `evidence_tier` | `EvidenceTier` | no | Any of the four tiers |
| `latency_p95_ms` | `int` | yes (not asserted) | Milliseconds |
| `availability_bps` | `int` | yes (not asserted) | Basis points, 0 to 10000 |

### `credence.procurement_class`

Class `ProcurementClass`. This is what a buyer accepts as a substitute. It does not claim that profiles are identical. There is no precision ranking. Only `allowed_precisions` and `allowed_quantizations` may be null.

| Member | Type | Null | Meaning |
| --- | --- | --- | --- |
| `model_family` | `str` | no | |
| `allowed_precisions` | `tuple[str, ...]` | yes (unconstrained) | Set, non-empty when present; null means no buyer constraint |
| `allowed_quantizations` | `tuple[str, ...]` | yes (unconstrained) | Set, non-empty when present; null means no buyer constraint |
| `minimum_evidence_tier` | `EvidenceTier` | no | |
| `delivery_mode` | `DeliveryMode` | no | |
| `allowed_jurisdictions` | `tuple[CountryCode, ...]` | no | Set, non-empty |
| `allowed_data_residencies` | `tuple[CountryCode, ...]` | no | Set, non-empty |
| `allowed_providers` | `tuple[ProviderId, ...]` | no | Set, non-empty |

### `credence.quote`

Class `Quote`. This is a time-limited price for one service class.

| Member | Type | Null | Meaning |
| --- | --- | --- | --- |
| `quote_id` | `QuoteId` | no | |
| `provider_id` | `ProviderId` | no | |
| `service_class_digest` | `Sha256Digest` | no | |
| `input_price_per_million_micro_trlusd` | `MicroTrlusd` | no | Price per 1,000,000 input tokens |
| `output_price_per_million_micro_trlusd` | `MicroTrlusd` | no | Price per 1,000,000 output tokens |
| `max_input_tokens` | `int` | no | Input token cap |
| `max_output_tokens` | `int` | no | Output token cap |
| `valid_until` | `Timestamp` | no | |

### `credence.facility`

Class `Facility`. This is a purpose-bound inference credit facility. The repayment schedule is not modelled.

| Member | Type | Null | Meaning |
| --- | --- | --- | --- |
| `facility_id` | `FacilityId` | no | |
| `borrower_account` | `XrplAccount` | no | |
| `xrpl_loan_id` | `XrplLedgerObjectId` | yes (not originated) | |
| `limit_micro_trlusd` | `MicroTrlusd` | no | |
| `drawn_micro_trlusd` | `MicroTrlusd` | no | |
| `term_seconds` | `int` | no | |
| `maturity_at` | `Timestamp` | no | |
| `spread_bps` | `int` | no | |
| `allowed_providers` | `tuple[ProviderId, ...]` | no | Set, non-empty |
| `allowed_procurement_classes` | `tuple[Sha256Digest, ...]` | no | Set of procurement class digests, non-empty |
| `minimum_evidence_tier` | `EvidenceTier` | no | |
| `purpose` | `Purpose` | no | |
| `owner_approval_digest` | `Sha256Digest` | no | |
| `status` | `FacilityStatus` | no | |

### `credence.purchase_request`

Class `PurchaseRequest`. The prompt is held only as a digest. The agent's signature is a `credence.signed_object`, not a member.

| Member | Type | Null | Meaning |
| --- | --- | --- | --- |
| `facility_id` | `FacilityId` | no | |
| `provider_id` | `ProviderId` | no | |
| `quote_id` | `QuoteId` | no | |
| `purpose` | `Purpose` | no | Part of identity; only `"inference"` |
| `max_total_cost_micro_trlusd` | `MicroTrlusd` | no | |
| `prompt_digest` | `Sha256Digest` | no | |
| `nonce` | `Nonce` | no | |
| `expires_at` | `Timestamp` | no | |

### `credence.reservation`

Class `Reservation`. This is an internal accounting hold against a facility. It is not funds on XRPL. `expires_at` must be after `created_at`.

| Member | Type | Null | Meaning |
| --- | --- | --- | --- |
| `reservation_id` | `ReservationId` | no | |
| `facility_id` | `FacilityId` | no | |
| `quote_id` | `QuoteId` | no | |
| `maximum_amount_micro_trlusd` | `MicroTrlusd` | no | |
| `created_at` | `Timestamp` | no | |
| `expires_at` | `Timestamp` | no | |
| `status` | `ReservationStatus` | no | |

### `credence.provider_attestation`

Class `ProviderAttestationBody`. These are exactly the five members a Tier 2 provider attests to.

| Member | Type | Null | Meaning |
| --- | --- | --- | --- |
| `purchase_request_digest` | `Sha256Digest` | no | |
| `execution_profile_digest` | `Sha256Digest` | no | |
| `response_digest` | `Sha256Digest` | no | The response is held only as a digest |
| `input_token_count` | `int` | no | |
| `output_token_count` | `int` | no | |

### `credence.delivery_receipt`

Class `DeliveryReceiptBody`. This is the canonical receipt identity. Its digest is the receipt-chain and replay identity. It contains no signatures and no XRPL payment hash.

- `completed_at` must not precede `started_at`.
- `evidence_tier` must be `1` or `2`. This is a launch restriction on receipts only.
- At Tier 1, `provider_attestation_digest` must be null. At Tier 2 it must be present.

| Member | Type | Null | Meaning |
| --- | --- | --- | --- |
| `purchase_request_digest` | `Sha256Digest` | no | |
| `execution_profile_digest` | `Sha256Digest` | no | |
| `provider_id` | `ProviderId` | no | |
| `response_digest` | `Sha256Digest` | no | |
| `input_token_count` | `int` | no | |
| `output_token_count` | `int` | no | |
| `price_micro_trlusd` | `MicroTrlusd` | no | Billed price |
| `started_at` | `Timestamp` | no | |
| `completed_at` | `Timestamp` | no | |
| `nonce` | `Nonce` | no | |
| `evidence_tier` | `EvidenceTier` | no | `1` or `2` |
| `provider_attestation_digest` | `Sha256Digest` | yes (Tier 1) | Digest of the `credence.provider_attestation` |
| `previous_receipt_digest` | `Sha256Digest` | yes (first receipt) | Chains receipts |

### `credence.settlement_record`

Class `SettlementRecord`. This links a receipt and a decision record to the XRPL payment. The payment hash lives only here.

| Member | Type | Null | Meaning |
| --- | --- | --- | --- |
| `receipt_digest` | `Sha256Digest` | no | Digest of a `credence.delivery_receipt` |
| `decision_record_digest` | `Sha256Digest` | no | Digest of a `credence.decision_record` |
| `xrpl_transaction_hash` | `XrplTransactionHash` | no | |
| `settled_amount` | `MicroTrlusd` | no | |
| `settlement_timestamp` | `Timestamp` | no | |

### `credence.decision_record`

Class `DecisionRecord`. An `accept` has a null `reason_code`. A `reject` has exactly one.

| Member | Type | Null | Meaning |
| --- | --- | --- | --- |
| `decision_stage` | `DecisionStage` | no | Part of identity |
| `input_digests` | `tuple[Sha256Digest, ...]` | no | Ordered, non-empty; order is part of identity |
| `outcome` | `DecisionOutcome` | no | |
| `reason_code` | `ReasonCode` | yes (accept) | |
| `engine_version` | `EngineVersion` | no | |
| `decided_at` | `Timestamp` | no | |
| `authorized_transaction_digest` | `None` | always (C1A) | Must be null until the XRPL authorization/settlement checkpoint chooses the scheme |
| `authorized_transaction_digest_scheme` | `None` | always (C1A) | Must be null, as above |

### `credence.signed_object`

Class `SignedObject`. This is the signature wrapper. A signature is never a member of the body it signs. C1A neither creates nor verifies signatures.

| Member | Type | Null | Meaning |
| --- | --- | --- | --- |
| `object_digest` | `Sha256Digest` | no | Digest of the signed object |
| `key_id` | `KeyId` | no | |
| `signature` | `Ed25519Signature` | no | |

## Signatures, receipts and settlement

These describe the intended model. In C1A they are carried in the types and not checked cryptographically.

- **Signature wrappers.** Unsigned bodies carry identity. A signature is a separate `credence.signed_object` over the body's digest.
- **Who signs which body.** The agent signs a `credence.purchase_request`. A Tier 2 provider signs a `credence.provider_attestation`. The gateway signs a `credence.delivery_receipt`.
- **Receipt and settlement are separate.** The payment hash goes in `credence.settlement_record`, never in the receipt, so the receipt digest does not change when payment happens.

## Billing arithmetic

`credence.core.billing` uses integer ceiling division only:

- `input_cost = ceil(input_tokens × input_price_per_million_micro_trlusd / 1,000,000)`
- `output_cost = ceil(output_tokens × output_price_per_million_micro_trlusd / 1,000,000)`
- `total_cost = input_cost + output_cost`

Whether the counts are within the quote's caps is not checked here.

## Refusal codes

Every refusal names a stable code. Domain refusals (`DomainError`) also name the member, for example `sampling.seed`.

### Domain refusal codes

`BOOL_NOT_INTEGER`, `DECISION_REASON_MISMATCH`, `DUPLICATE_ENTRY`, `EMPTY_SET`, `EMPTY_VALUE`, `FLOAT_NOT_INTEGER`, `INVALID_ENUM`, `MALFORMED_ACCOUNT`, `MALFORMED_COUNTRY_CODE`, `MALFORMED_DIGEST`, `MALFORMED_IDENTIFIER`, `MALFORMED_KEY_ID`, `MALFORMED_LEDGER_OBJECT_ID`, `MALFORMED_NONCE`, `MALFORMED_SIGNATURE`, `MALFORMED_TIMESTAMP`, `MALFORMED_TX_HASH`, `MISSING_MEMBER`, `MUST_BE_NULL_IN_C1`, `NEGATIVE_VALUE`, `NON_SCALAR_UNICODE`, `NOT_AN_INTEGER`, `NOT_AN_OBJECT`, `NOT_A_LIST`, `NOT_A_STRING`, `NOT_NFC`, `OUT_OF_RANGE`, `SEED_POLICY_MISMATCH`, `TIER_ATTESTATION_MISMATCH`, `TIMESTAMP_ORDER`, `UNKNOWN_MEMBER`, `UNSORTED_SET`, `UNSUPPORTED_RECEIPT_TIER`, `UNSUPPORTED_SCHEMA_VERSION`, `WRONG_OBJECT_TYPE`, `WRONG_TYPE`

### Serializer adapter codes

`ADDRESS_MEMBER_RESERVED`, `DIGEST_MISMATCH`, `FLOAT_NOT_ADMITTED`, `JCS_REJECTED`, `NON_STRING_MEMBER_NAME`, `NOT_AN_OBJECT`, `UNSUPPORTED_TYPE`

### canon.py codes passed through unchanged

`BOM_PRESENT`, `DUPLICATE_MEMBER`, `INVALID_UTF8`, `MALFORMED_JSON`, `NFC_NAME_COLLISION`, `NON_FINITE`, `NON_SCALAR_UNICODE`, `NUMBER_NOT_INTEGRAL`, `NUMBER_OUT_OF_RANGE`

## Not in this contract

Policy rules, verification, signing, the reservation ledger's behaviour, the repayment schedule, conversion to XRPL wire amounts, the authorized-transaction digest scheme, and any matching of service classes against procurement classes.
