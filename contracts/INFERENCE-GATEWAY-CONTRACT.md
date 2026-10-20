# INFERENCE-GATEWAY-CONTRACT

Owner: Inference (Claude Code). Status: Inference Checkpoint 3 (Nebius Tier 1 provider and the two-provider Qwen market added). Only the owner edits this file. Other workstreams file a request in `contracts/requests/`.

This contract describes the in-process inference gateway in `credence/inference/`:
- the gateway (`gateway.py`);
- the provider boundary (`provider.py`);
- payload digests and token-count source (`payload.py`);
- the receipt chain and the settled-receipt fact (`receipt_state.py`);
- the deterministic fake providers (`fake_providers.py`);
- the local Tier 2 providers (`local_llama.py`);
- the Nebius Tier 1 provider (`nebius.py`);
- routing among Core-qualified quotes (`market.py`).

The code is authoritative. `tests/unit/test_inference_contract.py` checks the lists and tables here against the code. `tests/unit/test_inference_gateway.py` runs every behaviour below through the real Core engines. `tests/unit/test_inference_local_llama.py` covers the local provider without the model, and `tests/integration/test_local_llama.py` runs it with the real model. Fixtures and evidence are in `contracts/fixtures/` (see Fixtures and evidence).

## Claim boundaries

| Tier | What a receipt at this tier shows | Who signs |
| --- | --- | --- |
| 1, Witnessed | Credence's gateway witnessed the delivery: the response digest it computed, the token counts and the profile digest the provider reported | Gateway |
| 2, Bound and signed | Tier 1, plus a `credence.provider_attestation` the provider signed: the provider signed what it says it served | Provider and gateway |
| 3, Measured | Not implemented | — |

- A Tier 1 provider signs nothing. Nothing at Tier 1 implies the provider signed or attested anything.
- Tier 2 is a provider's signed statement. It is not a measurement: no GPU execution proof, no loaded-artifact measurement, no TEE measurement.
- A model name alone verifies no weights. A profile field that is null is NOT ASSERTED, and the gateway never fills one in.
- The real providers are the local Tier 2 providers (see Local Tier 2 provider) and Nebius at Tier 1 (see Nebius Tier 1 provider).

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
- `PROVIDER_EXECUTION_ERROR`: the provider raised `ProviderExecutionError`.
- `PROVIDER_MALFORMED_RESPONSE`: any of the following:
  - the result is not a `ProviderResult`;
  - it names another provider;
  - the profile digest is not a `Sha256Digest`;
  - the response is not bytes of well-formed UTF-8;
  - a token count is missing;
  - the attestation does not match the provider's capability or is mistyped;
  - a receipt cannot be built from the result (for example a negative, bool or out-of-range count).

Any other exception raised inside a provider is a programming error, not a provider outcome. The gateway does not catch it: it propagates to the caller, and the reservation already taken stays held. A future public API must translate it into an opaque internal error and must not expose its message or stack trace.

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

## Service location (jurisdiction and residency)

Core leaves the general meaning of `ServiceClass.jurisdiction` undefined. `CORE-OBJECTS.md` gives its type, `CountryCode`, but no meaning. This section defines only the meaning the Inference workstream uses for the service classes it builds for this demo. It is not a Credence-wide or legal definition, and it changes nothing in Core.

- **`jurisdiction`** is the country in which the inference execution for that service occurs. It does not mean the provider's place of incorporation, its headquarters, or a governing-law jurisdiction.
- **`data_residency`** is the set of countries in which the service's execution takes place, under the same meaning.

| Provider | `jurisdiction` | `data_residency` | Provenance |
| --- | --- | --- | --- |
| Nebius | `FI` | `{FI}` | Nebius-declared service-region metadata from its live catalog: the selected model's region `eu-north1`, `country_code` `FI`. Not Nebius's legal or corporate jurisdiction. |
| Local provider | `US` | `{US}` | An operator-supplied deployment fact: the local execution machine physically operates in the United States. Not inferred from fixtures, IP geolocation or the operating system. |

**Buyer allowlists.** A procurement class's `allowed_jurisdictions` and `allowed_data_residencies` are buyer allowlists. The general marketplace class allows `{FI, US}` for both. That says the buyer accepts execution in either country; it is not a claim that any provider operates in both.

**How frozen Core matches them:**
- Nebius: `FI` ∈ `{FI, US}`, and `{FI}` ⊆ `{FI, US}`.
- Local: `US` ∈ `{FI, US}`, and `{US}` ⊆ `{FI, US}`.

## Local Tier 2 provider

