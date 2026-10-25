# T1c pre-query validated-history coverage evidence

Scope: T1c-H1 only, based on T1b commit
`a5e2d4bd28fb94c4f8b1d7f4a75ab5c060d23782`. R2 and the accepted R1 index boundary
are unchanged. No public Testnet or other live XRPL operation was performed.

## Correction

The terminal `EXPIRED_NOT_INCLUDED` guard now requires `history_covers` to succeed
independently for both the pre-query and post-query `complete_ledgers` observations
over the identical construction-ledger + 1 through `LastLedgerSequence` range.

If either observation omits any possible-inclusion ledger, polling records
`UNKNOWN_HISTORY_GAP` and stops without terminal non-inclusion, replacement, or
resubmission. The existing requirements remain unchanged: literal
`searched_all: true`, both validated-ledger indices strictly beyond expiry,
construction-ledger identity, bounded query range, consistent ledger progression,
and complete post-query history.

## Offline regression evidence

- Salvador's GAP ANALOG is reproduced with construction V=1000, inclusion at V+2,
  LLS=V+4, a pre-query V+6 observation missing only V+2, literal
  `searched_all: true`, and a post-query V+6 observation covering all history. It
  remains `UNKNOWN_HISTORY_GAP`.
- The inverse case—complete pre-query history and incomplete post-query history—also
  remains `UNKNOWN_HISTORY_GAP`.
- Complete pre/post history with both indices beyond LLS and genuine bounded
  not-found remains conclusively `EXPIRED_NOT_INCLUDED`.
- A transaction observed in the final eligible ledger still validates.
- A later lookup changes the GAP ANALOG transaction from unknown to validated using
  the same original hash and its V+2 ledger.
- The unknown path asserts one unchanged pre-existing journal claim, only a bounded
  `tx` RPC, zero submit RPCs, and no replacement Payment construction.

## Verification

- `uv run pytest -q spike/xrpl/tests/test_t1_public_testnet.py`: 93 passed.
- `uv run pytest -q spike/xrpl/tests`: 274 passed.
- `make test`: 17 unit and 4 conformance tests passed.
- `make lint`: Ruff and large-file checks passed.
- `make typecheck`: strict project mypy passed across 25 source files.
- `uv run mypy --strict spike/xrpl/scripts/t1_public_testnet`: passed across 8 files.
- `make secrets`: gitleaks reported no leaks.
- `git diff --check`: passed.

## Residual limitations

Polling still relies on each public node accurately reporting validated-ledger
identity, `complete_ledgers`, and bounded-search status. Any missing, malformed,
inconsistent, or incomplete evidence remains unknown. Later reconciliation requires
another poll of the original hash. Local journal deletion or another checkout remains
outside the existing replay-protection boundary.
