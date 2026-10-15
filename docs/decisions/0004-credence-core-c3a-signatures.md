# 0004. Credence Core C3A: signature and attestation layer

- Status: Accepted
- Date: 2026-10-14
- Scope: Credence Core checkpoint C3A
- Source: `docs/spec/credence-build-plan.md` (Serialization and receipts: "Ed25519 over the canonical bytes (PyNaCl, as in Sever). Each signed object carries the signer's public key identifier and the digest it signed."), Maura's C3A brief. Contract: `contracts/SIGNATURES.md`.

## Decisions

1. **Ed25519 via PyNaCl.** PyNaCl 1.6.2 (libsodium) is already a locked dependency, so no dependency was added and no cryptography is implemented here. Ed25519 signatures are deterministic, which makes the test vectors reproducible.
2. **Exact signed message.**
   - `lp("credence/signature/v1") || lp(object_type) || lp(schema_version) || lp(object_digest)`, where `lp(x)` is a 4-byte big-endian length followed by the ASCII bytes.
   - The digest is the object's `sha256:` digest through the pinned Kessai serializer (JCS (RFC 8785) + NFC). Signatures therefore depend only on the canonical object, never on Python field order or ordinary JSON.
3. **Domain separation.**
   - The domain string and the object type are in the message, and the object type is also inside the canonical bytes behind the digest.
   - Each key has one role, and each role signs one object type: agent signs `credence.purchase_request`, gateway signs `credence.delivery_receipt`, provider signs `credence.provider_attestation`.
   - So a signature for one object type, or from one role, cannot be replayed as another. Tests show this even when the attacker reuses the same digest string or the same key under a different role.
4. **Key identity and registry.**
   - `key_id` is `ed25519:` plus the public key, as frozen in C1, so it is the verifying key and never secret.
   - `KeyRegistry` is a deterministic in-memory map from `key_id` to `{role, subject, status}`. It does no database, network or XRPL lookup.
   - It refuses duplicates and invalid or small-order points.
   - Subject binding: agent keys to the facility's borrower account, provider keys to the receipt's `provider_id`.
5. **Revocation semantics.** A revoked key's signature is still cryptographically valid, but the key is not current. A request signed by a revoked working key therefore reaches the policy engine as `(request_signature_valid=true, working_key_current=false)`, giving `KEY_REVOKED`; an unknown or wrong key gives `SIG_INVALID`. For gateway and provider signatures, a revoked key is simply not accepted.
6. **Crypto is separate from C2.** The signature layer only produces the facts C2 already consumes. The policy engine and the verifier are unchanged and still make every decision: a correctly signed but over-billed receipt is still rejected by the verifier.
7. **Not XRPL signing.** Credence object signatures use no XRPL signing hash, transaction blob or key derivation, and no Ledger hardware. `authorized_transaction_digest` stays null. The local working-key status stands in for the borrower account's signer list until the XRPL workstream supplies it.
8. **Test keys.** Vectors and tests use deterministic TEST keys whose seeds are SHA-256 of public labels prefixed "CREDENCE TEST KEY - NOT FOR PRODUCTION". Only public key IDs, messages and signatures are committed. The secrets scan passes with its rules unchanged.

## Consequences

- **Working-key status.** Production will need the working-key status to come from the borrower account's XRPL signer list (the XRPL workstream). The registry interface already isolates that source.
- **Key storage.** Private keys for the agent, gateway and providers are not handled here. Their storage (1Password, the provider sidecar) belongs to the workstreams that own those components.