`LocalLlamaProvider` serves a model from a local Ollama runtime. It is the build plan's Provider B in process: the provider's signing key is held by the provider object, separate from the gateway key, but in the same process. A separate sidecar process is not implemented.

| Provider ID | Model family | Local artifact (Ollama library tag) |
| --- | --- | --- |
| `local-llama-provider` | Llama 3.1 8B Instruct (frozen at Checkpoint 2) | `llama3.1:8b-instruct-fp16` |
| `local-qwen-provider` | `qwen3-30b-a3b-instruct-2507` | `qwen3:30b-a3b-instruct-2507-q8_0` |

Both use the same binding and execution rules below. Each local execution artifact is an Ollama library GGUF conversion, not the model publisher's original files.

### Artifact identity

- **One file, one digest.** The artifact is the single GGUF file that the Ollama manifest for one library tag names as its one model layer. `model_artifact_digest` is SHA-256 of that file's exact bytes.
- **Checked at bind time.** `bind_local_artifact` streams the file and refuses (`ArtifactBindingError`) unless all of these hold:
  - its size and digest equal the manifest's content address;
  - there is exactly one model layer;
  - there is no adapter layer.
- **Checked before every execution.** The provider re-reads the manifest. It refuses (`ProviderUnavailableError`) if the tag now names a different model or template file than was bound. The file is not re-hashed on each request.
- **`model_identifier`** is `ollama:` followed by the tag.

### Runtime, template and tokenizer

| Profile member | Value |
| --- | --- |
| `runtime` | `ollama` and the version the runtime reports at bind time |
| `chat_template_digest` | SHA-256 of the manifest's template file only when the template the runtime reports it will use (`/api/show` `template`) is byte-for-byte that file. Otherwise null (NOT ASSERTED): a GGUF file may embed its own chat template, and the runtime may render with it instead. The runtime-reported template's SHA-256 is kept as an observation (`runtime_template_sha256`), never as a profile fact |
| `tokenizer_digest` | Null (NOT ASSERTED): the tokenizer is embedded in the GGUF file, so it has no separate bytes; the artifact digest covers the file it lives in |
| `adapter_digest` | Null: no adapter (bind refuses one) |
| `precision`, `quantization` | From the file's own metadata, never the tag. `F16` and `none` only when the file's GGUF `general.file_type` is 1 (mostly F16) and every tensor the runtime reports is F16 or F32. Quantization `Q8_0` with precision null only when `general.file_type` is 7 (mostly Q8_0) and every tensor is Q8_0 or F32, at least one Q8_0; precision is not asserted because Q8_0 stores 8-bit blocks with per-block scales. Otherwise both null (NOT ASSERTED) |
| `context_min_tokens` | The context window the provider configures (`num_ctx`) |
| `sampling` | The temperature and fixed seed the provider sends on every request |

The provider only talks to a loopback `http` address (`LoopbackOllamaTransport`). It calls `/api/chat` with one user message (the prompt), `stream` false, its configured temperature, seed and context window, and `num_predict` equal to the quote's `max_output_tokens`.

### Token-count provenance

- The counts are the runtime's own: `prompt_eval_count` (input, including cached prompt tokens) and `eval_count` (output).
- The provider reports them and signs them in its attestation. The gateway copies them (`provider_reported`).
- The runtime exposes no tokenizer endpoint, and the gateway runs no tokenizer, so nothing counts them independently.

### Provider signature

The provider signs every `credence.provider_attestation` through C3A `sign` with its own key. Its key ID is registered under the `provider` role with its provider ID as subject. The key is generated at runtime and never written. Only public key IDs and signatures appear in committed evidence.

### Claim boundary

**Artifact binding is not execution measurement.** For the local provider, Credence may say:
- the provider selected and configured this exact file, by digest;
- it ran the request through the recorded runtime;
- it returned these bytes;
- it reports these token counts;
- it signed the attestation.

Credence must not say any of the following:
- that these weights were resident in GPU memory;
- that loaded state was measured;
- that a TEE attested anything;
- that there is physical-device execution proof;
- that this is Tier 3.

Nothing here says the local file is what any other provider runs. The only assertion shared with another provider is the model family. The local Qwen file is not claimed to equal whatever Nebius runs.

### Runtime failures

| Runtime condition | Provider raises | Gateway outcome |
| --- | --- | --- |
| Runtime unreachable, model not found (HTTP 404), or the bound artifact changed or is gone | `ProviderUnavailableError` | `PROVIDER_UNAVAILABLE` |
| Other HTTP error, timeout, a body that is not a JSON object, or a reply naming another model, not done, without content, or without valid counts | `ProviderExecutionError` | `PROVIDER_EXECUTION_ERROR` |

