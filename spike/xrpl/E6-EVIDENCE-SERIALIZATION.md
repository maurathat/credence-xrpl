# E6 negative evidence serialization

Checkpoint 1E-NR repairs only the evidence representation used by the frozen
N1-N6 program. It does not adjudicate or rerun any negative case.

## Historical failure

The committed Checkpoint 1E-N N1 construction reproduces the historical
failure offline. The failing value was present at both of these paths in the
record passed to `write_json`:

- `state_assertions[4].actual`
- `state_assertions[4].expected`

The assertion name was `borrower SMT-NUL domains`. The values were Python
sets created in `negative_authorization_assertions`: a set comprehension over
the borrower signer rows and a one-element set literal containing the frozen
SMT-NUL prefix. Every N1-N6 construction calls that function and could produce
the same unsupported type.

The pre-repair `json_safe` converted each `StateAssertion` with `asdict()` and
returned the resulting dictionary without recursively normalizing its values.
The set therefore reached `json.dumps`, which raised
`TypeError: Object of type set is not JSON serializable`. The old preflight
checked whether assertions passed but did not construct and serialize complete
post-execution records, so it did not detect the type before N1 submission.

## Repair and deterministic representation

The unordered signing-domain collection is normalized at its construction
boundary by `normalize_unordered_strings`. It validates that every member is a
string, removes duplicates, sorts lexicographically, and returns a JSON array.
Thus the SMT-NUL assertion now records both `actual` and `expected` as the
deterministic array `[BORROWER_MULTISIGN_PREFIX_HEX]`. Insertion order cannot
change the serialized bytes. No transaction or cryptographic ordering is
changed.

The shared serializer now recursively converts the explicitly supported
`StateAssertion` dataclass and tuple values, accepts only JSON-native scalar,
array, and string-keyed object values, and fails with the exact value path for
sets, bytes, paths, enums, arbitrary dataclasses, custom objects, floats, or
non-string object keys. It does not use `default=str` or another stringifying
fallback.

`build_negative_case_record` applies that strict conversion when a record is
constructed. `write_json` and the new preflight both use `serialize_json`, so
the preflight exercises the production serialization path.

## Full pre-submit preflight

Before a future live N1-N6 transaction, the runner constructs complete record
shapes for all six cases and serializes them together with:

- protected-state assertion structures;
- public authorization/signing structures;
- result placeholders;
- phase and timing structures, including the N6 timing fields;
- experiment and checkpoint-result structures;
- manifest metadata.

The static pre-ledger preflight exercises the same six-case skeleton before
environment setup. After fresh fixtures and exact signing evidence are
prepared, the runner exercises the production serializer again over all six
records before N1 can be submitted. Any unsupported value fails closed.

Checkpoint 1E-N remains historical evidence that N1 submission and the
serialization STOP occurred. Its engine result and protected post-state were
not persisted, so N1 remains `BLOCKED_AFTER_SUBMISSION`. N2-N6 remain
`NOT_ATTEMPTED`, and the E6 authorization architecture remains
`NOT_VALIDATED`.
