# QUICKNODE-ADAPTER-CONTRACT

Owner: XRPL Integration. For checkpoints X1q, X1s and X1u, Maura delegated this file and
`credence/xrpl_adapter/quicknode.py` / `quicknode_probe.py` to Da Nang (Claude Code).

Status: X1q, hardened by X1s and remediated by X1u; read-only, offline-tested. Real-endpoint
verification is
**PENDING**, because no QuickNode endpoint was configured.

- Decisions: `docs/decisions/0012-xrpl-x1q-quicknode-read-only.md`,
  `docs/decisions/0013-xrpl-x1s-quicknode-hardening.md`.
- Reports: `spike/xrpl/X1Q-QUICKNODE.md`, `spike/xrpl/X1S-HARDENING.md`,
  `spike/xrpl/X1U-REMEDIATION.md`.

## Scope

QuickNode is an optional connectivity provider for the public XRPL **Testnet** (network ID 1).
Credence uses QuickNode's public XRPL JSON-RPC API; nothing here implies any arrangement with
QuickNode beyond that.

The X1q adapter:

- reads only;
- implements no submission;
- does not replace the pinned local rippled transport (`RippledTransport`) on the local standalone
  ledger (network 21337);
- does not change that transport's rules.

Credence settlement still runs on a local XRPL ledger with the lending amendments enabled.

## Interfaces

| Interface | Contract |
| --- | --- |
| `QuickNodeConfig.from_environment()` | Returns `None` unless `CREDENCE_QUICKNODE_XRPL_URL` is set. If the URL is set, `CREDENCE_QUICKNODE_XRPL_NETWORK=testnet` is also required. `mainnet`/`0` is refused with `MAINNET_NOT_AUTHORIZED`. `21337`/`local`/`standalone` is refused with `LOCAL_NETWORK_NOT_HOSTED`. Any other value is refused with `UNSUPPORTED_NETWORK`. `CREDENCE_QUICKNODE_XRPL_LABEL` is optional. |
| `QuickNodeEndpoint` | Requires one HTTPS URL on `*.quiknode.pro`: no user info, port 443, no query or fragment. A host naming `mainnet` or `devnet` is refused. The endpoint is immutable and cannot be pickled. Its repr shows only a 16-hex fingerprint. |
| `QuickNodeJsonRpcClient` | POSTs JSON-RPC to the pinned URL only, over `http.client`. TLS is verified (default trust store, hostname check, TLS 1.2 or later). Proxies are never used and redirects are never followed. Each attempt has one absolute deadline (`timeout_seconds`, default 10 s, max 30 s) covering DNS, connect, the TLS handshake, the request, the headers and the whole body. A retry sequence is bounded by `total_budget_seconds` (default 30 s, max 120 s). Timeouts, HTTP 429 (honoring `Retry-After`) and 5xx get bounded retries: default 3 attempts, exponential backoff, capped at 5 s, never sleeping past the budget. 3xx, 401/403, TLS failures, malformed HTTP and other statuses are not retried. Errors carry no URL and have no chained cause or context (see "Response limits, deadlines and credentials"). |
| `QuickNodeXRPLTransport.validated_ledger_identity()` | `server_info` supplies the network ID, Clio version, validated ledger and `complete_ledgers`; `ledger` then confirms the index and hash. |
| `QuickNodeXRPLTransport.sample_node_identity()` | One forwarded `server_state`: `pubkey_node`, build, network ID and the `forwarded` flag. Samples are recorded, never pinned. |
| `account_state` / `account_objects` / `account_lines` | `account_info` with SignerList, `account_objects` and `account_lines`, all pinned to the identity's `ledger_hash` and paged up to a bound. Account state is the X1a `AccountSnapshot` type. |
| `observe_payment(hash)` | Returns a `HostedPaymentObservation` with status `VALIDATED`, `PROVISIONAL`, `NOT_FOUND`, `UNAVAILABLE` (retryable) or `REFUSED`. Ledger and transport faults never raise. |
| `validated_ledger_observation(account)` | History coverage and sender state in the X0t `ValidatedLedgerObservation` type, **for reporting only**. |
| `QuickNodeReconciliationView` | The only binding to the X0t `HashLookup` and `ValidatedLedgerObserver` seams (see "Payment reconciliation"). |
| `assess_hosted_payment(observation, expectation)` | Advisory classification in the X0t verifier's terms. `settlement_authority` is always false. |
| `python -m credence.xrpl_adapter.quicknode_probe` | Operator-run, read-only compatibility probe that writes a sanitized report, including partial results and typed `FAIL` codes. It never ends in a traceback. Exit codes: `0` every check passed; `1` a check failed, a contradiction latched or the probe itself failed (`PROBE_INTERNAL_ERROR`); `2` not configured ("PENDING") or configuration refused; `3` incomplete (a check `NOT_RUN`, nothing failed). |

