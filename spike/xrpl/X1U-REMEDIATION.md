# Checkpoint X1u — QuickNode transport remediation (Salvador X1t: B1, B2)

## Outcome

X1u corrects Salvador's two blocking X1t findings against X1s `c1b5e3e` and changes nothing else.

- Offline: no signing, submission, faucet call, ledger write or real QuickNode call. The
  real-endpoint preflight is still PENDING.
- Unchanged:
  - Core, Inference, the journal, payment signing, settlement and the local rippled transport
    (byte-identical to `c1b5e3e` and to base `99ca646`);
  - QuickNode's read-only boundary;
  - TLS verification, redirect refusal, proxy isolation, retry policy and absolute deadlines.

Contract: `contracts/QUICKNODE-ADAPTER-CONTRACT.md`. Local, uncommitted evidence scripts are in
`.context/x1u/`:

- `repro_b1.py`
- `trace_b1.py`
- `probe_codes.py`
- `time_b2.py`
- `time_b2_large.py`

## B1 — connection-close framing

**Cause.** For a response that will close (HTTP/1.1 `Connection: close`, or HTTP/1.0), `http.client`
closes the socket itself once the parser has the complete body. The X1s read loop then called
`sock.settimeout()` on that closed socket before checking for completion. The result was
`OSError: [Errno 9] Bad file descriptor` at `quicknode.py:571`, reported as `CONNECTION_FAILED`.

**Fix.**

- The loop runs while `not response.isclosed()`, so completion is decided by `http.client`'s
  framing state and no socket operation follows a complete response.
- A new framing check refuses, before any of the body is read:
  - `Content-Length` together with `Transfer-Encoding`;
  - differing duplicate lengths;
  - non-digit lengths;
  - any transfer coding other than exactly `chunked`.

  `http.client` would otherwise silently prefer one header or read to EOF.

| Case | `c1b5e3e` | X1u |
| --- | --- | --- |
| 1. HTTP/1.1, `Content-Length`, `Connection: close` | `CONNECTION_FAILED` | accepted, body exact |
| 2. HTTP/1.0, `Content-Length` | `CONNECTION_FAILED` | accepted |
| 3. HTTP/1.1, `Content-Length`, keep-alive | accepted | accepted |
| 4. HTTP/1.1 chunked + close | accepted | accepted |
| 5. HTTP/1.0 delimited by close | accepted | accepted |
| 6. Body shorter than `Content-Length` | `CONNECTION_CLOSED` | `CONNECTION_CLOSED` (retryable), also for HTTP/1.0, a short chunk and a missing terminating chunk |

**Changed behavior for malformed framing.**

- Identical duplicate `Content-Length` headers were refused as `RESPONSE_TOO_LARGE` at `c1b5e3e`.
  They are now accepted.
- A non-digit length was `RESPONSE_TOO_LARGE`. It is now `MALFORMED_HTTP_RESPONSE`.
- `RESPONSE_TOO_LARGE` remains the code for oversized lengths and bodies.

**Operator CLI over real loopback TLS.** The test generates a throwaway certificate for the fake
QuickNode host at run time and keeps certificate and hostname verification on; it only adds that
CA to the trust store. The server sends Clio-shaped answers with `Content-Length` and
`Connection: close`.

| Response | `c1b5e3e` | X1u |
| --- | --- | --- |
| HTTP/1.1 + `Connection: close` | exit 1, `FAIL`: identity, node sampling and transaction `CONNECTION_FAILED`; account checks `NOT_RUN` | exit 0, `PASS`: all six checks `PASS` (`transaction_lookup` `tesSUCCESS`) |
| HTTP/1.0 | the same failure | exit 0, `PASS` |

An untrusted certificate on the same path is still refused (`TLS_VERIFICATION_FAILED`).

## B2 — linear JSON pre-check

**Cause.** The X1s pre-check removed strings with `rb'"(?:[^"\\]|\\.)*"'`. On an unterminated run of
escaped quotes, every quote starts a match attempt that scans to the end, which is quadratic.

**Fix.** The regex removal and the repeated innermost-pair passes are replaced by one forward byte
loop with constant work per byte. It tracks:

- whether it is inside a string;
- whether the previous byte was an escaping backslash;
- a stack of expected closers, never deeper than 32.

It refuses excessive depth (`RESPONSE_TOO_DEEP`), unterminated strings and mismatched brackets
(`MALFORMED_RESPONSE`). It uses no regular expressions, slicing or recursion. `json.loads` with the
existing number hooks is still the strict parser; the scan is only a bounded pre-check.

**Measurements.** The input is Salvador's `b'"' + b'\\"' * n`. Each value is the median of 3 runs on
one machine, process and environment, measured, not extrapolated:

| Size | `c1b5e3e` | ratio | X1u | ratio |
| --- | --- | --- | --- | --- |
| 16 KB | 465.26 ms | | 0.14 ms | |
| 32 KB | 1,869.44 ms | ×4.02 | 0.28 ms | ×1.96 |
| 64 KB | 7,531.08 ms | ×4.03 | 0.56 ms | ×2.00 |
| 128 KB | 31,059.17 ms | ×4.12 | 1.14 ms | ×2.05 |

X1u at larger sizes, each a median of 3 runs. The `c1b5e3e` pre-check was not run at these sizes:

