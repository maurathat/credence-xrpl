# XRPL-ADAPTER-CONTRACT

Owner: XRPL Integration (Codex)

Status: X0 payment and settlement readiness. Only the owner edits this file;
other workstreams file a request in `contracts/requests/`.

## Boundary

The adapter records four independent facts. None implies another:

1. inference delivery was verified;
2. Core accounting settlement completed;
3. an exact signed XRPL transaction was submitted; and
4. that transaction reached a validated final outcome.

X0 does not change Core accounting semantics. Its only downstream seam is an
atomic, one-time claim that becomes available after a Payment is both validated
with `tesSUCCESS` and verified against the authorized intent. The journal then
records `XRPL_SETTLED`. A provisional submit response, including `tesSUCCESS`,
never creates that status.

## Public interfaces

| Interface | X0 implementation | Contract |
| --- | --- | --- |
| Transaction preparation | `TransactionPreparer` / `OfflinePaymentPreparer` | Validate operation, separate payment authorization, account, destination, tRLUSD wire identity, integer micro-unit amount, fee ceiling, network, Sequence or TicketSequence, and LastLedgerSequence. |
| Signed persistence | `SignedTransactionInspector` / `PaymentJournal.persist_signed` | Decode the actual signed bytes with xrpl-py, require a Payment signature, match every authorized field, derive the transaction hash from those bytes, then persist. |
| Submission transport | `SubmissionTransport` | Submit the exact persisted bytes and hash. A timeout or rejected response is not a final outcome. |
| Hash lookup | `HashLookup` | Query the original transaction hash and return found, not found, or unavailable. |
| Validated-ledger observation | `ValidatedLedgerObserver` | Report validated progress and a complete-history range used for non-inclusion proof. |
| Result verification | `ResultVerifier` / `PaymentResultVerifier` | Require validated evidence and verify hash, type, account, destination, tRLUSD identity, intended and delivered amount, fee, network, sequence or ticket, LastLedgerSequence, ledger index, and ledger hash. |
| Restart reconciliation | `Reconciler` / `PaymentService.reconcile_after_restart` | Reconcile every nonterminal journal entry by the original hash; never recreate an intent or generate a new payment. |
| Read-only reporting | `PaymentStatusReporter` / `ReadOnlyPaymentStatus` | Return state and submission attempts without exposing mutation methods. |

## Journal and idempotency

`PaymentJournal` owns two SQLite tables: one transaction row per Credence
operation and an append-only submission-attempt list. The transaction row
contains the operation and payment-authorization IDs, requested type, account,
destination, tRLUSD wire identity and issuer, integer micro-unit amount, fee,
network, Sequence or TicketSequence, history window, LastLedgerSequence, exact
signed bytes, derived hash, current state, provisional status, final result,
final evidence, validated ledger index, and one-time settlement-effect flag.

Uniqueness is enforced for operation ID, payment-authorization ID, transaction
hash, `(account, Sequence)`, and `(account, TicketSequence)`. Every write is a
short `BEGIN IMMEDIATE` transaction. No write transaction spans submission,
lookup, validated-ledger observation, or any provider call.

The database file is mode `0600`. It contains signed public transaction bytes,
not private signing material.

## State machine and finality

The normal lifecycle is:

`PREPARED -> SUBMITTED -> PENDING_CONFIRMATION -> VALIDATED_SUCCESS | VALIDATED_FAILURE | EXPIRED_NOT_INCLUDED`

`UNKNOWN` means reconciliation is required. It is used when a response is
lost, submission is rejected without final ledger evidence, lookup is
unavailable, or ledger history cannot prove non-inclusion.

- A timeout moves to `UNKNOWN`; reconciliation queries the original hash.
- A safe resubmission may occur only after hash lookup finds no transaction
  before expiry. It reuses the exact persisted signed bytes and hash.
- A found but unvalidated transaction remains `PENDING_CONFIRMATION`.
- A validated non-`tesSUCCESS` result is `VALIDATED_FAILURE`.
- A validated `tesSUCCESS` whose expected fields or delivered amount do not
  match is `VALIDATED_FAILURE` and never `XRPL_SETTLED`.
- Expiry requires validated-ledger progress beyond LastLedgerSequence and a
  complete history range covering the full possible inclusion window. Missing
  history produces `UNKNOWN`.
- Repeated or competing reconciliation is idempotent. Contradictory terminal
  evidence fails closed.

## Supported transaction types

- Supported in X0: direct issued-currency `Payment` of tRLUSD to a provider,
  with Sequence or TicketSequence.
- Represented at the interface but not constructed or submitted in X0:
  `LoanSet` and `LoanPay`.
- Unsupported in X0: Vault, Loan Broker, trust-line, signer-list, account-key,
  escrow, DEX, and all other XRPL transactions.

X0 uses only deterministic injected transport. A real rippled transport and
live transaction execution require a later authorized checkpoint.
