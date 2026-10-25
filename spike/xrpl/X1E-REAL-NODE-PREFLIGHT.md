# Checkpoint X1e — real-node read-only compatibility preflight

## Outcome

X1c's transport was run read-only against a real pinned rippled 3.4.0 on API v2. Two transport corrections were made:

- **N1:** a pinned node identity is now required for authoritative reads;
- **N2:** the node identity is re-read and checked within every lookup.

Nothing was written to the node:

- no transaction was signed or submitted;
- no ledger was closed;
- no account was funded;
- no trust line, token or SignerList was created.

The Addis-Ababa experiment records were not altered:

- the historical container was not started;
- the historical volume `credence-xrpl-spike-data` was only read, through a read-only mount;
- every file in the volume hashed identically before and after.

Base: `99ca646` (X1c).

## The node

| Item | Verified value |
| --- | --- |
| Image | `credence-xrpl-spike:3.4.0`, Addis-Ababa's pinned build (`spike/xrpl/config/Dockerfile` at `7fdb768`), local image ID `sha256:ae83d611…` |
| Binary | `/usr/bin/xrpld` SHA-256 `9b4d3dd28b31a22419fd905682e238298eefb8941e2efcde5241a4b19fa0c417`, the same as `ENVIRONMENT.md` |
| Version | `xrpld version 3.4.0`, git `4a4fded2eba11427c48ce3f24d9c1aea5e7a9d17`, package `3.4.0-1 amd64` |
| Base config | `/etc/opt/xrpl/xrpld.cfg` SHA-256 `37ffc6f9…e820`, the same as `ENVIRONMENT.md` |
| Network ID | 21337 (`server_state.state.network_id`) |
| Node identity | `n9JFakqf8CSPigdXe6afH4G6iPXmt9XTJ9FxuAzzhCMbKbSC1Vbe`, stable across restart |
| Amendments | `feature`: 106 known, 9 enabled. The eight required (MPTokensV1, SingleAssetVault, LendingProtocol, LendingProtocolV1_1, fixCleanup3_1_3/3_2_0/3_3_0/3_4_0) are supported and enabled, plus fixRemoveNFTokenAutoTrustLine. This matches the historical record |
| Endpoint | `127.0.0.1:5015`, loopback only |
| Validated ledgers | `complete_ledgers` `"2"`: a fresh standalone genesis at validated index 2. The open ledger 3 is reported `validated: false` |

How the identity was established:

- **Without a seed, the identity is not stable.** With the historical config alone, `pubkey_node` changed on every restart (`n9M6yNGV…` → `n9Mq2UKp…`), because nothing persists a node seed. That identity cannot be pinned.
- **A seed now pins it.** A secp256k1 `[node_seed]` was generated in-process and piped straight into Docker volume `credence-x1e-node-identity` (file mode 0400). It was never printed, logged or written to the host or to Git. Only the derived public key was printed, and the node reports exactly that key before and after a restart.
- **Config otherwise unchanged.** The runtime config is the pinned base config byte-for-byte (verified by hash) plus that one stanza.
- **Logs are clean.** The container logs contain no seed-shaped strings.

**Historical ledger data is unavailable.** A copy of the historical volume, run with `--load`, failed with "The specified ledger could not be loaded". The historical runs persisted only the NuDB node store, with no SQL ledger index. No historical ledger or transaction can be served, so the node used is a fresh genesis. Both the attempt and its copy volume were removed.

**`infra/rippled/` is still the Checkpoint 0 placeholder.** It has no pinned image and no config, and `make ledger-up` refuses. The node above was established from the historical pinned image instead. Porting that Dockerfile and config into `infra/rippled/` is a remaining prerequisite and is not part of this read-only checkpoint.

## RPC compatibility matrix

The captured responses are in `x1e/responses/`, field shapes in `x1e/shapes.json`, and transport results in `x1e/transport-results.json`. File hashes are in `x1e/MANIFEST.json`. `tests/unit/test_xrpl_x1e_real_node_replay.py` replays the real responses through `RippledTransport` on every test run.

