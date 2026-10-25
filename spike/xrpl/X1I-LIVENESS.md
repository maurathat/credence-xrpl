# Checkpoint X1i — XRPL payment liveness and non-inclusion corrections

## Outcome

X1i corrects Salvador X1h findings H1-H3 within XRPL-owned read and verification code. No
transaction was signed or submitted, no ledger was written, and production payment authorization
was not changed.

Base: `3d1add76b4cf7b6bde71ac280e191d1e2c3ec3ca`.

## Independent reproductions at the base

- H1: validated inclusion at ledger 101 was `FOUND` for windows 999 and 1,000, but a 1,500-ledger
  window was rejected before the `tx` RPC as `UNAVAILABLE: MALFORMED_LOOKUP_RANGE`.
- H2: after a lost submit response, reconciliations at validated ledgers 101, 105 and 109 all
  became `UNKNOWN: TRANSACTION_RANGE_NOT_FULLY_SEARCHED`; no identical-byte rebroadcast occurred.
- H3: `None`, empty, whitespace and malformed node pins all constructed successfully.

## Correction and adversarial results

- Hash lookup is unbounded first. Validated inclusion at ledger 101 is now `FOUND` for 999-,
  1,000- and 1,500-ledger requested windows, with the existing exact-byte and ledger verification.
- A long-window miss is `NOT_FOUND` with no proof and only one ordinary `tx` request. It remains
  unresolved after expiry.
- At ledgers 101, 105 and 109, a stable unproven miss permits an explicitly requested rebroadcast.
  Four total attempts (initial timeout plus three reconciliations) contain byte-for-byte identical
  signed transactions and the same hash, including after reconstructing `PaymentService`.
- `UNAVAILABLE` does not rebroadcast. After ledger 110, an unproven miss neither rebroadcasts nor
  finalizes and remains `UNKNOWN`.
- Fake transaction-search ranges are separate from fake `complete_ledgers`. Complete ledger
  history with no transaction-search coverage returns `searched_all=false` and no proof.
- Missing and malformed node pins fail during `RippledTransportConfig` construction. A valid node
  public key is decoded through XRPL address-codec before acceptance.

## Real rippled semantics

Official rippled tag `3.4.0`, commit
`4a4fded2eba11427c48ce3f24d9c1aea5e7a9d17`, was inspected without running or writing a node.
`Tx.cpp` sources `searched_all` from the ranged transaction-database fetch and does not derive it
from `complete_ledgers`. The upstream transaction RPC tests exercise true complete search, false
truncated/gapped search, and successful lookup outside the requested range. The existing X1e node
capture contains only ordinary unbounded misses and therefore supplies no non-inclusion proof.

## Remaining limitations

- `EXPIRED_NOT_INCLUDED` still depends on one trusted pinned node; it is not independent consensus
  verification. It remains unavailable whenever that node cannot return literal
  `searched_all=true` for the full supported bounded window.
- A self-reported node key over unauthenticated loopback HTTP is not server authentication. It
  detects configuration drift and inconsistent node identity only.
- Found-transaction response shapes are still fake-tested because the preserved X1e node was never
  written to.
- N3, N4 and N5 remain ledger-write blockers. No signing, custody, public transport, account setup,
  Core modification or live submission is authorized by X1i.
- The QuickNode branch requires the separate interface and decision-number resolution recorded in
  decision 0014.
