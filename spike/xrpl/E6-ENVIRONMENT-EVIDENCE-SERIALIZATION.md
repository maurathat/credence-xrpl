# E6 environment evidence serialization

Checkpoint 1E-NR2 repairs only the pre-transaction environment-evidence
boundary. It does not execute or adjudicate N1-N6.

## Exact failure and parsing path

Checkpoint 1E-N-LIVE-02 persisted the exact exception but stopped before its
`server_info` body could be written:

```text
TypeError: unsupported JSON evidence type float at
$.server_info.validated_ledger.base_fee_xrp
```

The live-02 wire body is therefore not preserved. Raw committed evidence from
the same pinned rippled 3.4.0 build and network preserves the JSON token as
`"base_fee_xrp":1e-05`. `httpx.Response.json()` delegates JSON-number parsing
to Python's JSON decoder, which converts that token to `float(1e-05)`.
`sanitize_response` recursively filters prohibited signing fields but returns
scalar values unchanged. The float therefore reached the strict evidence
serializer.

An audit of every committed `environment-start.json` found this as the only
float path. No sets, tuples, bytes, paths, enums, dataclasses, custom objects,
or non-string dictionary keys occur in the fixture-equivalent environment
payload.

## Field classification and normalization

`base_fee_xrp` is rippled server telemetry. It is not Credence canonical
money, tRLUSD accounting, a signed Credence object, or a LoanSet economic
term. Credence money remains integer micro-units.

Only the exact path
`server_info.validated_ledger.base_fee_xrp` is normalized. Its Python float is
represented as:

```json
{
  "json_number": "1e-05",
  "normalization": "Python shortest round-trip decimal representation",
  "source_json_type": "number"
}
```

Python's `repr(float)` supplies the deterministic shortest decimal lexeme that
round-trips to the parsed binary float. The normalizer verifies that parsing
the recorded lexeme reproduces the original value exactly. It does not round,
convert the value to micro-tRLUSD, or make the generic serializer accept
floats. A float at any other path fails closed.

## Complete pre-transaction preflight

Before a future runner may start rippled, the production serializer now
processes fixture-equivalent shapes for:

- A: provenance, including the selected evidence destination;
- B: required-amendment and validation evidence;
- C: the feature response;
- D: the Amendments ledger-entry response;
- E: normalized server/environment evidence;
- F: ledger-header and close-time evidence;
- G: public actor and setup evidence skeletons;
- H: protected-state structures;
- I: all N1-N6 evidence skeletons;
- J: manifest metadata.

This preflight detects both historical packaging defects: an injected
signing-domain set fails in section I, and an unhandled environment float
fails in section E. The generic serializer retains its strict no-float and
no-stringification behavior.

## Prospective provenance label

The historical live-02 evidence remains unchanged, including its stale
runner-generated `evidence/checkpoint-1e-n` metadata label. Prospectively, the
runner derives the label from the actual selected destination. A future
`checkpoint-1e-n-live-03` run will therefore record
`evidence/checkpoint-1e-n-live-03` in both preflight and runner provenance.
