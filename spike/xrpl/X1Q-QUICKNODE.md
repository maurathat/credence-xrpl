# Checkpoint X1q — QuickNode XRPL read-only integration

## Outcome

X1q adds QuickNode as an optional, read-only connectivity provider for the public XRPL Testnet
(network ID 1). It is offline-tested against a deterministic fake Clio endpoint.

- **Real QuickNode verification: PENDING.** No endpoint was configured in this workspace, so no
  real response was observed and no sanitized real-response fixture exists.
- No XRPL ledger write, submission, signing, provider execution or token transfer.
- Unchanged:
  - the X0t journal and its schema;
  - the X1c multisignature verification;
  - `RippledTransport` and its pinning rules;
  - payment authorization and signing;
  - the settlement consumer;
  - Core, Inference, frozen canonical objects and fixtures.

Base: `99ca646dfd1fb1b3f8e4b473327595b1eedd4ad7` (X1c). The workspace branch
`maurathat/quicknode-xrpl-integration` was fast-forwarded to that commit from `810c787` (its own
ancestor, with no independent commits). No other workspace or branch was touched.

Contract: `contracts/QUICKNODE-ADAPTER-CONTRACT.md`. Decision:
`docs/decisions/0012-xrpl-x1q-quicknode-read-only.md`. Request:
`contracts/requests/0010-xrpl-public-network-reconciliation.md`.

## What QuickNode's documentation shows (and what it does not)

Sources:

- QuickNode XRPL `server_info`: <https://www.quicknode.com/docs/xrpl/server_info>
- QuickNode XRPL `server_state`: <https://www.quicknode.com/docs/xrpl/server_state>
- xrpl.org `server_state`:
  <https://xrpl.org/docs/references/http-websocket-apis/public-api-methods/server-info-methods/server_state>

Findings:

- **QuickNode serves XRPL through Clio.**
  - `server_info` returns `clio_version`, `rippled_version`, `network_id`, Clio's own
    `complete_ledgers` and `validated_ledger`, with warning 2001 ("This is a clio server. clio only
    serves validated data").
  - `server_state` is answered by a rippled node behind Clio and marked `"forwarded": true`.
- **The node key is not the endpoint's.** In the published examples the forwarded node's
  `complete_ledgers` (`102425420-…`) differs from Clio's (`32570-…`).
  - `pubkey_node` therefore identifies whichever rippled node the request was forwarded to.
  - Nothing in the documentation promises that node is the same across requests. X1q does not
    pin it.
- **Not yet established.** The published examples are mainnet (`network_id: 0`). Three things
  need a real Testnet endpoint:
  - that a QuickNode Testnet endpoint reports `network_id: 1`;
  - its host naming (`*.xrp-testnet.quiknode.pro` is assumed, not confirmed);
  - whether `pubkey_node` is stable in practice.

  The probe measures all three.

## Interface

`credence/xrpl_adapter/quicknode.py`:

- `QuickNodeConfig.from_environment()`
  - `CREDENCE_QUICKNODE_XRPL_URL` (secret);
  - `CREDENCE_QUICKNODE_XRPL_NETWORK=testnet` (required when the URL is set);
  - optional `CREDENCE_QUICKNODE_XRPL_LABEL`.
- `QuickNodeEndpoint`
  - HTTPS on `*.quiknode.pro` only;
  - immutable and not picklable;
  - its repr shows only a fingerprint.
- `QuickNodeJsonRpcClient`
  - verified TLS 1.2 or later;
  - no proxies, no redirects;
  - strict timeout;
  - bounded retries for timeouts, 429 and 5xx.
- `QuickNodeXRPLTransport`, read-only:
  - `validated_ledger_identity`;
  - `sample_node_identity`;
  - `account_state` (the X1a `AccountSnapshot`, with SignerList);
  - `account_objects`;
  - `account_lines`;
  - `observe_payment(hash)` returning `HostedPaymentObservation`;
  - `validated_ledger_observation` (reporting only).
- `QuickNodeReconciliationView`: the X0t `HashLookup` / `ValidatedLedgerObserver` binding, which
  is typed unsupported.
- `assess_hosted_payment`: advisory classification in the X0t verifier's terms.

`credence/xrpl_adapter/quicknode_probe.py`: the operator-run read-only probe and its sanitized
report.

Configuration example (values are placeholders, never committed):

```sh
export CREDENCE_QUICKNODE_XRPL_URL='https://<endpoint-name>.xrp-testnet.quiknode.pro/<token>/'
export CREDENCE_QUICKNODE_XRPL_NETWORK=testnet
uv run python -m credence.xrpl_adapter.quicknode_probe \
    --account <public Testnet account> --tx <existing public Testnet Payment hash> \
    --out .context/x1q-probe.json
```

Keep the report under `.context/` (gitignored) until it has been reviewed. It contains response
shapes and public ledger facts only, never the URL or token.

## Hosted-provider trust policy

The full policy is in the contract. In short, trust rests on:

- **Endpoint and credentials:** an explicitly configured HTTPS endpoint, verified TLS, and
  credential isolation (fingerprint only).
- **Network:** the expected network re-checked on every identity read. A contradiction latches the
  transport closed.
- **Validated ledger:**
  - its hash is confirmed by `ledger`;
  - one index answered with two hashes latches the transport closed;
  - progress must be monotonic;
  - its age is bounded.
- **History:** Clio's `complete_ledgers` must cover both the validated ledger and every
  transaction ledger.
- **Consistency:** repeated reads must agree.
- **No node pin.** A single QuickNode endpoint is a trusted source, not independent consensus
  verification.

## Network isolation results

All pass (offline):

| Check | Result |
| --- | --- |
| `testnet` accepted only with an explicit network variable | `NETWORK_NOT_CONFIGURED` without it |
| `mainnet`, `0` | `MAINNET_NOT_AUTHORIZED` |
| `21337`, `local`, `standalone` | `LOCAL_NETWORK_NOT_HOSTED` |
| `devnet`, `1` (not a name) | `UNSUPPORTED_NETWORK` |
| Endpoint host naming mainnet or devnet | `ENDPOINT_NETWORK_MISMATCH` |
| `server_info` reports 21337, 0 or 2 | `NETWORK_MISMATCH`, then latched `SOURCE_CONTRADICTED` even after the node "recovers" |
| Missing `network_id` (Clio or forwarded) | `NETWORK_IDENTITY_MISSING` (not latched) |
| Forwarded `server_state` reports network 0 | `NETWORK_MISMATCH`, latched |
| Payment carrying `NetworkID` 21337 or 1 | `TRANSACTION_NETWORK_MISMATCH` |
| View bound to a 21337 journal | `NETWORK_ISOLATION` at construction |
| Advisory assessment against a 21337 expectation | `UNRESOLVED` / `NETWORK_MISMATCH` |
| Hosted evidence offered to `PaymentResultVerifier` for a 21337 entry | `UNRESOLVED` / `FINAL_EVIDENCE_INVALID` |
| Mis-wired view on a 21337 journal across restart | Entry stays `UNKNOWN` (`QUICKNODE_TRANSACTION_NETWORK_MISMATCH`), 0 outbox events, no resubmission; later `VALIDATED_SUCCESS` with 1 event through the authorized local transport |

## Adversarial tests (offline)

`tests/unit/test_xrpl_x1q_quicknode.py` contains 91 tests. 60 of them are the brief's numbered
cases (`test_aNN_*`, counted after parametrization):

| # | Case | Tests |
| --- | --- | --- |
| 1 | Incorrect XRPL network | 3 |
| 2 | Missing network identity | 1 |
| 3 | Node-key changes behind the hosted endpoint (recorded, not pinned; backend network contradiction; local pin unchanged) | 3 |
| 4 | Incomplete ledger-history coverage (outside, gap, validated ledger outside or empty, not-found never proves non-inclusion) | 3 |
| 5 | Contradictory binary and JSON evidence | 11 |
| 6 | Incorrect delivered amount or issuer (`VALIDATED_UNEXPECTED_DELIVERY`; partial payments) | 3 |
| 7 | Unvalidated transaction | 1 |
| 8 | Malformed JSON-RPC responses | 11 |
| 9 | Redirects (301/302/303/307/308 and same-host) | 6 |
| 10 | Endpoint substitution and TLS verification | 2 |
| 11 | Request timeout | 1 |
| 12 | Rate limiting, server and auth errors | 2 |
| 13 | Repeated lookup, ledger-hash change, disappearing or changed transaction, stale/backwards/future ledgers, ledger beyond validated | 5 |
| 14 | Restart with an unresolved transaction | 2 |
| 15 | Testnet evidence treated as network 21337 evidence | 3 |
| 16 | Credential redaction and secret-scanning behavior | 3 |

The other 31 cover configuration and endpoint rules, the read paths, XRP and issued observations,
and the X0t type mapping.

## Gates (run on this tree)

| Gate | Result |
| --- | --- |
| `make lint` (ruff check, ruff format, large files, decision numbers) | pass |
| `make typecheck` (strict mypy, 54 source files) | pass |
| `tests/unit` | 1168 passed (1077 before X1q + 91 new) |
| `tests/conformance` (includes deterministic fixture regeneration) | 141 passed |
| `tests/integration -m "not integration"` | 64 passed, 28 skipped (as at base) |
| X0t/X1a/X1c regression (`test_xrpl_payment_readiness`, `test_xrpl_x1a_*`, `test_xrpl_x1c_remediation`, smoke) | 240 passed |
| gitleaks v8.21.2 (`make secrets`, directory scan) | no leaks |
| `git diff --check` | clean |
| Protected paths vs base (`journal`, `service`, `rippled`, `multisign`, `preparation`, `settlement`, `types`, `mock`, `trlusd_setup`, package exports, Core, signers, inference, agent, API, vectors, fixtures, X1a/X1c test worlds, ownership, tooling) | byte-identical (`git diff --quiet`) |

## Remaining dependencies for public-network reconciliation

1. **A configured QuickNode Testnet endpoint and a probe run.** The run must use a public account
   (ideally with a SignerList) and an existing public Payment hash. Its report should be reviewed
   before any shape fixture is committed. It answers:
   - whether `network_id: 1` is reported;
   - whether the host naming is as assumed;
   - whether `pubkey_node` is stable;
   - whether account reads echo `ledger_hash`;
   - the exact `tx` v2 shapes.
2. **The owner's answer to request 0010.**
   - Inspector and journal support for networks that forbid `NetworkID`.
   - Whether hosted evidence can ever finalize a journal entry, and under what evidence class.
   - Whether a hosted source may anchor submissions or prove non-inclusion.
3. **A Credence payment account and token issuer on Testnet**, if Testnet is to carry Credence
   payments at all. Today Credence runs on a local XRPL ledger with the lending amendments
   enabled.
4. **A gitleaks rule for QuickNode endpoint tokens.** The repository's `.gitleaks.toml` (owned by
   Maura) has none. X1q adds an in-repository test that fails on any `quiknode.pro/<token>` string
   instead.

