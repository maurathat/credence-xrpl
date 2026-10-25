# T1a offline remediation evidence

Scope: B1, B2 and C1–C4 only. This record compares the T1 parent at
`f9db879bf97f64e875d14b3b615a37ef22f62eb1` with the remediated offline code.
No faucet, key-generation, signing, submission, live RPC, or other ledger-write
operation was used to produce this evidence.

## Adversarial before and after

| Finding | Parent behavior | Remediated invariant | Offline adversarial evidence |
| --- | --- | --- | --- |
| B1 polling | A later `server_info` index beyond expiry was treated as non-inclusion without checking complete history or `searched_all`. | Polling pins validated-ledger identity before and after a bounded hash lookup. `EXPIRED_NOT_INCLUDED` requires an index beyond expiry, continuous relevant history and literal `searched_all: true`; contradictions and gaps remain `UNKNOWN`. | Last-valid inclusion after ledger advance succeeds; missing/false/non-Boolean `searched_all`, history gaps and contradictory hashes stop fail-closed. |
| B2 expiry | Structure validation required only a positive `LastLedgerSequence`; signing and submission did not bind it to construction. | Construction records its validated-ledger index/hash and identity. Construction, signing and submission independently require `LastLedgerSequence == construction ledger + 4`. | Altered expiry is rejected before credential handling or submission preflight; the constructed value is asserted exactly. |
| C1 transport | `urllib.request.urlopen` inherited environment proxies and redirect behavior. | A private opener contains `ProxyHandler({})`, refuses redirects, and requires an unchanged final URL. Exact endpoint and network-ID allowlists remain in force. | HTTP 301, 302, 303, 307 and 308 are refused; poisoned proxy variables are ignored; a changed response destination is refused. |
| C2 replay | The journal limited one submission per editable authorization ID, not per signed transaction hash. | A durable unique SQLite index also claims the canonical verified signed hash across authorization IDs and restarts. | Restart plus changed authorization ID cannot claim the same hash; one authorization cannot claim a second hash. |
| C3 lifetime | Authorization had only an expiry check and no maximum lifetime or file revalidation. | Schema v2 requires UTC `issued_at` and `expires_at`, a positive lifetime of at most 24 hours, current validity and an unchanged private mode-0600 file. | Missing, malformed, non-UTC, future, expired, contradictory, over-24-hour and exact-boundary cases are covered; expiry during signing/submission preparation is refused before the action. |
| C4 library gate | `--execute` existed only in the CLI; direct protected library calls could bypass it. | `setup_accounts`, `sign_unsigned` and `submit_once` require explicit Boolean intent plus a sealed, operation-specific capability bound to the authorization digest, network, accounts, one-drop label and transaction identity. | Each protected function rejects absent/false intent, absent/malformed capability and network mismatch before network/sign/submit; cross-operation and cross-transaction reuse are refused. |

The detailed executable cases are in `tests/test_t1_public_testnet.py`. The frozen
payment remains one native XRP drop between two distinct classic Testnet accounts,
with only `tfFullyCanonicalSig`, exact fee/sequence/expiry terms, and one memo carrying:

> XRPL connectivity proof transaction. Not governed by Credence procurement authorization.

## Durability and trust boundaries

- Replay refusal is durable only while the fixed local journal remains intact. Manual
  deletion, copying artifacts to another checkout, or using another device is outside
  this boundary. It is not global replay protection.
- The primary and secondary services are public third-party Testnet endpoints. Their
  continued availability and historical coverage are runtime prerequisites, not
  guarantees made by the offline suite.
- `UNKNOWN` is intentionally sticky. Recovery reconciles the original hash and never
  authorizes a replacement transaction.
- Authorization validates operator intent; it does not change Credence procurement,
  custody, journal, Core, X1, or marketplace payment authority.

## Acceptance boundary

All tests in this remediation are offline and use monkeypatched transport, signing,
wallet and ledger observations. The eventual execution remains prohibited until a
separate T1 execution authorization is issued and Salvador completes independent T1a
acceptance.

## Offline verification

The pre-commit acceptance run completed with:

- `uv run pytest -q spike/xrpl/tests/test_t1_public_testnet.py`: 80 passed.
- `uv run pytest -q spike/xrpl/tests`: 261 passed.
- `make test`: 17 unit and 4 conformance tests passed.
- `make lint`: Ruff check, Ruff format check across 99 files, and large-file check passed.
- `make typecheck`: strict project mypy passed across 25 source files.
- `uv run mypy --strict spike/xrpl/scripts/t1_public_testnet`: passed across 8 files.
- `make secrets`: gitleaks completed with no leaks found.
- `git diff --check`: passed.