The transport has no `submit`, `lookup`, `validated_ledger` or `ledger_context` method. It
therefore cannot be used as:

- the X0t `SubmissionTransport`, `HashLookup` or `ValidatedLedgerObserver`;
- the policy signer's `TrustedLedgerSource`.

`PaymentService` refuses it as a transport.

WebSocket support is a documented extension and is not implemented. It would need the same
endpoint pinning, TLS, network and ledger-consistency rules as the HTTPS client. It would also
need a subscription model in which a dropped or reordered `ledgerClosed` stream is never treated
as evidence.

## Hosted-provider trust policy

QuickNode serves XRPL through Clio. Its `server_info` is Clio's own answer and includes
`clio_version` and `network_id`. Its `server_state` is forwarded to a rippled node
(`"forwarded": true`). Clio's `complete_ledgers` can differ from the forwarded node's.

1. **Endpoint and credential.**
   - The endpoint is one explicitly configured HTTPS URL, read from the environment.
   - Its path token is a credential. It never appears in Git, logs, exceptions, evidence, test
     snapshots, probe reports or browser code.
   - Evidence and reports identify the source as `quicknode:<label>:testnet` plus the endpoint
     fingerprint.
2. **TLS.** Certificates and hostnames are verified. There is no option to disable verification.
3. **Expected network.**
   - Every identity read requires `server_info.network_id == 1`.
   - A missing network ID is refused (`NETWORK_IDENTITY_MISSING`).
   - A different network ID, from Clio or from the forwarded `server_state`, latches the transport
     closed (`NETWORK_MISMATCH`, then `SOURCE_CONTRADICTED`) until it is rebuilt.
   - A Payment carrying a `NetworkID` field is refused (`TRANSACTION_NETWORK_MISMATCH`), because
     network 1 forbids that field.
4. **No node pin.**
   - `pubkey_node` belongs to whichever rippled node Clio forwarded to. It is sampled and recorded.
   - A change is not an error. No node-key or build pin exists in `QuickNodeConfig`.
   - Whether a given endpoint's key is stable in practice is what the probe measures. That
     question is open until a real endpoint is configured.
5. **Validated-ledger consistency.**
   - The validated index and hash come from `server_info` and are confirmed by `ledger`.
   - If one index is ever answered with two hashes, the transport latches closed
     (`LEDGER_HASH_CONTRADICTION`).
   - A validated ledger lower than one already seen is `STALE_VALIDATED_LEDGER`: retryable, never
     accepted.
   - A ledger older than 120 s is stale.
   - A close time more than 30 s in the future is refused.
6. **History coverage.**
   - The validated ledger must lie inside Clio's `complete_ledgers`.
   - A transaction's ledger must also lie inside that history, and at or below the validated
     ledger.
   - `NOT_FOUND` never proves non-inclusion.
7. **Repeated reads.** Observing the same transaction again with different facts (ledger, hash,
   result or delivered amount), or later failing to find it, latches the transport closed
   (`TRANSACTION_OBSERVATION_CONTRADICTION`).
8. **Evidence.**
   - The binary metadata is authoritative for what moved: `DeliveredAmount`, or the signed
     `Amount` for a successful non-partial Payment.
   - The JSON `delivered_amount`, `TransactionResult`, `Account`, `Destination` and
     `DeliverMax`/`Amount` must agree with the binary form. Otherwise the result is
     `EVIDENCE_INCONSISTENT`.
   - A partial payment without `DeliveredAmount`, or `delivered_amount: "unavailable"`, is
     `DELIVERED_AMOUNT_UNAVAILABLE`.
   - The envelope schema is `credence.xrpl.hosted-payment-observation.v1`, with
     `trust: "single-hosted-source"`.

A single QuickNode endpoint is a trusted source. It is not independent consensus verification.
The pinned local rippled rules (network, required build pin, optional node-key pin on every
lookup) are unchanged.

## Response limits, deadlines and credentials (X1s)

**Hostile responses.** Every public read raises only `QuickNodeError`, and `observe_payment`
never raises. Limits are checked before any expensive work:

