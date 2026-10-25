# XRPL-ADAPTER-CONTRACT

Owner: XRPL Integration (Codex)

Status: X0t offline legacy-journal boundary, amended by X1a (offline preflight) and X1c
(payment-assurance remediation; journal schema version 3). Only the owner edits this file;
other workstreams file a request in `contracts/requests/`.

## Boundary

The adapter records four independent facts in a correctly initialized X0s
journal. None implies another:

1. inference delivery was verified;
2. Core accounting settlement completed;
3. an exact signed XRPL transaction was submitted; and
4. that transaction reached a validated final outcome.

X0s does not change Core accounting semantics. Its downstream seam is a durable
transactional outbox event created in the same SQLite transaction that records a
Payment as validated with `tesSUCCESS` and verifies trusted-source evidence
against the recorded intent. Since X1c that outbox event, applied by an idempotent
consumer keyed by `event_id`, is the only supported settlement-effect path; the
legacy one-time local claim is refused (`LEGACY_SETTLEMENT_CLAIM_RETIRED`) whenever
an outbox event exists. A provisional submit response, including `tesSUCCESS`,
never creates `XRPL_SETTLED` or an outbox event.

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
idempotently. The local claim bit is not a settlement authority (X1c); its
stored value is kept as history and never rewritten.

Schema version 3 (X1c) is version 2 (anchor, verified-evidence and outbox
storage) with a state constraint that also admits `VALIDATED_UNEXPECTED_DELIVERY`.
No journal is migrated automatically. Before any schema write, `PaymentJournal`
distinguishes a genuinely empty database, the complete X0 signature, the complete
version-2 signature, and the complete version-3 signature whose state constraint
admits every state. An X0 journal, including one with no payment rows, is
preserved and refused with `LEGACY_JOURNAL_UNSUPPORTED`. A version-2 journal,
including one with no payment rows, is preserved byte for byte and refused with
`JOURNAL_VERSION_2_UNSUPPORTED`. Unknown versions and ambiguous, corrupt, partly
migrated or relabelled schemas are preserved and refused with
`UNSUPPORTED_JOURNAL_SCHEMA`. Only a missing or genuinely empty database is
initialized, directly at version 3.

Legacy recovery or migration (X0 or version 2) requires separate authorization
and is not part of X0t or X1c. Initialization never clears a legacy settlement claim, reopens a terminal
payment, synthesizes an outbox event, submits a transaction or imports rows into
another database. Until an approved recovery procedure exists, the legacy
journal and its evidence remain the historical record. The payment-reliability
guarantees in this contract apply only to correctly initialized version-3
journals.

Uniqueness is enforced for operation ID, payment-authorization ID, transaction
hash, `(account, Sequence)`, and `(account, TicketSequence)`. Every write is a
short `BEGIN IMMEDIATE` transaction. No write transaction spans submission,
lookup, validated-ledger observation, or any provider call.

The database file is mode `0600`. It contains signed public transaction bytes,
not private signing material.

## State machine and finality

The normal lifecycle is:

`PREPARED -> SUBMITTED -> PENDING_CONFIRMATION -> VALIDATED_SUCCESS | VALIDATED_FAILURE | VALIDATED_UNEXPECTED_DELIVERY | EXPIRED_NOT_INCLUDED`

`UNKNOWN` means reconciliation is required. It is used when a response is
lost, submission is rejected without final ledger evidence, lookup is
unavailable, or ledger history cannot prove non-inclusion.

- A timeout moves to `UNKNOWN`; reconciliation queries the original hash.
- A safe resubmission may occur only after hash lookup finds no transaction
  before expiry. It reuses the exact persisted signed bytes and hash.
- A found but unvalidated transaction remains `PENDING_CONFIRMATION`.
- A validated non-`tesSUCCESS` result is `VALIDATED_FAILURE`.
- Valid evidence for `tesSUCCESS` with the wrong delivered asset or amount is
  `VALIDATED_UNEXPECTED_DELIVERY` (X1c): the ledger transaction succeeded and
  funds may have moved, but the authorized obligation was not met. It is never a
  ledger failure and never `XRPL_SETTLED`, creates no outbox event, keeps the raw
  evidence and the verified delivered amount, is terminal (never retried or
  replaced automatically), and is reported by
  `ReadOnlyPaymentStatus.requires_operator_reconciliation` and
  `operator_reconciliation_queue`. A contradiction between the evidence
  transaction and normalized or recorded fields remains `UNKNOWN`.
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

X1 must also replace the self-declared `offline_only` marker with an enforceable
transport boundary. X1c retired the direct journal claim as a settlement authority
and defined one settlement-effect path (the outbox). Outbox delivery is
at-least-once: a non-idempotent consumer can apply an event twice, so the live
consumer must enforce `event_id` idempotency in its own durable transaction.

## X1a additions (offline preflight)

