# T1 public XRPL Testnet connectivity proof

Status: offline preparation complete; execution is not authorized.

Required transaction label:

> XRPL connectivity proof transaction. Not governed by Credence procurement authorization.

This is an isolated connectivity proof. It does not change Credence Core, X1,
journal, custody, marketplace payment authority, or the product's local-ledger
claim. No faucet request, key generation, signing, submission, or ledger write is
part of this planning checkpoint.

## Frozen public services

- Primary native Testnet JSON-RPC: `https://s.altnet.rippletest.net:51234/`
- Independent native Testnet JSON-RPC: `https://testnet.xrpl-labs.com/`
- Official Testnet faucet API: `https://faucet.altnet.rippletest.net/accounts`
- Official interactive faucet: `https://xrpl.org/resources/dev-tools/xrp-faucets`

The primary and independent RPC endpoints are listed on the XRPL public servers
page: <https://xrpl.org/docs/tutorials/public-servers>. Ripple's public service is
appropriate for this one proof, not sustained or business use.

The scripts accept no endpoint argument and no endpoint credential. Both RPC URLs
are credential-free HTTPS allowlist entries. Mainnet, Devnet, XRPL EVM, localhost,
private endpoints, and the repository's standalone network ID 21337 are refused.

## Network identity rule

Every network-capable stage first calls `server_info` and requires the exact integer
`result.info.network_id == 1`, a validated-ledger index and hash, and a declared
complete-ledger range. IDs 0, 2, 21337 and the XRPL EVM chain IDs are explicitly
refused; every other ID is refused as unknown.

The HTTP client ignores `HTTP_PROXY`, `HTTPS_PROXY` and related environment proxy
configuration. It installs no proxy, refuses HTTP 301, 302, 303, 307 and 308
redirects, and rejects a response whose final URL differs from the fixed requested
URL. Neither CLI nor library callers can provide a replacement endpoint outside the
two exact allowlist entries.

The transaction itself must omit `NetworkID`. Native XRPL Testnet is network ID 1,
and the protocol disallows the transaction field on networks with IDs at or below
1024. See the official common-fields reference:
<https://xrpl.org/docs/references/protocol/transactions/common-fields>.

## Frozen transaction

The only permitted transaction is a direct native XRP `Payment`:

| Field | Rule |
| --- | --- |
| `Account` | Separate authorization's Testnet-only source classic address |
| `Destination` | Separate authorization's distinct, funded Testnet-only classic address |
| `Amount` | Exact string `"1"` drop |
| `Flags` | Exact `tfFullyCanonicalSig` (`0x80000000`) |
| `Fee` | Exact `fee.drops.open_ledger_fee` observed at construction; 1–1000 drops; never bumped after signing |
| `Sequence` | Exact source `Sequence` from the pinned validated ledger |
| `LastLedgerSequence` | Pinned validated ledger index + 4 |
| `NetworkID` | Omitted, as required for native Testnet ID 1 |
| `Memos` | Exactly one memo; no destination tag, paths, partial payment, token amount, or other fields |

The one memo is:

- `MemoType`, UTF-8 then uppercase hex: `claw-street:t1-connectivity-proof`
- `MemoData`, UTF-8 then uppercase hex: the exact required label above

Both accounts must already exist on the pinned validated ledger, be classic
addresses, have `OwnerCount == 0`, and be distinct. The source master key must be
enabled. The destination must not require a destination tag or deposit authorization.
The source balance after amount and fee must remain at or above the reported base
reserve.

Fee guidance and the +4 expiry rule come from the official transaction cost and
reliable submission references:

- <https://xrpl.org/docs/concepts/transactions/transaction-cost>
- <https://xrpl.org/docs/concepts/transactions/reliable-transaction-submission>

## Authorization and secret boundary

No script generates a wallet. The operator must create two Testnet-only wallets
outside this repository using a separately approved method. Never reuse Mainnet
credentials.

Faucet, signing, and submission each require all three gates:

1. a mode-0600 authorization JSON file stored outside the repository; and
2. the explicit CLI `--execute` switch; and
3. an opaque, operation-specific library capability issued only after independently
   supplied execution intent and authorization revalidation.

`setup_accounts`, `sign_unsigned` and `submit_once` default to refusal. A direct
library call cannot bypass the CLI gate: each protected function validates execution
intent, the capability seal, authorization digest, operation, network ID, accounts,
amount, label and—when applicable—the exact transaction identity before any network
call, signing, or submission. Authorization is checked again immediately before the
protected action.

The authorization is exact, UTC-only and short-lived. `expires_at` must be later than
`issued_at`, no more than 24 hours after it, and later than every validation instant.
Missing, malformed, non-UTC, future-dated, expired and contradictory timestamps are
refused. The private file is re-read and its SHA-256 digest and complete parsed value
must remain unchanged during an operation.

