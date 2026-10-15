# INFERENCE-GATEWAY-CONTRACT

Owner: Inference (Claude Code). Status: Inference Checkpoint 1. Only the owner edits this file. Other workstreams file a request in `contracts/requests/`.

This contract describes the in-process inference gateway in `credence/inference/`:
- the gateway (`gateway.py`);
- the provider boundary (`provider.py`);
- payload digests and token-count source (`payload.py`);
- the receipt chain and the settled-receipt fact (`receipt_state.py`);
- the deterministic fake providers (`fake_providers.py`).

The code is authoritative. `tests/unit/test_inference_contract.py` checks the lists and tables here against the code. `tests/unit/test_inference_gateway.py` runs every behaviour below through the real Core engines. The fixtures are in `contracts/fixtures/inference_c1/`.

## Claim boundaries

| Tier | What a receipt at this tier shows | Who signs |
| --- | --- | --- |
| 1, Witnessed | Credence's gateway witnessed the delivery: the response digest it computed, the token counts and the profile digest the provider reported | Gateway |
| 2, Bound and signed | Tier 1, plus a `credence.provider_attestation` the provider signed: the provider signed what it says it served | Provider and gateway |
| 3, Measured | Not implemented | — |

- A Tier 1 provider signs nothing. Nothing at Tier 1 implies the provider signed or attested anything.
- Tier 2 is a provider's signed statement. It is not a measurement: no GPU execution proof, no loaded-artifact measurement, no TEE measurement.
- A model name alone verifies no weights. A profile field that is null is NOT ASSERTED, and the gateway never fills one in.
- Checkpoint 1 runs only the fake providers below. No real provider is called.

## Core dependency

The gateway imports and calls Core (checkpoint C3A, commit `3b095aebe034afc76f9817e073d1125f93259b82`). It does not modify, copy or reimplement any of it:
- objects: `credence.core.objects` (see `contracts/CORE-OBJECTS.md`);
- billing: `credence.core.billing.total_cost`;
- policy and reservation: `credence.core.reservation_ledger.ReservationLedger.reserve`, which runs `policy_engine.evaluate`;
- signatures: `credence.core.signatures` `sign`, `policy_signature_facts`, `verifier_signature_facts` (see `contracts/SIGNATURES.md`);
- verifier: `credence.core.verifier.verify` (see `contracts/POLICY-VERIFIER.md`).

## Provider request boundary

`ProviderRequest` is frozen. It holds only immutable Core objects for one approved purchase and the prompt bytes. A provider never receives the facility, the reservation, the key registry, the ledger or any decision record.

| Field | Type | Meaning |
| --- | --- | --- |
| `purchase_request` | `PurchaseRequest` | The agent-signed request being served |
| `execution_profile` | `ExecutionProfile` | The approved profile |
| `quote` | `Quote` | The approved quote, including its token caps |
| `prompt` | `bytes` | The prompt, plaintext and transient |

## Provider response boundary

A provider returns one `ProviderResult`. Adapters convert their provider's own wire format into it; no provider-specific shape reaches Core.

| Field | Type | Meaning |
| --- | --- | --- |
| `provider_id` | `ProviderId` | Must equal the ID of the provider the gateway called |
| `execution_profile_digest` | `Sha256Digest` | The profile the provider says it served |
| `response` | `bytes` | The response, plaintext and transient; well-formed UTF-8 |
| `input_token_count` | `int` or None | Provider-reported; None means not reported |
| `output_token_count` | `int` or None | Provider-reported; None means not reported |
| `attestation` | `ProviderAttestation` or None | Body plus signature wrapper (signature may be None); only from an attesting provider |

A result carries no price. The gateway computes the price.

`AttestationCapability` values: `none`, `signs_attestation`.
- `none`: the provider must not send an attestation.
- `signs_attestation`: the provider must send one.

A provider signals failure by raising `ProviderUnavailableError` or `ProviderExecutionError` (see Failure behaviour).

## Prompt and response digests

`prompt_digest` and `response_digest` are payload digests:

`payload_digest(data) = "sha256:" + lower-case hex SHA-256 of the exact bytes`

- The payload must be well-formed UTF-8. Nothing is normalized, wrapped or re-encoded first: no NFC, no JSON and no Kessai serializer.
- So any byte change changes the digest, including an NFC-equivalent rewrite.
- Prompts and responses are not canonical Credence objects. The Kessai serializer, JCS (RFC 8785) + NFC, applies to the canonical objects only.

Plaintext boundary:
- The prompt and the response exist in plaintext only on the execution path: the caller, the gateway and the provider.
- Canonical evidence objects hold only their digests: the purchase request, attestation, receipt, signatures and decision records.
- The gateway computes `prompt_digest` itself and refuses a prompt that does not match the signed request.
- It computes `response_digest` itself from the bytes it received. The verifier's `delivered_response_digest` is computed again from the bytes being delivered.
- The provider sees the prompt. Nothing here hides the prompt from the provider.