X1a adds offline-tested modules beside the unchanged X0t journal and service. It authorizes no
live submission, and `PaymentService` still accepts only `offline_only` submission transports.
Details, refusal codes and remaining dependencies are in `spike/xrpl/X1A-PREFLIGHT.md`; the
decision is `docs/decisions/0010-xrpl-x1a-multisign-preflight.md`.

| Interface | X1a implementation | Contract |
| --- | --- | --- |
| Multisignature | `credence.xrpl_adapter.multisign` | Build the unsigned Payment and its `xrpl-stx-sha512half-v1` digest; assemble canonical `Signers`; verify both master-key signatures, the validated SignerList (ResearchBot 1, policy signer 1, quorum 2), no RegularKey, the configured master-key state, the multisign fee, Sequence or TicketSequence, and LastLedgerSequence. |
| Core payment authorization | `credence.signers.payment_authorization` | An Ed25519, domain-separated `PaymentAuthorization` statement binding operation, accounting settlement, receipt and chain head, amount, asset, destination, unsigned digest, expiry and network. Its `authorization_id` is the journal's `payment_authorization_id`. |
| Policy signer | `credence.signers.policy_signer.PolicySigner` | Signs only after independently re-checking the statement against Core's accounting settlement and the validated ledger; one transaction per receipt and per operation, durably. |
| Trusted transport | `credence.xrpl_adapter.rippled.RippledTransport` | Loopback-only JSON-RPC (API v2) to the pinned local rippled; all reads pinned to one validated ledger hash; implements `HashLookup` (X0s evidence envelope with raw responses) and `ValidatedLedgerObserver`. `submit` refuses unless explicitly enabled; `offline_only` is false. |
| Outcome classification | `credence.xrpl_adapter.settlement.classify_payment` | Seven outcomes from one journal record, including `VALIDATED_UNEXPECTED_DELIVERY` decided from stored evidence. |
| Settlement consumer | `credence.xrpl_adapter.settlement.SqliteSettlementEventConsumer` | Applies an outbox event once per `event_id` in the destination's own transaction, together with its effect. This is the single live settlement-effect authority path; `claim_xrpl_settlement` is not. |
| tRLUSD setup | `credence.xrpl_adapter.trlusd_setup` | Unsigned setup steps, reserve arithmetic and read-only readiness findings. Nothing is submitted. |

The X1a transport response shapes were exercised only against a deterministic fake node, not the
pinned server.

## X1c remediation

X1c closes reviewer findings B1-B5 and C1-C3 against `607cc2a`. Details and
before/after exploit results are in `spike/xrpl/X1C-REMEDIATION.md`; the decision
is `docs/decisions/0011-xrpl-x1c-payment-assurance.md`.

| Finding | Contract now |
| --- | --- |
| B1 | Lookup takes the delivered amount from binary metadata (`DeliveredAmount`, or the signed `Amount` for a successful Payment without `tfPartialPayment`) and requires the JSON `delivered_amount` to match in currency, issuer and exact value; otherwise `EVIDENCE_INCONSISTENT`, nothing settles. Both observations are kept in the evidence envelope. |
| B2 | Every lookup re-reads and re-verifies the node's validated-ledger identity (network ID, pinned build, optional pinned node key). The transaction's ledger must be validated, at or below the validated ledger, inside complete history, and hash-consistent. `expected_build_version` is required. |
| B3 | The policy signer reads ledger state through its own configured trusted source; a caller's context is only compared (`LEDGER_CONTEXT_MISMATCH`) and never used as evidence. |
| B4 | A funded signer account holding a RegularKey is refused (`SIGNER_REGULAR_KEY_PRESENT`) and is a setup-readiness finding. |
| B5 | `claim_xrpl_settlement` is refused (`LEGACY_SETTLEMENT_CLAIM_RETIRED`) whenever an outbox event exists. The outbox and idempotent consumer are the one authoritative payment-event path. |
| C1 | `VALIDATED_UNEXPECTED_DELIVERY` state; journal schema version 3; version-2 journals refused unchanged. |
| C2 | Signatures must be canonical as rippled requires: strict DER with low S (secp256k1), `S < L` (Ed25519). `SIGNATURE_NOT_CANONICAL`. |
| C3 | The loopback client never follows an HTTP redirect. |

## X1q addition (QuickNode, read-only)

X1q adds an optional, read-only QuickNode XRPL Testnet transport beside the unchanged modules
above. Its contract is `contracts/QUICKNODE-ADAPTER-CONTRACT.md` (delegated to Da Nang for X1q).
It does not change any interface in this file. Its only binding to the X0t seams answers every
lookup `UNAVAILABLE` with a `QUICKNODE_*` detail, so hosted evidence never finalizes a journal
entry. Public-network reconciliation is requested in
`contracts/requests/0010-xrpl-public-network-reconciliation.md`.
