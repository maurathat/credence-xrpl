# 0014. XRPL X1i payment liveness and non-inclusion

- Status: Proposed. Awaiting Salvador's targeted X1i acceptance.
- Date: 2026-10-24
- Scope: XRPL read and verification path only; no ledger writes.
- Base: `3d1add76b4cf7b6bde71ac280e191d1e2c3ec3ca`.

## Decisions

1. **Positive inclusion is range-independent.** Lookup asks for the hash without a ledger range
   first. If found, the existing signed-byte, hash, validation, metadata, delivered-amount,
   ledger-hash and identity checks run. The non-inclusion window is not allowed to suppress this
   path.
2. **The bounded query is negative evidence only.** After ordinary `txnNotFound`, a second `tx`
   query uses the persisted anchor+1 and `LastLedgerSequence` only when rippled 3.4.0 supports the
   range. Only literal `searched_all=true`, stable node identity, and continuous ledger history
   can construct `NonInclusionProof`.
3. **Non-final absence remains live.** A stable absence without proof is `NOT_FOUND`, not
   `UNAVAILABLE`. Before expiry, an operator-requested reconciliation may rebroadcast the exact
   persisted bytes/hash. After expiry it stays `UNKNOWN`. Transport or identity failures are
   `UNAVAILABLE` and do not rebroadcast. Credence never creates a replacement transaction.
4. **Search coverage is not ledger coverage.** The deterministic fake exposes transaction search
   ranges independently from `complete_ledgers`. Tests must configure each explicitly.
5. **Node pins are mandatory configuration.** Missing, blank, whitespace, or address-codec-invalid
   XRPL node public keys are rejected before transport construction. Because the common local
   endpoint is unauthenticated loopback HTTP and the node reports its own key, the pin is a
   consistency and misrouting check, not server authentication or consensus proof.

## rippled 3.4.0 basis

The official `3.4.0` source at commit
`4a4fded2eba11427c48ce3f24d9c1aea5e7a9d17` establishes the behavior independently:

- `src/xrpld/rpc/handlers/transaction/Tx.cpp` rejects a range only when
  `max_ledger - min_ledger > 1000`, obtains `TxSearched` from
  `TransactionMaster.fetch(hash, range)`, and emits `searched_all` only when that result is known;
- `src/test/rpc/Transaction_test.cpp` covers true complete searches, false truncated or gapped
  transaction history, found transactions outside a requested range, and omission of
  `searched_all` for ordinary unbounded not-found responses.

`server_state.complete_ledgers` is not the source of `searched_all`. The committed X1e capture is
an unbounded `txnNotFound`, so it proves only that an ordinary miss carries no proof. Automatic
expiry remains disabled for such observations and for every bounded response other than literal
`searched_all=true` with the additional Credence continuity checks.

## Separate QuickNode/Yeosu integration resolution

The unmerged `maurathat/quicknode-xrpl-integration` branch is not modified here. It has three
collisions requiring a separate review:

- `QuickNodeReconciliationView.lookup(transaction_hash)` does not implement the current
  `HashLookup.lookup(transaction_hash, *, min_ledger=None, max_ledger=None)` signature;
- its decisions named 0012 and 0013 collide with the existing X1e and X1g decisions;
- it modifies `contracts/XRPL-ADAPTER-CONTRACT.md` while replacing X1e/X1g artifacts from an older
  base.

The minimum resolution is to rebase after X1i, preserve decisions 0012-0014 and the X1e/X1g/X1i
artifacts, assign new decision numbers, and update the read-only QuickNode view to accept the
bounded keyword arguments while still returning its typed unsupported result. That change must be
reviewed on the QuickNode workstream; it does not authorize hosted-source finality.
