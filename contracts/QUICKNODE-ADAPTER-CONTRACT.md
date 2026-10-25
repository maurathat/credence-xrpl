# QUICKNODE-ADAPTER-CONTRACT

Owner: XRPL Integration. For checkpoint X1q only, Maura delegated this file and
`credence/xrpl_adapter/quicknode.py` / `quicknode_probe.py` to Da Nang (Claude Code).

Status: X1q, read-only, offline-tested. Real-endpoint verification is **PENDING**, because no
QuickNode endpoint was configured. Decision: `docs/decisions/0012-xrpl-x1q-quicknode-read-only.md`.
Readiness report: `spike/xrpl/X1Q-QUICKNODE.md`.

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
| `QuickNodeJsonRpcClient` | POSTs JSON-RPC to the pinned URL only. TLS is verified (default trust store, hostname check, TLS 1.2 or later). Environment proxies are ignored and redirects are never followed. Each request has a strict timeout (default 10 s, max 30 s). Timeouts, HTTP 429 (honoring `Retry-After`) and 5xx get bounded retries: default 3 attempts, exponential backoff, capped at 5 s. 3xx, 401/403, TLS failures and other statuses are not retried. Errors carry no URL and have no chained cause or context. |
| `QuickNodeXRPLTransport.validated_ledger_identity()` | `server_info` supplies the network ID, Clio version, validated ledger and `complete_ledgers`; `ledger` then confirms the index and hash. |
| `QuickNodeXRPLTransport.sample_node_identity()` | One forwarded `server_state`: `pubkey_node`, build, network ID and the `forwarded` flag. Samples are recorded, never pinned. |
| `account_state` / `account_objects` / `account_lines` | `account_info` with SignerList, `account_objects` and `account_lines`, all pinned to the identity's `ledger_hash` and paged up to a bound. Account state is the X1a `AccountSnapshot` type. |
| `observe_payment(hash)` | Returns a `HostedPaymentObservation` with status `VALIDATED`, `PROVISIONAL`, `NOT_FOUND`, `UNAVAILABLE` (retryable) or `REFUSED`. Ledger and transport faults never raise. |
| `validated_ledger_observation(account)` | History coverage and sender state in the X0t `ValidatedLedgerObservation` type, **for reporting only**. |
| `QuickNodeReconciliationView` | The only binding to the X0t `HashLookup` and `ValidatedLedgerObserver` seams (see "Payment reconciliation"). |
| `assess_hosted_payment(observation, expectation)` | Advisory classification in the X0t verifier's terms. `settlement_authority` is always false. |
| `python -m credence.xrpl_adapter.quicknode_probe` | Operator-run, read-only compatibility probe that writes a sanitized report. It exits 2 with "PENDING" when no endpoint is configured. |

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

## Unsupported in X1q

- Submission, signing and any ledger write.
- WebSocket subscriptions.
- Mainnet, Devnet and any network other than Testnet.
- Journal finalization, expiry proof, submission anchors or settlement events from hosted evidence.
- Non-Payment transaction observation (`NOT_A_PAYMENT`).
- Testnet Payments in the X0t journal. The X0 inspector requires `NetworkID`, which network 1
  forbids. See `contracts/requests/0010-xrpl-public-network-reconciliation.md`.
- Multi-endpoint quorum or cross-provider comparison.
