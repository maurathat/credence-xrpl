# XRPL-ADAPTER-CONTRACT

Owner: XRPL Integration (Codex)

Status: X0s offline payment-integrity remediation. Only the owner edits this file;
other workstreams file a request in `contracts/requests/`.

## Boundary

The adapter records four independent facts. None implies another:

1. inference delivery was verified;
2. Core accounting settlement completed;
3. an exact signed XRPL transaction was submitted; and
4. that transaction reached a validated final outcome.

X0s does not change Core accounting semantics. Its downstream seam is a durable
transactional outbox event created in the same SQLite transaction that records a
Payment as validated with `tesSUCCESS` and verifies trusted-source evidence
against the recorded intent. The legacy one-time local claim remains available,
but is not a delivery guarantee. A provisional submit response, including
`tesSUCCESS`, never creates `XRPL_SETTLED` or an outbox event.

`operation_id` and `payment_authorization_id` are journal identifiers. They are
not proof that Core authorized a payment. X1 must bind an immutable Core
settlement and approved unsigned-transaction digest before live submission.

## Public interfaces

| Interface | X0 implementation | Contract |
| --- | --- | --- |
| Transaction preparation | `TransactionPreparer` / `OfflinePaymentPreparer` | Validate operation and authorization identifiers, account, destination, tRLUSD wire identity, integer micro-unit amount, fee ceiling, network, Sequence or TicketSequence, and LastLedgerSequence. Identifier validation is not Core authorization verification. |
| Signed persistence | `SignedTransactionInspector` / `PaymentJournal.persist_signed` | Decode the actual Payment bytes with xrpl-py, reject contradictory signing structures, match every recorded field, derive the transaction hash, then persist. This is structural inspection, not cryptographic signature, signer-identity or quorum verification. |
| Submission transport | `SubmissionTransport` | X0s permits only explicitly offline transports. Submit the exact persisted bytes and hash. A timeout or rejected response is not a final outcome. |
| Hash lookup | `HashLookup` | Query the original transaction hash and return found, not found, or unavailable. |
| Validated-ledger observation | `ValidatedLedgerObserver` | Before first broadcast, persist a trusted-source ledger index, hash, source identity and sender state. Later report explicit continuous history ranges and current sender state for non-inclusion proof. |
| Result verification | `ResultVerifier` / `PaymentResultVerifier` | Parse the versioned evidence envelope, derive the hash and Payment fields from its exact transaction blob, verify metadata result and delivered amount, then strictly cross-check normalized fields. Missing, malformed or contradictory evidence remains unresolved. The trusted source is not independent cryptographic consensus proof. |
| Restart reconciliation | `Reconciler` / `PaymentService.reconcile_after_restart` | Reconcile every nonterminal journal entry by the original hash; never recreate an intent or generate a new payment. |
| Read-only reporting | `PaymentStatusReporter` / `ReadOnlyPaymentStatus` | Return state and submission attempts without exposing mutation methods. |
| Settlement event delivery | `SettlementEventConsumer` / `PaymentService.deliver_settlement_events` | Retry a deterministic outbox event until an idempotent consumer durably applies it. Mark delivery only after the consumer returns. |

The X0s evidence envelope is JSON with schema
`credence.xrpl.validated-payment-evidence.v1`. It preserves the trusted source's
validated flag, ledger index and hash, transaction hash, exact signed transaction
hex, and metadata `TransactionResult` plus issued-currency `delivered_amount`.
The verifier derives transaction identity and fields from that blob and stores
the unmodified envelope alongside verified ledger and delivered-amount columns.

## Journal and idempotency

`PaymentJournal` owns three SQLite tables: one transaction row per Credence
operation, an append-only submission-attempt list and a settlement outbox. The transaction row
contains the operation and payment-authorization IDs, requested type, account,
destination, tRLUSD wire identity and issuer, integer micro-unit amount, fee,
network, Sequence or TicketSequence, caller history hint, trusted submission
anchor, LastLedgerSequence, exact signed bytes, derived hash, current state,
provisional status, raw final evidence, verified ledger identity and delivered
amount, evidence version, and the legacy one-time settlement-effect flag.

The outbox contains a deterministic event ID bound to operation ID and
transaction hash, canonical payload, attempt count and delivery timestamp. The
event is inserted atomically with verified success. Exactly-once economic effect
across a second database requires the destination to apply `event_id`
idempotently; the local claim bit alone is insufficient.

Schema version 2 adds anchor, verified-evidence and outbox storage. Terminal X0
rows without evidence verification version 1 are reopened as `UNKNOWN` with
`X0S_REVERIFICATION_REQUIRED`; their retained evidence must be reconciled again
before settlement status or an outbox event can exist.

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
- Valid evidence for `tesSUCCESS` with the wrong delivered asset or amount is
  `VALIDATED_FAILURE` and never `XRPL_SETTLED`. A contradiction between the
  evidence transaction and normalized or recorded fields remains `UNKNOWN`.
- Expiry requires validated-ledger progress beyond LastLedgerSequence and a
  trusted pre-broadcast anchor plus continuous history ranges covering the full
  possible inclusion window. Missing provenance, gaps, source disagreement or
  sequence/ticket consumption produces `UNKNOWN`. The caller history hint is
  never authoritative.
- Missing, malformed or contradictory final evidence produces `UNKNOWN` and can
  be resolved by a later adequate observation; verification failure alone is
  not a terminal ledger failure.
- Repeated or competing reconciliation is idempotent. Contradictory terminal
  evidence fails closed.

## Supported transaction types

- Supported in X0: direct issued-currency `Payment` of tRLUSD to a provider,
  with Sequence or TicketSequence.
- Represented at the interface but not constructed or submitted in X0:
  `LoanSet` and `LoanPay`.
- Unsupported in X0: Vault, Loan Broker, trust-line, signer-list, account-key,
  escrow, DEX, and all other XRPL transactions.

X0s enforces deterministic offline transport. Cryptographic signature
verification, exact signer identities, validated SignerList weights/quorum,
Core authorization binding, custody, a real rippled transport and live
transaction execution require the separately authorized X1 checkpoint. Test
fixtures using `TxnSignature="AA"` are deliberately noncryptographic mock data
and are not evidence of valid XRPL authorization.
