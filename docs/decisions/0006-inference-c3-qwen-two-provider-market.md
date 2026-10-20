# 0006. Inference Checkpoint 3: Qwen two-provider market (Nebius Tier 1 + local Tier 2)

- Status: Accepted
- Date: 2026-10-19
- Owner: Inference
- Contract: `contracts/INFERENCE-GATEWAY-CONTRACT.md`

## Context

The live Nebius catalog (observed 2026-10-16) lists no Llama model. The CP2 local Llama 3.1 8B provider stays frozen and valid. Maura chose a different shared model family for the two-provider market and authorized one local download.

## Decisions

1. **Shared model family `qwen3-30b-a3b-instruct-2507`.**
   - Nebius serves it as `Qwen/Qwen3-30B-A3B-Instruct-2507`. The model ID and its catalog entry come from sanitized live catalog observations in `contracts/fixtures/inference_c3/`.
   - Locally it is `qwen3:30b-a3b-instruct-2507-q8_0`.
   - Both name the same release: Qwen3, 30B-A3B, Instruct, revision 2507.
   - The comparison is at model-family level only. No artifact, precision or quantization equality is claimed.
2. **Nebius is Tier 1 and its profile asserts nothing it does not substantiate.**
   - `model_identifier` is the exact Nebius ID.
   - Artifact, tokenizer and chat-template digests, runtime, precision and quantization are null (NOT ASSERTED).
   - Nebius signs no `credence.provider_attestation`; the gateway signs the Tier 1 receipt.
3. **Catalog metadata is not profile evidence.**
   - Nebius's catalog `quantization: fp8`, its pricing strings and its limits are recorded as Nebius-declared metadata.
   - None of it populates the ExecutionProfile.
4. **Credential boundary.**
   - The Nebius key enters only through `NEBIUS_API_KEY`, supplied by the operator in a temporary subshell.
   - Credence code never invokes a password manager and never stores, logs or returns the key.
   - Live runs also need `CREDENCE_NEBIUS_LIVE=1`.
   - The transport talks only to the official HTTPS host and refuses redirects, so the `Authorization` header cannot be forwarded elsewhere.
5. **Token counts are provider-reported.**
   - Nebius `usage.prompt_tokens` maps to input and `usage.completion_tokens` to output.
   - A non-zero `cached_tokens` or `reasoning_tokens` fails closed, because its relation to those totals is not established.
6. **Service location: an Inference interpretation, not a Core definition.**
   - Core leaves the general meaning of `ServiceClass.jurisdiction` undefined.
   - For the service classes Inference builds, jurisdiction is the country where the inference executes, and residency is the countries where execution takes place. Neither means incorporation, headquarters or governing law.
   - Nebius: `FI` and `{FI}`, from its declared service region `eu-north1`.
   - Local: `US` and `{US}`, from Maura's operator-supplied fact that the machine physically operates in the United States.
   - The general class's `{FI, US}` is a buyer allowlist.
   - This is not a Credence-wide legal definition.
7. **Routing only among Core-qualified quotes.**
   - Every candidate goes through Core's real `policy_engine.evaluate` as a dry run.
   - Routing picks the lowest maximum cost among accepted candidates, breaking ties by provider ID, then quote ID.
   - A Core rejection is never chosen, and the chosen purchase is decided again by `ReservationLedger.reserve`.
8. **Demo quotes.**
   - Nebius's catalog pricing strings have no established unit or currency, so they are not converted.
   - Market quotes are configured demo values in micro-tRLUSD (tRLUSD is a test token), labelled as such, and never described as Nebius's billed price.
9. **Q8_0 binding from artifact metadata.**
   - The local binder asserts quantization `Q8_0` only for GGUF `general.file_type` 7 whose every tensor is Q8_0 or F32, with at least one Q8_0.
   - Precision is not asserted for Q8_0, because 8-bit blocks with per-block scales have no single precision label.
   - The F16 rule used by the frozen Llama provider is unchanged.
10. **Chat-template binding.**
    - A separate chat-template digest is bound only when the template the runtime reports it will use is byte-identical to the manifest's template file.
    - Otherwise it is NOT ASSERTED, and the runtime-reported template's SHA-256 is recorded as an observation only.
    - Local Qwen: the runtime renders with the template embedded in its GGUF (`sha256:40c21f34…`), not the template file (`sha256:eade0a07…`), so `chat_template_digest` is null.
    - Local Llama: the runtime reports exactly its template file (`sha256:948af274…`), so the frozen CP2 profile is unchanged, byte for byte.
    - Correction to the CP2 report: the Llama GGUF does embed a chat template (4,614 characters). The CP2 observation of "no embedded template" came from runtime metadata that omits that key. The committed CP2 profile and decision 0005 are unaffected.
