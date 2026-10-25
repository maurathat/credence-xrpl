# Checkpoint X1s — QuickNode transport hardening

Amended by X1u (`spike/xrpl/X1U-REMEDIATION.md`): the nesting pre-check is now a linear byte
scanner, and connection-close framing is fixed.

## Outcome

X1s corrects Salvador's four X1r findings (Q1–Q4) against X1q `baadf60`. It is offline:

- no ledger writes, submission, signing or token transfer;
- no real QuickNode call. No endpoint was configured, so the §7 real-endpoint preflight is
  **PENDING**.

Unchanged:

- Core, Inference, the XRPL journal, the local rippled transport and payment signing
  (byte-identical to the base, `git diff --quiet`);
- the trust policy, network isolation and read-only surface;
- the no-finalization reconciliation binding.

Contract: `contracts/QUICKNODE-ADAPTER-CONTRACT.md` (section "Response limits, deadlines and
credentials"). Decision: `docs/decisions/0013-xrpl-x1s-quicknode-hardening.md`.

## Before and after

`.context/x1s/exploits.py` (local, not committed) runs the same inputs against an archive of
`baadf60` and against the X1s tree, and asserts which tree it loaded. The committed regressions
are in `tests/unit/test_xrpl_x1s_hardening.py`.

| Finding | Input | `baadf60` | X1s |
| --- | --- | --- | --- |
| Q1 | 5,000-digit `DeliverMax` | Uncaught `ValueError` (int digit limit) | `REFUSED` / `MALFORMED_AMOUNT` |
| Q1 | Delivered value `1E+999999999` | Uncaught `decimal.Overflow` | `REFUSED` / `MALFORMED_AMOUNT` |
| Q1 | 200,000-level nested JSON (`observe_payment`, `validated_ledger_identity`) | Uncaught `RecursionError` | `REFUSED` / `RESPONSE_TOO_DEEP`; typed `QuickNodeRefused` |
| Q1 | Malformed HTTP status line (real loopback socket) | Uncaught `http.client.BadStatusLine` | `QuickNodeRefused` / `MALFORMED_HTTP_RESPONSE`, not retried |
| Q1 | Restart reconciliation with one hostile entry | Uncaught `RecursionError` aborted `reconcile_after_restart` | Hostile entry `UNKNOWN` (`QUICKNODE_RESPONSE_TOO_DEEP`); the other entries still reconcile; 0 outbox events |
| Q2 | Body dripping 1 byte / 50 ms, 1 s timeout | Still running after 6.0 s | `TIMEOUT` after 1.00 s |
| Q2 | Same, 3 attempts, 0.5 s timeout | Still running after 6.0 s | `TIMEOUT` after 1.66 s (3 × 0.5 s plus 0.15 s backoff) |
| Q3 | Frame locals and one level of their attributes on the escaping traceback (malformed-status case) | Token in 6 frames: `_once:request`, `open:fullurl`, `open:req`, `_open:req`, `https_open:req`, `do_open:req` (urllib `Request` objects) | None; the chain is the single `QuickNodeRefused` |
| Q4 | `run_probe` with the extreme-exponent transaction / nested JSON | Uncaught `decimal.Overflow` / `RecursionError` | `FAIL` with `MALFORMED_AMOUNT` / `RESPONSE_TOO_DEEP`; passing checks kept |

A note on Q3: rendering the `baadf60` traceback with `capture_locals=True` alone did not show the
token, because urllib's `Request` repr omits the URL. The exposure is in the objects' attributes
(`_full_url`), which a crash reporter that serializes object state would capture. An earlier run of
the local script that reported the token in rendered locals on both trees was an artifact (it
caught the exception at module level, where the script's own `TOKEN` global was a local). The
corrected script and the unit test both start the walk below the catching frame.

## Fixes

- **Q1 (`quicknode.py`)**
  - `parse_json_bounded`: an 8 MiB size cap; a nesting cap of 32, checked on a C-speed bracket
    skeleton before `json.loads`; integer, float and constant hooks.
  - XRPL-range bounds on drops, issued values, string integers, blobs, `complete_ledgers` and error
    tokens, all checked before any arithmetic or normalization.
  - `_typed_errors` on every public read, and a final catch in `observe_payment`.
  - Failures are raised outside handlers, so they carry no context.
- **Q2 (`QuickNodeJsonRpcClient`)**
  - `http.client` replaces urllib.
  - `RequestWatchdog` shuts the socket down at the absolute deadline.
  - `resolve_with_deadline` bounds DNS for the caller.
  - The body is read with `read1`, and the remaining time is applied before each read.
  - `total_budget_seconds` bounds the retry sequence, and the client never sleeps past it.
  - Content-Length and streamed-size limits are preserved, and a short body is
    `CONNECTION_CLOSED`.
- **Q3**
  - The target exists only in `_exchange`, which returns failures instead of raising them.
  - `call` raises the typed error outside any handler.
  - `http.client` debug level is 0, and nothing logs.
- **Q4 (`quicknode_probe.py`)**
  - Each check is contained, with typed codes.
  - Account reads are split into three checks so partial results survive.
  - The report has an `outcome` field.
  - Documented exit codes: 0 pass, 1 failed, 2 not configured, 3 incomplete.
  - `PROBE_INTERNAL_ERROR` is reported as a structured result instead of a traceback.

## Tests and gates

`tests/unit/test_xrpl_x1s_hardening.py` contains 44 tests:

| Group | Tests |
| --- | --- |
| Q1 | 21 |
| Q2 | 9 |
| Q3 | 6 |
| Q4 | 7 |
| Regression | 1 |

The 91 X1q tests still pass. The 12 that used the urllib test seam now run over real loopback
sockets with the same assertions; one probe test now expects the split account checks.

| Gate | Result |
| --- | --- |
| `make lint` (ruff, format, large files, decision numbers) | pass |
| `make typecheck` (strict mypy, 54 files) | pass |
| `tests/unit` | 1212 passed (1168 + 44) |
| `tests/conformance` (includes deterministic fixture regeneration) | 141 passed |
| Offline integration | 64 passed, 28 skipped |
| X0t/X1a/X1c regressions | 240 passed |
| X1q | 91 passed |
| X1s | 44 passed (stable across 3 repeated runs) |
| gitleaks | see the commit report |
| Protected paths vs `baadf60` | byte-identical |

## Credential-handling limitations

- The endpoint object holds the URL in a `__slots__` attribute.
  - It is redacted in its repr, absent from `vars()`, and refuses copying and pickling.
  - A crash reporter configured to walk private attributes several levels deep could still read
    `endpoint._url`, so crash-reporting configuration must treat it as a secret.
- During a request, `http.client` holds the target transiently in its own frames and send buffer.
  None of it outlives `_exchange`, because failures are returned, never raised. A debugger
  attached during the request could still see it.
- The fake token in the tests is derived at runtime and is not a credential. No real endpoint URL
  was printed or committed.

## Real-endpoint preflight (§7)

Not performed: no endpoint is configured in this environment (`CREDENCE_QUICKNODE_XRPL_*` unset).
When one is configured privately with a rotated token, run
`python -m credence.xrpl_adapter.quicknode_probe --account … --tx … --out .context/x1s-probe.json`
and review the report before committing any shape fixture.

## Remaining request 0010 dependencies

Unchanged by X1s:

- inspector and journal support for networks that forbid `NetworkID`;
- whether hosted evidence may ever finalize a journal entry, and under what evidence class;
- whether a hosted source may anchor submissions or prove non-inclusion.