## Unsupported capabilities (exact)

- Submission, signing, and any ledger write.
- WebSocket transport and subscriptions (documented extension only).
- Mainnet, Devnet, and any network other than Testnet.
- Journal finalization, `EXPIRED_NOT_INCLUDED`, submission anchors and settlement outbox events
  from QuickNode evidence. `QuickNodeReconciliationView` returns `UNAVAILABLE` with a typed
  `QUICKNODE_*` detail for every lookup and refuses the observer.
- Persisting or verifying Testnet Payments in the X0t journal (request 0010).
- Non-Payment transaction observation (`NOT_A_PAYMENT`).
- Node-key or build pinning for a hosted endpoint.
- Multi-endpoint quorum or cross-provider comparison.
- Durable hosted-consistency state. Ledger hashes, observed transactions and node keys are kept in
  memory for the transport's lifetime. The journal, not the transport, is the restart-safe record.

## Ownership note

`credence/xrpl_adapter/`, `contracts/XRPL-ADAPTER-CONTRACT.md` and `spike/xrpl/` are listed under
XRPL Integration (Codex) in `docs/OWNERSHIP.md`. For X1q only, Maura delegated the isolated
QuickNode module, its tests, a dedicated contract (plus a short pointer section in the existing
contract) and this report to Da Nang (Claude Code). `docs/OWNERSHIP.md` was not changed.