11. **Credential hardening.**
    - A key with leading or trailing whitespace, or any whitespace or control character, is refused before any network call (`credential_malformed`), never silently stripped.
12. **Operational errors.** Documented Nebius HTTP statuses map to typed provider failures:
    - authentication, payment, model unavailable, rate limit and service unavailable → unavailable;
    - server error, request rejected, timeout and malformed response → execution error.

    Programming errors propagate.
13. **One Nebius inference call for the live evidence.**
    - Market scenario A's real Nebius delivery is also the Tier 1 gateway evidence, and the Tier 1 attacks reuse it in memory.
    - Scenario B makes no Nebius call, because Core rejects Nebius before any provider is called.
    - The live call's request body is byte-identical to the operator's successful exact-body probe.

## Local Qwen artifact (as bound and observed)

- **Tag:** `qwen3:30b-a3b-instruct-2507-q8_0`. The manifest SHA-256 is `528dfe43…`, equal to the registry manifest recorded before download.
- **GGUF:** `sha256:da17ea1e24268d63d8c5f876acc6a428a389f45e3d0d016ebdaaf3e1aed81c32`, 32,483,931,680 bytes.
- **Metadata:**
  - `general.name` Qwen3 30B A3B Instruct 2507, `finetune` Instruct, `version` 2507;
  - `qwen3moe`, 30,532,122,624 parameters, context 262,144;
  - `general.file_type` 7, tensors 338 Q8_0 + 241 F32.
- **Profile:** quantization `Q8_0`; precision, tokenizer, adapter and chat template NOT ASSERTED; runtime `ollama 0.34.2`; context 8192; temperature 0 with fixed seed 42.
- **Measured on the M4 Pro (observations, not guarantees):**
  - cold load 8.7–11.9 s;
  - warm generation about 50–52 tok/s;
  - Ollama reported `100% GPU` and 33 GB.

## Earlier HTTP 401 failures

- Three earlier runs of the two-call recorder failed their first Nebius request with HTTP 401.
- A controlled probe sending the exact recorder request body did not reproduce them: HTTP 200, 27 prompt and 2 completion tokens, no cached or reasoning tokens, 687 ms.
- They are recorded as transient or invocation-specific failures. Their cause was not established and was not investigated further.
- The probe produced no Credence evidence and is not used as such.

## Live results (runtime observation, 2026-10-19)

The live market run made one Nebius inference call; the evidence is in `contracts/fixtures/inference_c3/`.

- **Nebius Tier 1 delivery.**
  - HTTP 200 from `POST /v1/chat/completions` for `Qwen/Qwen3-30B-A3B-Instruct-2507`, finish reason `stop`, latency 789 ms.
  - `usage`: `prompt_tokens` 27, `completion_tokens` 2, `total_tokens` 29. `prompt_tokens_details` and `completion_tokens_details` were null, so the mapping is unambiguous.
  - The receipt is Tier 1 with no attestation, priced at 2 micro-tRLUSD (demo quote). The gateway signature is valid, the provider signature fact is `not_present`, and the Core verifier ACCEPTs.
- **Tier 1 attacks, run on that evidence in memory:**
  - an inserted attestation is refused at the gateway boundary (`PROVIDER_MALFORMED_RESPONSE`) and is inconsistent input to the verifier (`VerifierInputError`);
  - Tier 1 presented as Tier 2 → `RECEIPT_SIG_INVALID`;
  - swapped response bytes → `RESPONSE_DIGEST_MISMATCH`;
  - wrong profile digest → `BINDING_MISMATCH`;
  - counts above the caps → `TOKEN_COUNT_EXCEEDS_QUOTE`;
  - altered billed amount → `BILLED_AMOUNT_MISMATCH`;
  - invalid gateway signature → `RECEIPT_SIG_INVALID`.
- **Scenario A (general).** Both providers qualify in Core, with maximum costs of 85 for Nebius and 430 for local. Routing chooses Nebius, and its delivery is the Tier 1 evidence above.
- **Scenario B (controlled: quantization `Q8_0`, Tier 2 floor).**
  - Nebius is still cheaper (85 against 430) and Core rejects it with `PROFILE_NOT_ALLOWED`: its quantization is NOT ASSERTED, so the structural rule fires before the tier rule.
  - Routing chooses local Qwen, which delivers at Tier 2. Both its provider signature and the gateway signature are valid, and the Core verifier ACCEPTs.
- **Unchanged between scenarios:** only the procurement class and the facility differ. The quotes and provider evidence are the same.

## Consequences

- The controlled scenario's exclusion of Nebius comes from Core's own rules, because its profile leaves quantization NOT ASSERTED or its tier is too low. It does not depend on catalog metadata.
- Changing the meaning of jurisdiction for all of Credence would need a Core decision. This record does not make one.
