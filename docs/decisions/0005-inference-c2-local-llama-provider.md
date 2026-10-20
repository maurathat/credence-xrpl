# 0005. Inference Checkpoint 2: local Llama 3.1 8B Instruct Tier 2 provider

- Status: Accepted
- Date: 2026-10-14
- Owner: Inference
- Contract: `contracts/INFERENCE-GATEWAY-CONTRACT.md` (Local Tier 2 provider)

## Context

The shared model family for both providers is Llama 3.1 8B Instruct. Checkpoint 2 adds the local provider (the build plan's Provider B) as the one real Tier 2 path. Nebius (Provider A) comes later.

## Decisions

1. **Runtime: the Ollama already installed on the host.** At the time of this decision it was version 0.34.2, serving on loopback; it runs models through a llama.cpp server. No new system software was installed. The provider talks to it only over loopback HTTP, using the standard library.
2. **Artifact: `llama3.1:8b-instruct-fp16` from the Ollama library**, chosen by Maura over `q8_0` and `q4_K_M`.
   - It is a GGUF conversion of Meta's model published by Ollama (`registry.ollama.ai`), not Meta's original files.
   - Upstream model family and local execution artifact are recorded separately, and only the local file is bound.
   - It is stored in Ollama's model store outside the repository.
3. **Artifact identity: one file, SHA-256 of its exact bytes.**
   - The tag's manifest names exactly one model layer, a single GGUF file.
   - The provider re-hashes it at bind time and checks it against the manifest's content address. No multi-file digest scheme is needed or defined.
   - Adapters and multiple model layers are refused.
4. **Chat template: the manifest's template file, by SHA-256.**
   - Ollama renders `/api/chat` with that file.
   - **Tokenizer: not asserted (null).** It is embedded in the GGUF file, so it has no separate bytes.
5. **Precision and quantization are asserted only when exact.**
   - `F16` and `none` are asserted only for a GGUF `general.file_type` of 1 whose every tensor the runtime reports is F16 or F32.
   - Anything else leaves both null.
   - The precision asserted is the file's own weight type, F16. It is not called BF16, and no claim is made about how Ollama converted it from the upstream weights.
6. **Token counts are provider-reported.**
   - The counts are the runtime's `prompt_eval_count` and `eval_count`. `prompt_eval_count` includes cached prompt tokens, as observed.
   - The runtime has no tokenizer endpoint, and the gateway runs no tokenizer, so the gateway does not count independently.
7. **Provider key: generated at runtime.**
   - It is never written. Only its public key ID and signatures are committed.
   - Unit fixtures keep using the deterministic TEST-key mechanism.
8. **Operational errors are narrowed.**
   - The gateway catches only `ProviderUnavailableError` and `ProviderExecutionError`. Any other exception propagates as a programming error.
   - The local provider maps transport and runtime failures to those two types.
9. **Evidence is split in two.**
   - Deterministic protocol fixtures (`inference_c1/`) regenerate byte-identically.
   - The real run is committed as runtime-observation evidence (`inference_c2/`), which is not regenerable. Instead, it is re-verified through Core.

## Consequences

- **The claim boundary is unchanged: Tier 2, never Tier 3.** Artifact binding shows which file the tag names, not what was resident in GPU memory. There is no TEE or hardware attestation.
- **The tag is re-checked by manifest, not by bytes.** The provider re-reads the manifest before each execution but does not re-hash the 16 GB file each time. A file changed in place under the same content address would go unnoticed until the next bind. Ollama's store is content-addressed, so that would require tampering outside Ollama.
- **The provider signs in-process.** Its key is held by the provider object in the same process as the gateway. A separate sidecar process for key isolation is not implemented.
- **Nothing ties the local file to Nebius's.** No claim relates the local file to whatever Nebius executes. The only shared assertion is the model family.
