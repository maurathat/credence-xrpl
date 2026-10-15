# SIGNATURES

Owner: Credence Core (Claude Code). Status: checkpoint C3A. Only the owner edits this file. Other workstreams file a request in `contracts/requests/`.

This contract describes Credence object signatures as implemented in `credence/core/signatures.py`. The code is authoritative. `tests/unit/test_core_signatures_contract.py` checks the lists and tables here against the code. Vectors are in `vectors/c3a/`. The decision is `docs/decisions/0004-credence-core-c3a-signatures.md`.

## What is signed

A signature is Ed25519 (RFC 8032, via PyNaCl/libsodium) over a domain-separated statement of one canonical object's digest. It never covers any of the following: a Python repr, ordinary JSON, a mutable dictionary, prompt or response text, or an XRPL transaction.

`object_digest` is the object's `sha256:` digest through the pinned Kessai serializer, JCS (RFC 8785) + NFC (see `contracts/CORE-OBJECTS.md`). Equivalent admitted representations of an object have the same digest, and therefore the same message and signature. Changing any canonical member changes the digest, so an old signature no longer verifies.

## Message

```
message = lp(domain) || lp(object_type) || lp(schema_version) || lp(object_digest)
lp(x)   = uint32 big-endian byte length of x || x        (x is ASCII)
```

- `domain` is `credence/signature/v1`.
- `object_type` and `schema_version` are the signed object's own, and `object_digest` is its `sha256:<hex>` string.
- Because every field is length-prefixed, no two different field tuples produce the same bytes.

## Signature wrapper

A signature is a `credence.signed_object` `{object_digest, key_id, signature}`, kept outside the body it signs:
- `object_digest` must equal the digest of the object being verified;
- `key_id` is `ed25519:` followed by the 32-byte public key in hex, so `key_id` *is* the verifying key and is not secret;
- `signature` is the 64-byte signature in hex.

## Roles

A key has one role, and each role signs exactly one object type:

| Role | Signs | Subject |
| --- | --- | --- |
| `agent` | `credence.purchase_request` | The facility's borrower account |
| `gateway` | `credence.delivery_receipt` | The gateway's own name (not checked) |
| `provider` | `credence.provider_attestation` | The provider ID; must equal the receipt's `provider_id` |

## Key registry

`KeyRegistry` is a deterministic in-memory registry of recognized keys. It does no database, network or XRPL lookup.
- **Entries.** Each entry is `{key_id, role, subject, status}`.
- **Statuses.** `status` is one of `current` or `revoked`.
- **Refused registrations.** Registration refuses a duplicate `key_id`, an empty subject, and any `key_id` that is not a valid, non-small-order Ed25519 point.
- **Revocation.** `revoke(key_id)` sets `revoked`.

## Verification

`verify_signed(object, signed, registry, role, subject)` returns `signature_valid`, `key_current` and the first failure, checked in this order:

1. `WRONG_OBJECT_TYPE`: the expected role does not sign this object type (or no signature wrapper is given).
2. `DIGEST_MISMATCH`: `signed.object_digest` is not the object's digest.
3. `UNKNOWN_KEY`: `key_id` is not registered.
4. `WRONG_ROLE`: the registered key has another role.
5. `WRONG_SUBJECT`: the registered subject is not the expected subject.
6. `INVALID_PUBLIC_KEY`: `key_id` is not a valid Ed25519 point.
7. `BAD_SIGNATURE`: Ed25519 verification of the message fails.
8. `REVOKED_KEY`: the key is revoked. The signature is still valid, but the key is not current.

Every failure before `REVOKED_KEY` gives `signature_valid = false` and `key_current = false`.

## Facts for the C2 engines

These turn verification into the facts the policy engine and the verifier consume (`contracts/POLICY-VERIFIER.md`). They answer only "was this object signed by a recognized key, and is the key current?". Whether a purchase or delivery is allowed stays with C2.

| Fact | Source |
| --- | --- |
| `request_signature_valid` | `signature_valid` for the request under an `agent` key whose subject is the facility's borrower account; false if no signature is supplied |
| `working_key_current` | `key_current` for that same check |
| `gateway_signature_valid` | `signature_valid` and `key_current` for the receipt under a `gateway` key; false if no signature is supplied |
| `provider_signature` | `not_present` if no provider signature is supplied; `invalid` if one is supplied without an attestation, or fails, or its key is not current; otherwise `valid` (a `provider` key whose subject is the receipt's `provider_id`) |

So a request signed by a revoked working key reaches the policy engine as `request_signature_valid = true, working_key_current = false`, and is rejected as `KEY_REVOKED`. A request signed by an unknown key, or altered after signing, is rejected as `SIG_INVALID`.

## Tier behaviour

- **Tier 1 (witnessed).** Only the gateway signature is expected. The provider fact is `not_present`.
- **Tier 2 (bound and signed).** The provider attestation must exist and its provider signature must be `valid`. The C2 verifier then checks that the attestation binds to the receipt. A Tier 2 receipt without a valid provider signature is `RECEIPT_SIG_INVALID`.
- **Tier 3.** Not implemented.

## Not XRPL signing

Credence object signatures are not XRPL transaction signatures:
- no XRPL signing hash, transaction blob or key derivation is used;
- there is no Ledger hardware signing;
- `authorized_transaction_digest` in decision records remains null.

The working-key status here is a local registry fact. It is not read from the borrower account's signer list.