**Sanitization.** The node public key is not secret, but the secret scanner's generic rule flags any high-entropy value under a key-like field name. Every occurrence of it in the evidence files was therefore replaced with `REDACTED_PUBLIC_NODE_KEY`, and no other byte was changed. The real key is recorded in the node table above, and `MANIFEST.json` holds its SHA-256. A test checks that hash against the key in this report.

| Method | Real API v2 shape (where the transport reads) | X1c/X1e parser |
| --- | --- | --- |
| `server_state` | `result.state`: `network_id` int, `build_version` str, `pubkey_node` str, `complete_ledgers` str (`"2"`), `validated_ledger` {`seq` int, `hash` str, `base_fee` int, `reserve_base` int, `reserve_inc` int} | Compatible |
| `ledger` (validated index) | `result`: `ledger_hash` str, `ledger_index` int, `validated` true, `ledger` {`ledger_hash`, `ledger_index` int, `closed` true, …} | Compatible |
| `ledger` (open index 3) | `result`: `validated` false, no `ledger_hash`, `ledger_current_index`, `ledger` {`closed` false} | Compatible: refused as `LEDGER_NOT_VALIDATED` |
| `account_info` (funded) | `result.account_data` {`Account`, `Balance` str, `Flags` int, `OwnerCount` int, `Sequence` int, …}; `signer_lists` at the result root (API v2), `[]` when none; `ledger_hash`, `ledger_index`, `validated` | Compatible (root and v1 locations both read) |
| `account_info` (unfunded) | `status` error, `error` `actNotFound`, `error_code` 19; still carries `ledger_hash`, `ledger_index`, `validated` | Compatible: `exists=False` |
| `account_objects` | `account_objects` [], `ledger_hash`, `ledger_index`, `validated`; `actNotFound` for an unfunded account | Compatible (tickets are read only for existing accounts) |
| `account_lines` | `lines` [], `ledger_hash`, `ledger_index`, `validated` | Compatible |
| `tx` (not found, binary and JSON) | `status` error, `error` `txnNotFound`, `error_code` 29; no `searched_all`, because no ledger range was requested | X1g correction: compatible only as an unavailable/unresolved observation (`BOUNDED_LOOKUP_REQUIRED`), never non-inclusion proof. A bounded real-node response with `searched_all` remains unobserved. |
| `tx` (found, binary `tx_blob`/`meta_blob`, JSON `meta`/`delivered_amount`) | **Not observed.** The fresh ledger has no transactions, historical hashes return `txnNotFound`, and submitting one is not authorized | Verified only against the deterministic fake. Must be confirmed on a real validated Payment before settlement relies on it |

The historical Addis-Ababa `validated_transaction_response` records (for example `09-issue_tRLUSD_to_lender.json` at `7fdb768`) are sanitized API v1 JSON: top-level fields, `inLedger`, no `ledger_hash`, signatures removed. They show `meta.delivered_amount` as an issued-amount object. They are not API v2 binary evidence and were not used as such.

## N1 and N2

| Scenario | `99ca646` | X1e |
| --- | --- | --- |
| N1: authoritative lookup with no node pin | `FOUND`, validated | `UNAVAILABLE` `NODE_IDENTITY_NOT_PINNED` (no `tx` call is made) |
| N1: pinned node key changed | `UNAVAILABLE` `NODE_IDENTITY_MISMATCH` (already refused) | Unchanged |
| N2: network changes between identity reads | `FOUND`, validated (1 identity read) | `UNAVAILABLE` `NETWORK_MISMATCH` (2 reads) |
| N2: node key changes between reads | `FOUND`, validated | `UNAVAILABLE` `NODE_IDENTITY_MISMATCH` |
| N2: build changes between reads | `FOUND`, validated | `UNAVAILABLE` `BUILD_VERSION_MISMATCH` |
| N2: history loses the transaction's ledger | `FOUND`, validated | `UNAVAILABLE` `LEDGER_OUTSIDE_COMPLETE_HISTORY` |