## Token accounting

`TokenCountSource` values: `provider_reported`.

- In Checkpoint 1, every receipt's token counts are the provider-reported counts, copied unchanged, and the evidence records `provider_reported`.
- The gateway runs no tokenizer, so it does not verify counts independently. A result without both counts is a malformed provider response.
- The Core verifier checks the counts against the quote's caps (`TOKEN_COUNT_EXCEEDS_QUOTE`).
- At Tier 2 the provider signs the same counts in its attestation, and the verifier checks that they match the receipt.
- The fake providers count one token per started 4 bytes. That is a synthetic rule, not any real tokenizer.

## Purchase flow

`Gateway.purchase(Purchase)` runs these steps in order. `Purchase` carries the signed request, its signature wrapper, the prompt and the Core objects it is approved against: facility, quote, service class, execution profile and procurement classes.

1. **Prompt check.** Digest the prompt. If it is not UTF-8, return `Refused` with `PROMPT_NOT_UTF8`. If it differs from `request.prompt_digest`, return `Refused` with `PROMPT_DIGEST_MISMATCH`. Either way there is no Core call, no reservation and no provider call.
2. **Signature facts.** Call `policy_signature_facts` on the request and its signature (C3A).
3. **Reserve.** Call `ReservationLedger.reserve`, which runs the C2 policy engine. On reject, return `PolicyRejected` with the policy decision. The provider is never called.
4. **Find the provider** named by `request.provider_id`. If none is connected, return `ProviderFailed` with `PROVIDER_UNAVAILABLE`.
5. **Execute.** Read `started_at` from the clock, call `provider.execute(ProviderRequest)`, then read `completed_at`.
6. **Check the result's shape.** On failure, return `ProviderFailed` with `PROVIDER_MALFORMED_RESPONSE`.
7. **Build and sign the evidence.** Build the receipt (see Evidence construction) and sign it with the gateway key through C3A `sign`.
8. **Verify.** Call `verifier_signature_facts`, then the C2 `verify` (see Verification inputs).
9. **Report.** On reject, return `DeliveryRejected` and withhold the response. On accept, move the facility's chain head to this receipt and return `DeliveryVerified` with the response.

The gateway decides no policy or verification outcome itself. `DeliveryVerified` refuses to exist without a Core verifier `accept`, and `PolicyRejected` without a Core policy `reject`.

## Evidence construction

### ExecutionProfile, ServiceClass and Quote

- The gateway passes the approved execution profile, service class and quote through unchanged.
- Their relationships are Core's to check: quote → service class digest → profile digest, and procurement class membership. The policy engine checks them at reserve; the verifier checks them again.
- The receipt's `execution_profile_digest` is the profile the provider *reported*, not the approved one. A provider that serves another profile is caught by the verifier as `BINDING_MISMATCH`.

### ProviderAttestationBody construction

- The attesting provider builds the body for what it served:
  - `purchase_request_digest` of the request it received;
  - its own `execution_profile_digest`;
  - `response_digest` (the payload digest of its response);
  - its two token counts.
- It signs the body through C3A `sign` under its `provider` key.
- The gateway does not build, change or sign provider attestations. It only puts the body's digest in the receipt.

### DeliveryReceiptBody construction

| Member | Source |
| --- | --- |
| `purchase_request_digest` | Digest of the signed purchase request |
| `execution_profile_digest` | Provider-reported profile digest |
| `provider_id` | The provider the gateway called |
| `response_digest` | Payload digest of the response bytes, computed by the gateway |
| `input_token_count` | Provider-reported |
| `output_token_count` | Provider-reported |
| `price_micro_trlusd` | `credence.core.billing.total_cost(quote, input_token_count, output_token_count)`, in micro-tRLUSD (tRLUSD is a test token) |
| `started_at` | Gateway clock before `execute` |
| `completed_at` | Gateway clock after `execute` |
| `nonce` | The request's nonce |
| `evidence_tier` | `1` if the result has no attestation, `2` if it has one |
| `provider_attestation_digest` | Digest of the attestation body, or null at Tier 1 |
| `previous_receipt_digest` | The facility's chain head, or null for its first receipt |

Whether the receipt's tier matches the approved service class is the verifier's decision (`BINDING_MISMATCH`).

### Receipt chaining

- `ReceiptChain` keeps each facility's last accepted receipt digest in process memory.
- A facility's first receipt has `previous_receipt_digest` null. Each later receipt binds the previous *accepted* receipt.
- Only a `DeliveryVerified` moves the head, by compare-and-set against the head the receipt was built on. A rejected delivery or a provider failure leaves it unchanged.
- There is no persistence in Checkpoint 1.

### Verification inputs

