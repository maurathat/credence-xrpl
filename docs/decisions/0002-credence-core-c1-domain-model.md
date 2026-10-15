# 0002. Credence Core C1: domain model and serializer binding

- Status: Accepted
- Date: 2026-10-14
- Scope: Credence Core checkpoint C1 only (typed objects, serializer binding, conformance vectors)
- Source: `docs/spec/credence-build-plan.md`, decision 0001, and Maura's C1 brief and decisions of 2026-10-14, amended by the C1A contract freeze of the same date (see Amendments)

## Serializer

1. **Kessai serializer: JCS (RFC 8785) with NFC normalization, bound using the SCITT Canonical Payload Binding `jcs` construction.** The short form everywhere else is "Kessai serializer: JCS (RFC 8785) + NFC". NFC is not part of RFC 8785. It is applied to every string value and member name before JCS serialization, so a plain JCS implementation can produce different bytes for non-normalized input.
2. **C1 uses the pinned custody implementation.** The authoritative code is `third_party/custody/impl/canon.py` at custody `5dc15ff6fb097dc5f4a47e72969fae4a296620a5` (sha256 `48d6ad1c92a292b1979ef05bc3010007ba0120e5da0d3be5f3f4f9b77902eb8d`, vendored from canonical-dataset-lineage). Its public `dataset_id` accepts only the `dataset.v1` shape. So `credence/core/kessai.py` follows the pattern custody's own `impl/handoff.py` uses for its second payload class: it calls `canon._admit`, then `canon._fold_nfc`, then `canon._identifier`. canon.py does not return canonical bytes, so the bytes are `rfc8785.dumps` of the folded value, the same call with the same pinned `rfc8785==0.1.4`. They are released only if their SHA-256 equals `canon._identifier`.
3. **No placeholder serializer exists, and nothing is copied or reimplemented.** `kessai.py` contains no canonicalization algorithm. It refuses to load canon.py unless the file matches the pinned hash and the installed rfc8785 is 0.1.4. It executes only the verified bytes.
4. **The adapter fails closed and never coerces.** It refuses Python floats (canon admits `1.0` as `1`), non-JSON types, a non-object top level, and a top-level `address` member (canon would drop it from the digest without any error). canon's own rejection codes are passed through unchanged.
5. **No `kessai-canon` package is created in this checkpoint.** Decision 0001's open blocker stands for the general package. C1 depends on canon.py's private step functions, and moving to `kessai-canon` later will require re-checking every vector.

## Domain model

6. **Application money is integer micro-tRLUSD.** `MicroTrlusd` is a non-negative `int` within ±(2**53 − 1); 1 tRLUSD = 1,000,000 micro-tRLUSD. Floats, bools and strings are refused.
7. **Conversion to XRPL wire amounts is unresolved and out of scope.** There is no implicit conversion in either direction.
8. **Billing arithmetic is frozen as integer-only.**
   - `input_cost = ceil(input_tokens × input_price_per_million_micro_trlusd / 1,000,000)`, with the same formula for output.
   - `total_cost = input_cost + output_cost`.
   - Implemented with integer ceiling division in `credence/core/billing.py`. Checks against quote caps belong to C2.
9. **Unsigned bodies carry identity.**
   - Signatures live only in `SignedObject {object_digest, key_id, signature}`, never inside the body they sign.
   - `DeliveryReceiptBody` is the receipt identity, and its digest is the chain and replay identity. It carries no signatures and no payment hash.
   - `SettlementRecord` carries the XRPL transaction hash.
   - A Tier 2 provider signs `ProviderAttestationBody`, which has exactly five members: request digest, profile digest, response digest, input tokens and output tokens. The gateway signs `DeliveryReceiptBody`. A Tier 1 receipt has no attestation digest, and a Tier 2 receipt must have one.