## Nebius Tier 1 provider

`NebiusProvider` (provider ID `nebius-token-factory`) is the build plan's Provider A. It calls Nebius Token Factory's public API; no partnership is claimed, and Credence pays Nebius through normal billing. It is Tier 1: Credence's gateway witnessed Nebius's response and signs the receipt. Nebius signs no `credence.provider_attestation`, so the result carries no attestation and the provider signature fact is `not_present`.

### Credential boundary

- The key enters only through the `NEBIUS_API_KEY` environment variable, supplied by the operator. Credence code never invokes a password manager and never stores, logs, returns or includes the key in an error.
- A key with leading or trailing whitespace, or any whitespace or control character, is refused before any network call (`credential_malformed`). It is never silently stripped. A missing key is `credential_missing`.
- `HttpsNebiusTransport` sends requests only to `https://api.tokenfactory.nebius.com/v1` with `Authorization: Bearer <key>`, and refuses redirects so the header cannot be forwarded to another host.
- Live calls also require `CREDENCE_NEBIUS_LIVE=1`. Without it, and without the key, live tests and recorders do not run.

### Profile and catalog metadata

| Profile member | Value |
| --- | --- |
| `model_identifier` | The exact Nebius model ID, `Qwen/Qwen3-30B-A3B-Instruct-2507`, confirmed in Nebius's live catalog |
| `model_artifact_digest`, `tokenizer_digest`, `adapter_digest`, `chat_template_digest`, `runtime`, `precision`, `quantization` | Null (NOT ASSERTED) |
| `context_min_tokens` | The context the Credence quote relies on (8192), not a Nebius claim |
| `sampling` | Temperature 0 as sent; seed policy `provider`, seed null (Nebius exposes no seed parameter) |

Nebius's catalog metadata (for example `quantization: fp8`, pricing strings, rate limits) is a provider declaration. It is recorded as metadata in sanitized catalog observations and never put into the ExecutionProfile.

### Request and token counts

- `POST /chat/completions` with the model ID, one user message, `max_tokens` equal to the quote's `max_output_tokens`, `temperature` 0, `stream` false, and no seed.
- The reply must name the same model, have exactly one choice with string content, and carry `usage`.
- `input_token_count` is `usage.prompt_tokens`; `output_token_count` is `usage.completion_tokens`. Both are provider-reported.
- A non-zero `usage.prompt_tokens_details.cached_tokens` or `usage.completion_tokens_details.reasoning_tokens` is refused (`unsupported_usage`), because its relation to the totals is not established.

### Failures

| Condition | Category | Raised as | Gateway outcome |
| --- | --- | --- | --- |
| Key missing or malformed | `credential_missing`, `credential_malformed` | `ProviderUnavailableError` | `PROVIDER_UNAVAILABLE` |
| HTTP 401 or 403 | `authentication` | `ProviderUnavailableError` | `PROVIDER_UNAVAILABLE` |
| HTTP 402 | `payment_required` | `ProviderUnavailableError` | `PROVIDER_UNAVAILABLE` |
| HTTP 404 | `model_unavailable` | `ProviderUnavailableError` | `PROVIDER_UNAVAILABLE` |
| HTTP 429 | `rate_limited` | `ProviderUnavailableError` | `PROVIDER_UNAVAILABLE` |
| HTTP 503 or 529 | `service_unavailable` | `ProviderUnavailableError` | `PROVIDER_UNAVAILABLE` |
| Host unreachable | `unreachable` | `ProviderUnavailableError` | `PROVIDER_UNAVAILABLE` |
| HTTP 500 or other 5xx | `server_error` | `ProviderExecutionError` | `PROVIDER_EXECUTION_ERROR` |
| HTTP 400, 422 or other 4xx | `request_rejected` | `ProviderExecutionError` | `PROVIDER_EXECUTION_ERROR` |
| Timeout | `timeout` | `ProviderExecutionError` | `PROVIDER_EXECUTION_ERROR` |
| Body not a JSON object, or an unusable reply | `malformed_response` | `ProviderExecutionError` | `PROVIDER_EXECUTION_ERROR` |
| Cached or reasoning tokens reported | `unsupported_usage` | `ProviderExecutionError` | `PROVIDER_EXECUTION_ERROR` |

Error messages carry only the category and HTTP status, never the key or the response body.

## Market routing

`Gateway.select(candidates)` and `Gateway.purchase_best(candidates)` choose among quotes for one job. This is not a policy engine.