| Size | X1u | ratio |
| --- | --- | --- |
| 256 KB | 2.3 ms | |
| 512 KB | 4.6 ms | ×2.02 |
| 1 MiB | 9.1 ms | ×1.98 |
| 2 MiB | 18.3 ms | ×2.01 |
| 4 MiB | 37.2 ms | ×2.04 |
| 8,191 KB (just under the 8 MiB cap) | 73.7 ms | ×1.98 |

Other near-cap shapes (X1u, end-to-end `parse_json_bounded`, including `json.loads` where accepted):

- valid flat `[[],…,[]]`: 329.3 ms, accepted;
- one long ordinary string: 114.0 ms, accepted;
- backslash-pair run then a quote: 81.5 ms, accepted;
- a run of `[`: refused at depth 33 in under 0.1 ms;
- a cap + 1 body: refused before scanning.

**Concurrency.** During a cap-sized hostile scan (73.1 ms), an unrelated thread doing 1 ms sleeps
advanced 9 times. The scan is pure Python and yields at the interpreter's switch interval, so
other threads keep running. A unit test asserts that a concurrent thread progresses and that the
hostile scan ends in a typed refusal.

## Regression tests

`tests/unit/test_xrpl_x1u_remediation.py` contains 57 tests. B2 performance gates are kept separate
from the correctness assertions; their bounds sit far above the measured linear cost and far below
the quadratic one.

**B1:**

- the six reproduced framings;
- four short-body cases;
- six contradictory or malformed framings;
- identical duplicate lengths, plus the oversized and bad-status refusals;
- a guard that fails if `settimeout` is ever called on a closed socket;
- the operator probe over real loopback TLS (HTTP/1.1 close and HTTP/1.0);
- TLS still enforced.

**B2:**

- quotes and brackets in strings;
- escaped backslashes before a quote;
- an escaped quote at the depth limit;
- unterminated strings;
- excessive and mismatched nesting;
- invalid UTF-8;
- invalid and extreme numbers;
- maximum-size and oversized bodies;
- hostile JSON through all seven public read methods (`validated_ledger_identity`,
  `validated_ledger_observation`, `sample_node_identity`, `account_state`, `account_objects`,
  `account_lines`, `observe_payment`), each typed and under 1 s;
- the cap-sized hostile input refused promptly;
- thread progress.

**Run against `c1b5e3e`.** With the old module loaded, 15 of these tests fail, among them the
connection-close framings, the socket guard, both TLS probe runs, the new framing refusals and the
scanner checks. The large-input tests were not run on the old module: at its measured quadratic
rate they would take hours.

## Gates

| Gate | Result |
| --- | --- |
| X1q suite | 91 passed (file unchanged) |
| X1s suite (original X1r Q1–Q4 cases) | 44 passed (file unchanged) |
| X1u suite | 57 passed |
| X0t/X1a/X1c payment regressions | 240 passed |
| `tests/unit` | 1269 passed (1212 + 57) |
| `tests/conformance` (includes deterministic fixture regeneration) | 141 passed |
| Offline integration | 64 passed, 28 skipped (as before) |
| `make lint` (ruff, format, large files, decision numbers) and strict mypy (54 files) | pass |
| Protected paths vs `c1b5e3e` and vs `99ca646` | byte-identical |

The X1s suite confirms, unchanged:

- oversized `DeliverMax` is refused as `MALFORMED_AMOUNT`;
- extreme exponents are refused without overflow;
- deep nesting is refused without `RecursionError`;
- a malformed status line is a typed failure;
- drip responses stop at the deadline;
- retries stay within the budget;
- credentials appear in no error, chain, log or probe output;
- probe exit codes are 0/1/2/3;
- a hostile entry does not abort the other restart reconciliations, and creates no settlement
  event.

## Integration issues (recorded only)

See the contract, section "Integration issues". In short:

- San Francisco's `HashLookup.lookup` takes keyword-only `min_ledger`/`max_ledger`, which the
  QuickNode view does not accept.
- Decisions `0012` and `0013` collide with San Francisco's `0012`–`0015`.
- The XRPL adapter contract differs between the branches.
- San Francisco's non-inclusion, identity and reconciliation corrections are not inherited.
- Any integration must keep San Francisco's payment-authority and evidence boundaries.

Yeosu owns the resolution, under separate authorization.

## Residual limitations

- **DNS.** Resolution cannot be cancelled. Repeated timeouts against a hanging resolver can
  accumulate background DNS worker threads, one per timed-out attempt, with no cap.
- **Deadlines.** Absolute network deadlines are enforced by socket shutdown and thread scheduling,
  not as hard real-time guarantees.
- **Parsing.** Parsing runs after the response is received, under its own size, depth and number
  bounds. A cap-sized body costs up to about 0.33 s here.
- **Credentials.** The endpoint URL, including its token, is held in private configuration
  (`QuickNodeEndpoint._url`). Crash reporters that walk private attributes are a trust boundary.
- **Duplicate keys.** There is no duplicate-key rule: `json.loads` keeps the last value. The X1u
  brief described one as existing; none existed, and adding one was outside X1u's authorization.
- **Provider trust.** A hosted QuickNode provider is a trusted source, not independent XRPL
  consensus verification.
- **Real endpoint.** QuickNode Testnet validation is still missing.
- **Request 0010.** It remains open, and every ledger-write authorization boundary still applies.
