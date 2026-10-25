# T1b R1/R2 offline hardening evidence

Scope: R1 and R2 only, based on accepted T1a commit
`2603839a41bfefc5c672ef147b51fbf2402e981c`. No public Testnet execution or other
live XRPL operation was performed.

## R1: pre-query expiry boundary

Before this change, a bounded not-found response could become
`EXPIRED_NOT_INCLUDED` when the post-query validated ledger had passed
`LastLedgerSequence`, even if the pre-query ledger had not. The terminal condition
now independently requires the pre-query validated-ledger index to be strictly
greater than `LastLedgerSequence`, in addition to all accepted B1 checks.

The offline regressions cover:

- Salvador's exact V+3 pre-query, literal `searched_all: true`, V+5 post-query
  reproduction, which remains `UNKNOWN_PREQUERY_LEDGER_NOT_EXPIRED`;
- the strict boundary at LLS and the permitted terminal boundary at LLS+1;
- incomplete and gapped history;
- inclusion in the final valid ledger after the node advances; and
- a later successful lookup of the same original transaction hash after the R1
  unknown classification.

No replacement or resubmission path was added.

## R2: construction-ledger submission binding

Before this change, signing bound the internally consistent construction fields but
submission preflight did not compare the claimed construction ledger with validated
node history. Preflight now refuses before journal claim or submit RPC unless:

1. the recorded construction index is no greater than current validated state;
2. an exact validated historical-ledger lookup returns the recorded construction
   hash; and
3. when construction is the current ledger, `server_info` and the exact ledger lookup
   agree on its identity.

The offline regressions cover a shifted future construction ledger, false hash,
missing historical ledger, inconsistent current-node observations, a correct
historical index/hash and a correct current index/hash. Every negative case asserts
zero submit RPCs.

## Boundary

These checks retain the accepted T1a authorization, transaction-identity,
exact-four-ledger expiry, network-ID, replay, transport and redaction controls. Local
journal deletion or use of another checkout remains outside the replay-protection
boundary. Public endpoint availability and historical coverage remain runtime
prerequisites. T1 execution still requires separate explicit authorization.

## Offline verification

- `uv run pytest -q spike/xrpl/tests/test_t1_public_testnet.py`: 91 passed.
- `uv run pytest -q spike/xrpl/tests`: 272 passed.
- `make test`: 17 unit and 4 conformance tests passed.
- `make lint`: Ruff checks across 100 files and the large-file check passed.
- `make typecheck`: strict project mypy passed across 25 source files.
- `uv run mypy --strict spike/xrpl/scripts/t1_public_testnet`: passed across 8 files.
- `make secrets`: gitleaks reported no leaks.
- `git diff --check`: passed.