10. **Null execution fields mean NOT ASSERTED.** This applies to `model_artifact_digest`, `tokenizer_digest`, `chat_template_digest`, `runtime`, `precision` and `quantization` (the two digests were added in C1A). Null is never a wildcard, and it never satisfies a procurement requirement for that field. A provider whose model name maps to public weights does not claim the artifact digest. `ExecutionProfile.model_identifier` is required.
11. **Procurement matching uses membership, not a ranking.**
    - `ServiceClass.model_family` matches `ProcurementClass.model_family`.
    - Precision and quantization match through `allowed_precisions` and `allowed_quantizations`. There is no precision ranking.
12. **The DecisionRecord transaction digest is not chosen.** `authorized_transaction_digest` and `authorized_transaction_digest_scheme` exist and must be null in C1. The scheme is chosen at the XRPL authorization/settlement checkpoint.
13. **Representation choices made in C1:**
    - Timestamps are RFC 3339 UTC, whole seconds, with a literal `Z`.
    - Digests are `sha256:` plus 64 lower-case hex digits.
    - Key IDs are `ed25519:` plus the public key in hex. Signatures are 128 lower-case hex digits.
    - XRPL hashes and ledger object IDs are 64 upper-case hex digits.
    - Nonces are 32 lower-case hex digits.
    - Application identifiers use ASCII `[A-Za-z0-9][A-Za-z0-9._:-]{0,127}`.
    - Country codes are ISO 3166-1 alpha-2 in shape. Membership in the assigned list is not checked.
    - Domain text must already be NFC. Each canonical object carries `object_type` and `schema_version: "1"`.
    - Unordered collections are tuples given strictly ascending, so duplicates and alternative orders are refused rather than repaired. `allowed_*` sets are non-empty. `DecisionRecord.input_digests` is ordered.
14. **Out of scope for C1:** policy rules, verification, signing, the reservation ledger, the repayment schedule, XRPL, inference, API and UI.

## Consequences

- **Vectors:** `vectors/c1/` holds the conformance vectors, generated by `vectors/build_c1_vectors.py` through the real serializer and checked by `tests/conformance/test_core_c1_vectors.py`.
- **Interface contract:** `contracts/CORE-OBJECTS.md` is the frozen contract for these types as of C1A. `tests/unit/test_core_contract.py` checks it against the code and the vectors.

## Amendments

### C1A contract freeze (2026-10-14)

15. **PurchaseRequest gains `purpose`.** It uses the closed `Purpose` vocabulary, and its only value is `"inference"`. It is part of the canonical identity.
16. **DecisionRecord gains `decision_stage`.** It uses the closed `DecisionStage` vocabulary (`"policy"`, `"verifier"`) and is part of the canonical identity.
17. **`tokenizer_digest` and `chat_template_digest` are nullable.** Null means NOT ASSERTED, with the same meaning as item 10.
18. **The generic `EvidenceTier` keeps all four values (0 to 3).** Only `DeliveryReceiptBody` restricts itself to Tier 1 or Tier 2, as a launch restriction.
19. **`temperature_milli` is unchanged.** It records the actual execution/request temperature. Allowed temperature ranges belong to later service/procurement constraints and are not modelled.
20. **The contract is published and checked.** `contracts/CORE-OBJECTS.md` describes the implemented schemas and is checked by `tests/unit/test_core_contract.py`. Vectors regenerate deterministically, and `tests/conformance/test_core_c1_vectors.py` requires the committed files to equal a fresh regeneration.

### C2 amendment to the C1A contract (2026-10-14)

21. **`ReasonCode` gains two values.** `QUOTE_EXPIRED` (policy stage, placed immediately after `FACILITY_EXPIRED`) and `BILLED_AMOUNT_MISMATCH` (verifier stage, placed after `TOKEN_COUNT_EXCEEDS_QUOTE` and before `CHAIN_BROKEN`). This is an explicit, bounded amendment of the C1A contract; C1A was not otherwise changed. No object schema, member, nullability or other vocabulary changed, and every pre-existing vector is byte-identical. Two vectors were added (`decision_record-quote-expired` and `decision_record-billed-amount-mismatch`). Stage ownership and precedence are in decision 0003 and `contracts/POLICY-VERIFIER.md`.