```json
{
  "schema": "credence.t1.execution-authorization.v2",
  "authorization_id": "operator-assigned-unique-id",
  "issued_at": "YYYY-MM-DDTHH:MM:SSZ",
  "expires_at": "YYYY-MM-DDTHH:MM:SSZ",
  "network_id": 1,
  "label": "XRPL connectivity proof transaction. Not governed by Credence procurement authorization.",
  "amount_drops": "1",
  "source": "<source classic address>",
  "destination": "<destination classic address>",
  "allowed_operations": ["sign", "submit", "poll", "capture"],
  "transaction_identity": "<64-character uppercase SHA-256 identity>"
}
```

Account setup may use `transaction_identity: null` and only `faucet`. Construction
may use `transaction_identity: null` and only `construct`, because the exact fee and
sequence do not exist until validated Testnet state is read. After construction, the
operator must issue a fresh payment authorization with the unsigned artifact's exact
`transaction_identity`, the same `authorization_id` as that artifact, and only the
needed later operations. Editing an authorization while a command is running causes
refusal.

The signing credential is accepted only through a private terminal prompt. It is
never accepted as a command argument, environment variable, file, report field, or
log field. The signed blob is written mode 0600 to the ignored `evidence/raw/` tree;
it is not console output or committed evidence. Do not capture the signing screen.

## Replay and unexpected-result rules

The scripts use one fixed SQLite operation journal at
`spike/xrpl/evidence/raw/t1/operations.sqlite3`. Before a faucet request or
submission, the script commits a unique operation claim with full synchronous
durability. A repeated authorization/operation/target is refused. Only one
submission hash is allowed for an authorization, and the same canonical signed
transaction hash cannot be claimed under another authorization ID. These constraints
survive process restarts. The command line cannot redirect the replay journal to a
fresh file.

This is local replay protection, not a global guarantee. Deleting the journal or
using another independent checkout/device is outside its durability boundary. If the
journal is lost, stop for review; do not reconstruct submission authority from the
authorization file alone.

Submission uses the frozen blob and `fail_hard=true`. A timeout after the request is
an unknown outcome: do not submit again. Poll only by the already computed hash.
Polling reads a validated-ledger identity before the bounded `tx` lookup and again
after it. It checks that the node stayed on native Testnet, did not move backwards,
and still agrees with the construction/prior ledger hashes.

`EXPIRED_NOT_INCLUDED` is recorded only when the latest trusted validated ledger is
strictly greater than the frozen `LastLedgerSequence`, the bounded lookup covers
construction ledger + 1 through `LastLedgerSequence`, its result contains literal
`searched_all: true`, and `complete_ledgers` covers that entire range without a gap.
A last-valid-ledger inclusion wins even when the ledger advances during the query.
Missing evidence, a history gap, inconsistent ledger identity, timeout, or an
unexpected response remains `UNKNOWN`; the branch stops and reconciles only by the
original hash. No replacement is built or submitted.

## Eventual evidence

After validated `tesSUCCESS`, evidence capture reads the transaction and its validated
ledger independently from both allowlisted Testnet endpoints. It writes evidence only
if both agree on:

- transaction hash and validated `tesSUCCESS` result;
- ledger index and ledger hash;
- exact 1-drop amount, fee, source, destination, sequence and expiry;
- exact decoded memo type and required label.

The evidence record contains those fields, the two public endpoint observations and
SHA-256 digests of their complete responses. It omits the signed blob, signature,
signing public key, credentials and raw responses.

## Scripts and later execution order

Run from the repository root with `PYTHONPATH=spike/xrpl/scripts`. These commands are
documentation only until explicit T1 execution authorization is delivered.

```text
uv run python -m t1_public_testnet.account_setup --authorization /secure/t1-account-auth.json --execute
uv run python -m t1_public_testnet.construct_transaction --authorization /secure/t1-construct-auth.json --output spike/xrpl/evidence/raw/t1/unsigned.json
# Stop: independently inspect unsigned.json and issue t1-payment-auth.json with its exact transaction_identity.
uv run python -m t1_public_testnet.sign_transaction --authorization /secure/t1-payment-auth.json --input spike/xrpl/evidence/raw/t1/unsigned.json --output spike/xrpl/evidence/raw/t1/signed.json --execute
uv run python -m t1_public_testnet.submit_transaction --authorization /secure/t1-payment-auth.json --input spike/xrpl/evidence/raw/t1/signed.json --execute
uv run python -m t1_public_testnet.poll_transaction --authorization /secure/t1-payment-auth.json --input spike/xrpl/evidence/raw/t1/signed.json
uv run python -m t1_public_testnet.capture_evidence --authorization /secure/t1-payment-auth.json --input spike/xrpl/evidence/raw/t1/signed.json --output spike/xrpl/evidence/t1/validated-transaction.json
```

Stop after evidence capture. Do not generalize this authorization to a second payment
or to Credence procurement settlement.
