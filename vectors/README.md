# vectors

Owner: Credence Core (Claude Code).

These are serializer conformance vectors for each Credence class. Each vector holds the input JSON, the expected canonical bytes and the expected digest. Across the set they cover NFC normalization, key ordering with non-ASCII keys, large integers and integer amounts.

## c1/

These are the Credence Core C1 vectors, produced by the Kessai serializer: JCS (RFC 8785) + NFC, through the pinned custody `canon.py` (see `docs/decisions/0002-credence-core-c1-domain-model.md`).

- **Regenerate:** `uv run python vectors/build_c1_vectors.py`. Every expected value comes from the real serializer; none is written by hand.
- **Check:** `make test-conformance` runs `tests/conformance/test_core_c1_vectors.py`. It recomputes every vector and checks each digest against `hashlib` directly.
- **`MANIFEST.json`:** a list of `[sha256, file name]` pairs that pins every vector file, so a vector that is removed, added or edited is detected.

Each file is one vector:

| Field | Meaning |
| --- | --- |
| `id`, `description` | What it covers |
| `entry` | The function under test: `objects.<Class>.from_canonical`, `kessai.canonicalize` or `kessai.canonicalize_json_bytes` |
| `input` or `input_json_text` | The input value, or the raw JSON text for the bytes entry point |
| `expected` | Either `canonical_text`, `canonical_hex` and `digest`, or `reject` with a `reason` (plus `field` for domain refusals) |

All values are synthetic and illustrative:
- Digests are SHA-256 of labels.
- Key IDs and signatures are derived from labels and verify nothing.
- The XRPL account is the classic address of a fixed 20-byte pattern with no key behind it.

## c3a/

These are the Credence Core C3A signature vectors (`contracts/SIGNATURES.md`, decision 0004).

- **Content.** Each vector holds the key registry, the signed object, the `credence.signed_object`, and the expected digest, message and verification result.
- **Keys.** They are deterministic TEST keys (NOT FOR PRODUCTION). Each seed is SHA-256 of a public label, derived at run time; no key material is written.
- **Regenerate:** `uv run python vectors/build_c3a_signature_vectors.py`.
- **Check:** `tests/conformance/test_core_c3a_signature_vectors.py`. It requires byte-identical regeneration, rebuilds every message by hand, and re-verifies every signature with PyNaCl directly.