`Gateway.verify_delivery(evidence, delivered_response)` feeds the real evidence to Core and does not move the chain head:
- `gateway_signature_valid` and `provider_signature` come from `verifier_signature_facts`;
- `delivered_response_digest` is the payload digest of `delivered_response`;
- `expected_previous_receipt_digest` is the chain head for the request's facility;
- `receipt_already_settled` comes from `SettledReceipts.is_settled(receipt digest)`;
- `evaluated_at` comes from the gateway clock.

The Checkpoint 1 gateway only reads `SettledReceipts` and never marks a receipt settled.

Core checks `CHAIN_BROKEN` before `REPLAYED_RECEIPT`. So a settled receipt resubmitted after the head has moved past it is rejected as `CHAIN_BROKEN`. `REPLAYED_RECEIPT` is reached only when the settled fact is true while the head still precedes the receipt.

## Signing responsibilities

| Signer | Signs | C3A role | Key held by |
| --- | --- | --- | --- |
| Agent | `credence.purchase_request` | `agent` | The caller; the gateway only checks the signature |
| Gateway | `credence.delivery_receipt` | `gateway` | The gateway, as an in-memory signing key |
| Tier 2 provider | `credence.provider_attestation` | `provider` | The provider |

The gateway's key must be registered in the C3A key registry under the `gateway` role, or every receipt fails `RECEIPT_SIG_INVALID`.

## Outcomes

| Outcome | When | Reservation | Provider called |
| --- | --- | --- | --- |
| `Refused` | Prompt check failed | None created | No |
| `PolicyRejected` | Core policy rejected | None created | No |
| `ProviderFailed` | Operational provider failure | Held | Possibly |
| `DeliveryRejected` | Core verifier rejected | Held | Yes |
| `DeliveryVerified` | Core verifier accepted | Held | Yes |

`ProviderFailed`, `DeliveryRejected` and `DeliveryVerified` carry the accepting policy decision behind their reservation.

## Failure behaviour

Gateway refusal codes: `PROMPT_NOT_UTF8`, `PROMPT_DIGEST_MISMATCH`.

Provider failure kinds: `PROVIDER_UNAVAILABLE`, `PROVIDER_EXECUTION_ERROR`, `PROVIDER_MALFORMED_RESPONSE`.

What causes each provider failure:
- `PROVIDER_UNAVAILABLE`: no provider connected for the request's provider ID, or the provider raised `ProviderUnavailableError`.
- `PROVIDER_EXECUTION_ERROR`: the provider raised any other exception, including `ProviderExecutionError`.
- `PROVIDER_MALFORMED_RESPONSE`: any of the following:
  - the result is not a `ProviderResult`;
  - it names another provider;
  - the profile digest is not a `Sha256Digest`;
  - the response is not bytes of well-formed UTF-8;
  - a token count is missing;
  - the attestation does not match the provider's capability or is mistyped;
  - a receipt cannot be built from the result (for example a negative, bool or out-of-range count).

What these codes are not:
- They are not Core reason codes. No decision record is created for them.
- They do not settle, release or expire the reservation. It stays held and lapses by Core's own expiry.
- Release, disputes and settlement belong to a later checkpoint (C3B).

## Fake providers

These are deterministic and in process, with no network or credentials:

| Class | Provider ID | Capability | Behaviour |
| --- | --- | --- | --- |
| `FakeWitnessedProvider` | `synthetic-witnessed-provider` | `none` | Fixed response, synthetic token counts, its configured profile; signs nothing |
| `FakeSigningProvider` | `synthetic-signing-provider` | `signs_attestation` | Same, plus a C3A-signed attestation over its own result |

- The IDs are explicitly synthetic. Neither fake stands for a real provider.
- The Tier 1 fake does not model Nebius behaviour.
- The Tier 2 fake runs no model and hashes no weights file.

## Fixtures

`contracts/fixtures/inference_c1/` holds one complete Tier 1 delivery and one complete Tier 2 delivery, plus a `MANIFEST.json` of their SHA-256s:
- each holds the synthetic prompt and response text, every canonical object, the TEST key registry (public key IDs only) and the expected digests;
- `contracts/fixtures/build_inference_c1_fixtures.py` regenerates them byte-identically;
- `tests/conformance/test_inference_c1_fixtures.py` re-verifies each one through Core alone.

## Not in this contract

- Quoting: there is no 402 step, and `gateway.quote` is not implemented.
- Real providers: no Nebius calls (`provider_a.py` is a stub), and no local model or sidecar process (`provider_b_sidecar.py` is a stub).
- ResearchBot.
- Settlement: no settlement, reservation release, disputes, XRPL payments, LoanSet or LoanPay.
- Persistence: none for chain heads or settled receipts.
- Interfaces: no rate limiting, HTTP or FastAPI API, or frontend.
- Credentials of any kind.