- **Candidates.** Each candidate is a complete signed purchase for the same facility and the same prompt, one per offered quote.
- **Eligibility is Core's.** Every candidate goes through the real `policy_engine.evaluate` with its real C3A signature facts and the ledger's current headroom and nonce use, as a dry run: nothing is reserved and no decision is stored.
- **Routing rule.** Among the candidates Core accepted: lowest maximum cost (Core's `max_total_cost_value` for the quote), then provider ID, then quote ID.
- **No override.** A candidate Core rejected is never chosen, however cheap. When no candidate qualifies, nothing is reserved and no provider is called.
- **Authority.** The chosen purchase then runs the normal purchase flow, where `ReservationLedger.reserve` decides again.
- **Quotes.** Market quotes are configured demo values in micro-tRLUSD (tRLUSD is a test token). They are not Nebius's billed price; Nebius's catalog pricing strings have no established unit and are not converted.

## Fixtures and evidence

`contracts/fixtures/inference_c1/` holds one complete Tier 1 delivery and one complete Tier 2 delivery, plus a `MANIFEST.json` of their SHA-256s:
- each holds the synthetic prompt and response text, every canonical object, the TEST key registry (public key IDs only) and the expected digests;
- `contracts/fixtures/build_inference_c1_fixtures.py` regenerates them byte-identically;
- `tests/conformance/test_inference_c1_fixtures.py` re-verifies each one through Core alone.

These are **deterministic protocol fixtures**.

`contracts/fixtures/inference_c2/local-llama-tier2-observation.json` is **runtime-observation evidence** from one real local run:
- **Not regenerable.** It carries wall-clock timestamps, runtime timings and a provider key generated for that run.
- **Contents:**
  - artifact provenance by content address;
  - the bound profile;
  - every canonical object;
  - the public key registry;
  - the synthetic prompt;
  - the observed counts and timings;
  - whether a repeat of the same prompt gave the same response digest.
- **Left out:** the response text (only its digest is kept), model weights, private keys and machine paths.
- **How it is checked:** `contracts/fixtures/record_local_llama_observation.py` writes it, and `tests/conformance/test_inference_c2_observation.py` re-verifies its digests, signatures and verifier decision through Core alone. Because the response text is not committed, the delivered-response digest in that check is the recorded one, not an independent one.

Checkpoint 3 evidence is in `contracts/fixtures/inference_c3/`:

| File | Kind | Written by | Checked by |
| --- | --- | --- | --- |
| `market-selection-deterministic.json` | **Deterministic protocol fixture**: Core's dry-run qualification and the routing choice for both price-vs-evidence scenarios, built from the real profiles and demo quotes; no provider is called | `build_inference_c3_market_fixture.py` | `test_inference_c3_market_fixture.py` regenerates it byte-for-byte |
| `local-qwen-tier2-observation.json` | **Runtime observation**: one real local Qwen Tier 2 purchase | `record_local_qwen_observation.py` | `test_inference_c3_local_qwen_observation.py`, through Core alone |
| `nebius-qwen-tier1-observation.json` | **Runtime observation**: the real Nebius Tier 1 delivery, its sanitized usage, and the seven Tier 1 attacks run on it in memory | `record_qwen_market_live.py` | `test_inference_c3_live_observations.py`, through Core alone |
| `qwen-market-live-observation.json` | **Runtime observation**: both live market scenarios; scenario A's delivery is the same single Nebius call as the Tier 1 evidence | `record_qwen_market_live.py` | `test_inference_c3_live_observations.py`, including each candidate's dry-run decision |
| `nebius-model-discovery.json`, `nebius-model-Qwen_Qwen3-30B-A3B-Instruct-2507.json` | **Runtime observation**: sanitized live catalog snapshots (model IDs and Nebius-declared metadata) | `discover_nebius_models.py`, `describe_nebius_model.py` | Recorded only |

Runtime observations are not deterministically regenerable. None of them holds a credential, a request header, response text, a private key, model weights or a machine path. Where a check needs the delivered response, it uses the recorded digest, not an independent one.

## Not in this contract

- Quoting: there is no 402 step, and `gateway.quote` is not implemented. Market quotes are configured demo values.
- `provider_a.py` and `provider_b_sidecar.py` remain Checkpoint 0 stubs. Provider A is implemented in `nebius.py`, and Provider B in `local_llama.py`, signing in process; there is no separate signing-sidecar process.
- Tier 3, or any measurement of loaded weights.
- ResearchBot.
- Settlement: no settlement, reservation release, disputes, XRPL payments, LoanSet or LoanPay.
- Persistence: none for chain heads or settled receipts.
- Interfaces: no rate limiting, HTTP or FastAPI API, or frontend.
- Credentials beyond `NEBIUS_API_KEY` in the process environment.