What changed:

- **N1.** Three operations can authorize signing, anchor expiry or establish settlement: `lookup`, `validated_ledger` and `ledger_context`. They now refuse `NODE_IDENTITY_NOT_PINNED` without `expected_node_public_key`. So an unpinned transport also cannot feed the policy signer, which sees `LEDGER_UNAVAILABLE`.
- **Diagnostic reads.** `diagnostic_lookup` works without a pin but returns only a `DiagnosticLookup` summary, never a `LookupResult`, `LedgerPayment` or evidence envelope. It cannot be handed to the journal. Read-only setup observation stays diagnostic.
- **N2.** After the binary, JSON and ledger evidence, the lookup re-reads `server_state` and `ledger` and requires:
  - the same network, build and node key (`IDENTITY_CHANGED_DURING_LOOKUP`, or the pin's own code);
  - no regression of the validated ledger (`VALIDATED_LEDGER_REGRESSED`) and no hash change at the same index;
  - the transaction's ledger still at or below the validated ledger, inside complete history, and hash-consistent.

  Both identity reads are kept in the evidence envelope (`validated_identity`, `validated_identity_after`, and the raw responses).
- **On the real node.** The unpinned lookup was refused `NODE_IDENTITY_NOT_PINNED`, an unpinned `validated_ledger` raised `LookupUnavailable`, a wrong pin was refused `NODE_IDENTITY_MISMATCH`, and a wrong network was refused `NETWORK_MISMATCH`.

## Submission blockers (inspected, not changed)

These must be closed before any ledger-writing checkpoint.

- **N3, journal finalization.** `PaymentJournal.finalize` (`credence/xrpl_adapter/journal.py:653`) is public and accepts caller-supplied state, result, evidence and verification version. A local probe (not committed) persisted a Payment and called `finalize(VALIDATED_SUCCESS, evidence=b"caller-produced", evidence_verification_version=1)`. That produced `XRPL_SETTLED` and one outbox event with no lookup and no verifier. Finalization must be reachable only from the verified reconciliation path.
- **N4, persistence without multisign verification.** `PaymentService.prepare_and_persist` (`credence/xrpl_adapter/service.py:322-326`) checks only the X0 structural inspector. `PaymentJournal.persist_signed` (`journal.py:389`) is public and checks nothing. The same probe persisted the X0 fixture's noncryptographic `TxnSignature="AA"` Payment. The live path must require `verify_multisigned_payment` against the policy, the authorized digest and a pinned ledger context before persistence.
- **N5, self-declared offline flag.** `PaymentService` (`service.py:309`) accepts any transport with `offline_only is True`. `RippledTransport` is refused only because its own flag is false, and `submit` is gated by its own `submission_enabled` config. A reviewed, explicit live-submission authority must replace both self-declared flags.

Other remaining prerequisites, unchanged:

- Core request 0009, the accounting-settlement read;
- key custody for the policy signer and settlement authority;
- ResearchBot signing integration;
- tRLUSD issuer, trust-line and SignerList setup, and XRP funding (C4);
- reissue and replacement (C5);
- a production Core settlement effect;
- a real validated Payment to confirm the found-`tx` shapes;
- porting the pinned node into `infra/rippled/`.

Passing these read-only checks does not authorize ledger submission.

## Local environment left behind

All of these were created by X1e. The historical container and volume are untouched.

- **Container** `credence-x1e-readonly`: stopped at the end of the checkpoint.
- **Volume** `credence-x1e-readonly-data`: a fresh standalone ledger.
- **Volume** `credence-x1e-node-identity`: the runtime config holding the node seed. Keep or delete deliberately. It is the only copy of the seed behind the pinned key.