| Limit | Value | Refusal |
| --- | --- | --- |
| Response body | 8 MiB (declared `Content-Length` checked first, then the streamed size) | `RESPONSE_TOO_LARGE` |
| JSON nesting | 32 levels, checked by a single linear byte scan (string and escape state tracked; no regular expressions, slicing or recursion) before the recursive parser runs. Unterminated strings and mismatched brackets are refused by the same scan. | `RESPONSE_TOO_DEEP` / `MALFORMED_RESPONSE` |
| JSON numbers | integers up to 24 digits; floats up to 32 characters and finite; no `NaN`/`Infinity` | `NUMBER_OUT_OF_RANGE` |
| XRP amounts | digit strings of at most 18 digits, at most 10^17 drops | `MALFORMED_AMOUNT` |
| Issued amounts and trust-line values | at most 48 characters, 16 significant digits, adjusted exponent -81 to 95 (XRPL's range), checked before normalization | `MALFORMED_AMOUNT` / `MALFORMED_RESPONSE` |
| Integers in strings | at most 20 digits | `MALFORMED_RESPONSE` |
| `tx_blob`, `meta_blob` | 1 Mi hex characters | `MALFORMED_RESPONSE` |
| `complete_ledgers` | 4096 characters | `MALFORMED_COMPLETE_LEDGERS` |
| Node error tokens | `[A-Za-z][A-Za-z0-9_]{0,47}`, otherwise `malformedError` | `RPC_<token>` |
| Malformed HTTP (bad status line, oversized or too many headers) | `http.client` limits | `MALFORMED_HTTP_RESPONSE` |
| Contradictory or malformed framing (X1u) | `Content-Length` together with `Transfer-Encoding`; differing duplicate lengths; a non-digit length; any transfer coding other than exactly `chunked` | `MALFORMED_HTTP_RESPONSE` |
| Body shorter than its framing (`Content-Length` not reached, missing or short chunk) | | `CONNECTION_CLOSED` (retryable) |

Accepted framings (X1u):

- HTTP/1.1 and HTTP/1.0 bodies framed by `Content-Length`, whether the connection is kept alive or
  closed;
- chunked bodies;
- HTTP/1.0 bodies delimited by connection close. Truncation of these cannot be detected by
  definition; the strict JSON parser still refuses an incomplete document.

Body completion comes from `http.client`'s framing state (`HTTPResponse.isclosed()`). No socket
operation follows a complete response.

JSON duplicate keys: there is no duplicate-key rule. `json.loads` keeps the last value. The X1u
brief described one as existing; it does not, and adding one was outside X1u's authorization.

Anything else that hostile input still reaches becomes `RESPONSE_PROCESSING_FAILED`, with no cause
or context. A refused observation has no evidence and no settlement authority. Through
`QuickNodeReconciliationView` it leaves only its own journal entry `UNKNOWN`; other entries still
reconcile.

**Deadlines.**

- A timer thread shuts the request's socket down at the absolute deadline. This wakes `connect`,
  the TLS handshake and `recv`.
- The body is read with `read1`, and the remaining time is re-applied before every read.
- A peer that drips bytes, in the headers or in the body, gets `TIMEOUT` at the deadline, and the
  connection is closed.

Limitations:

- **DNS.** `getaddrinfo` cannot be cancelled. It runs in a daemon thread and the caller returns
  `TIMEOUT` at the deadline, but the lookup itself may continue in the background until the system
  resolver gives up. Repeated timeouts against a resolver that hangs can therefore accumulate
  background DNS worker threads, one per timed-out attempt, until those lookups return. There is
  no pool or cap.
- **Socket shutdown.** Waking a blocked call by shutting its socket down is the documented
  behavior on Linux and macOS, and it is what the tests exercise. Other platforms are not
  verified.
- **Scheduling.** Enforcement is as precise as thread scheduling: within tens of milliseconds in
  the tests, not a real-time guarantee.
- **Parsing.** JSON parsing and evidence checks run after the body is received and are not counted
  against the HTTP deadline. They are bounded by the limits above instead.

**Credentials.**

- The credential-bearing request target exists only inside `QuickNodeJsonRpcClient._exchange`.
  That method returns failures instead of raising them, so its frame, and any `http.client`
  exception that saw the target, never appears in a traceback, exception chain or frame-locals
  crash report.
- Errors are raised from `call`, whose locals hold only the method, parameters, body and redacted
  objects.
- The client never logs, and it sets `http.client`'s debug level to 0.

Limitation: the endpoint object must hold the URL to use it. It is kept in a `__slots__`
attribute (not reachable through `vars()`), and the endpoint refuses copying and pickling. Its
repr is redacted. Code that deliberately reads `endpoint._url` (for example, a crash reporter
configured to walk private attributes several levels deep) can still reach it, so the token must
still be treated as a secret in crash-reporting configuration.

## Integration issues (recorded in X1u, not resolved)

San Francisco's independently reviewed XRPL branch (`maurathat/credence-multi-sig-ledger`,
`bc82eb6`) has moved on since this branch left `99ca646`. These issues are recorded only. Yeosu
owns any later resolution, under separate authorization. X1u did not rebase, merge, renumber or
change any shared XRPL code.

1. **`HashLookup` signature.** San Francisco's protocol is
   `lookup(transaction_hash, *, min_ledger=None, max_ledger=None)`.
   `QuickNodeReconciliationView.lookup(transaction_hash)` accepts neither keyword, so San
   Francisco's `PaymentService` would fail with `TypeError` when it passes them.
2. **Decision numbers.** San Francisco has committed:
   - `0012-xrpl-x1e-real-node-preflight`;
   - `0013-xrpl-x1g-non-inclusion-proof`;
   - `0014-xrpl-x1i-payment-liveness`;
   - `0015-xrpl-x1k-reconciliation-ownership`.

   Da Nang's `0012` and `0013` collide with the first two. Request number `0010` does not collide.
3. **Adapter contract.** `contracts/XRPL-ADAPTER-CONTRACT.md` differs between the branches. San
   Francisco changed it by about 113 lines after `99ca646`; Da Nang added only the X1q pointer
   section.
4. **Corrections not inherited.** San Francisco's non-inclusion, identity and reconciliation
   corrections (`journal.py`, `rippled.py`, `service.py`, `types.py`) are not in this branch.
5. **Boundaries to preserve.** Any later integration must preserve San Francisco's accepted
   payment-authority and evidence boundaries. The hosted view must remain unable to finalize,
   anchor or settle.

## Payment reconciliation

The X0t journal finalizes a payment only on evidence from the pinned trusted node, re-verified on
every lookup. It proves expiry only from a pre-broadcast anchor on the same source. A hosted source
can supply neither, so `QuickNodeReconciliationView`:

- refuses construction unless the declared journal network equals the hosted network
  (`NETWORK_ISOLATION`; a 21337 journal can never bind it);
- answers every `lookup` with `LookupDisposition.UNAVAILABLE` and one of these details:
  - `QUICKNODE_FINALITY_UNSUPPORTED` (validated);
  - `QUICKNODE_PROVISIONAL_UNSUPPORTED`;
  - `QUICKNODE_NON_INCLUSION_UNSUPPORTED` (not found);
  - `QUICKNODE_<code>` (refused or unavailable);
- refuses `validated_ledger` with `LookupUnavailable("QUICKNODE_LEDGER_ANCHOR_UNSUPPORTED")`.

`PaymentService` therefore leaves the entry `UNKNOWN`. Nothing is finalized, no settlement outbox
event is created, nothing is resubmitted and Core is untouched. The entry later reconciles normally
by its original hash through an authorized source.

The hosted evidence schema is not the X0t schema, so `PaymentResultVerifier` rejects it as
`FINAL_EVIDENCE_INVALID`.

`assess_hosted_payment` keeps the X0t distinctions in an advisory result:

- validated versus provisional;
- intent contradictions as `UNRESOLVED`;
- `VALIDATED_FAILURE`;
- `VALIDATED_UNEXPECTED_DELIVERY` for the wrong delivered asset or amount;
- integer micro-units for issued amounts.

XRPL settlement in the journal and Core accounting settlement stay separate facts. A hosted
observation is neither.

## Unsupported in X1q, X1s and X1u

- Submission, signing and any ledger write.
- WebSocket subscriptions.
- Mainnet, Devnet and any network other than Testnet.
- Journal finalization, expiry proof, submission anchors or settlement events from hosted evidence.
- Non-Payment transaction observation (`NOT_A_PAYMENT`).
- Testnet Payments in the X0t journal. The X0 inspector requires `NetworkID`, which network 1
  forbids. See `contracts/requests/0010-xrpl-public-network-reconciliation.md`.
- Multi-endpoint quorum or cross-provider comparison.
