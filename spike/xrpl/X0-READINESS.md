# Checkpoint X0/X0s/X0t — offline payment and settlement readiness

## Outcome

X0 implements, X0s remediates and X0t places a fail-closed legacy-journal
boundary around an isolated, durable XRPL Payment submission and reconciliation
adapter. It made no live XRPL calls, submitted no live transactions, moved no
funds, created no loans, and changed no Core accounting semantics.

Baseline: `a3ec0a31f262e325061761e72c3b11246e2a400d`.

This is pre-hackathon preparation. It is not itself the new XRPL feature for
the October 24–25 event.

## Read-only Addis-Ababa review

The source was the unchanged Addis-Ababa head
`7fdb768d1461a8620041c19891b78ab391b032d3`, especially
`spike/xrpl/REPORT.md`, `RESULTS.json`, `ENVIRONMENT.md`, and the committed
evidence directories.

Reusable findings, preserved without reclassification:

- The tested environment was standalone rippled 3.4.0, source commit
  `4a4fded2eba11427c48ce3f24d9c1aea5e7a9d17`, network ID 21337. Its `feature`
  response and validated Amendments ledger object agreed on all eight required
  amendments.
- E3/E4 were PASS/RETIRED within their tested Vault and Loan Broker scope. E5
  was PASS for the tested borrower authorization scope.
- Historical E6 remains FAIL. Separately, its LoanSet authorization
  architecture was adjudicated VALIDATED FOR PRODUCT INTEGRATION within the
  tested scope: ResearchBot plus Credence 2-of-2 borrower authorization;
  either alone is insufficient; Loan Broker owner counterparty authorization
  is independently required; invalid or altered authorization fails; and a
  wrong-phase LoanSet is rejected without protected lending-state mutation.
- The historical positive LoanSet validated `tesSUCCESS`. Its frozen timing
  oracle was defective; the observed timing was adjudicated source-conformant.
- The E6 N6 run used `fail_hard=true`. In the pinned source that maps to
  `TapFailHard`; its submission-level `tecTOO_SOON`, `validated=false`,
  unchanged sequence and fee, and no inclusion are source-conformant for that
  submission mode. The historical N6 disposition oracle remains FAIL because
  it did not bind its expected effects to submission mode. X0 does not use
  that result as proof of ordinary non-fail-hard disposition.
- LoanPay was not submitted. E7–E9 were NOT_ATTEMPTED. No repayment behavior,
  LoanPay amount rule, or repayment finality claim is imported into X0.
- Earlier false stops came from local harness assumptions, including the
  `feature` response path, the Amendments fixed-object selector, object-count
  expectations, result handling, and evidence serialization. X0 treats an
  unexpected ledger result as evidence to record and a branch to stop, not a
  reason to switch builds or reinterpret the protocol.

## Implemented adapter

The contract is `contracts/XRPL-ADAPTER-CONTRACT.md`. The implementation is in
`credence/xrpl_adapter/`:

- `OfflinePaymentPreparer` validates the recorded payment identifiers and
  security/economic fields. An identifier is not proof of Core authorization.
- `XrplSignedPaymentInspector` decodes the exact Payment with xrpl-py, rejects
  contradictory signing structures, and compares recorded fields before
  persistence. It does not cryptographically verify signatures, signer identity
  or SignerList quorum.
- `PaymentJournal` stores exact signed bytes, the XRPL hash derived from those
  bytes, account Sequence or TicketSequence, a trusted pre-broadcast ledger
  anchor, LastLedgerSequence, attempts, raw and verified final evidence, a
  durable settlement-event outbox, and the legacy one-time settlement bit.
- `PaymentService` implements submission, timeout handling, hash lookup,
  anchored complete-history expiry, exact-byte resubmission, restart
  reconciliation, structured evidence verification, and restart-safe outbox
  delivery to an idempotent consumer.
- `ReadOnlyPaymentStatus` exposes status and attempts without journal mutation.
- `DeterministicMockTransport` supplies offline submission, lookup, and ledger
  observations.

## Legacy journal boundary

X0t removes the automatic X0-to-X0s migration. `PaymentJournal` inspects an
existing database read-only before any schema change. An original X0 schema is
refused with `LEGACY_JOURNAL_UNSUPPORTED`, even when it has no payment rows.
Every row, signed transaction, hash, state, evidence, settlement status and
`settlement_effect_claimed` value remains unchanged. No outbox event, submission,
resubmission, reconciliation, replacement database or import is attempted.

A missing or genuinely empty database initializes directly at schema version 2,
and a complete version-2 journal reopens normally. Unknown versions and
ambiguous, corrupt or partially migrated schemas fail closed with
`UNSUPPORTED_JOURNAL_SCHEMA`. A legacy journal requires a separately approved
recovery or migration procedure before its payments can be reconciled; X0t
retains its evidence and does not invent an outcome. X0s payment-reliability
guarantees apply only to correctly initialized version-2 journals.

## Supported and unsupported types

Supported: direct issued-currency tRLUSD provider `Payment`, using either
Sequence or TicketSequence.

Not constructed or submitted: `LoanSet` and `LoanPay`. Their names are present
at the integration seam so future work cannot silently pass a loan action
through the Payment implementation.

Unsupported: every other XRPL transaction type. X0s explicitly rejects live
transport and does not provide a rippled transport, key custody, cryptographic
signing or verification, SignerList quorum validation, Core authorization
binding, Vault lifecycle, lending lifecycle, or repayment scheduler.

## Amendment dependencies

The offline Payment state machine has no lending-amendment dependency. The
historical local lending environment required these eight amendments, all
observed supported and enabled in the tested pinned build:

- `MPTokensV1`
- `SingleAssetVault`
- `LendingProtocol`
- `LendingProtocolV1_1`
- `fixCleanup3_1_3`
- `fixCleanup3_2_0`
- `fixCleanup3_3_0`
- `fixCleanup3_4_0`

Any later LoanSet or LoanPay checkpoint must revalidate the exact target
server, amendments, network ID, and complete ledger-history behavior before
use. X0 makes no claim about another server or network.

## Security and accounting dependencies

- The signer boundary supplies exact Payment bytes. X0s checks their structure
  and recorded fields but does not establish cryptographic authorization. The
  adapter holds no private signing material. Fixtures using `TxnSignature="AA"`
  are explicitly noncryptographic mock data.
- Payment execution authorization must remain separate from procurement
  authorization.
- Core remains authoritative for inference verification, reservations, and
  accounting settlement. X0 neither imports IntegrationService internals nor
  changes settlement meaning.
- Verified success atomically creates a deterministic settlement outbox event.
  Delivery is retried after restart and the destination must apply `event_id`
  idempotently. The legacy atomic local claim does not by itself guarantee a
  downstream effect across databases.
- Two settlement-effect authority paths still exist. Direct journal methods are
  not an acceptable live settlement authority, and a non-idempotent consumer
  can apply an at-least-once event twice. X1 must establish one authority path
  and durable consumer-side `event_id` idempotency.
- A later real transport must preserve a versioned evidence envelope containing
  the exact transaction blob, metadata result, delivered amount and ledger
  identity. X0s parses and cross-checks that evidence rather than trusting its
  normalized fields. The configured ledger source remains a trust boundary,
  not cryptographic consensus proof.
- A real node used for expiry must expose complete ledger-history coverage for
  the signed transaction's possible inclusion window. A higher validated
  ledger alone is insufficient.
- The X0s `offline_only` transport marker is self-declared and is not a live
  security boundary. X1 must enforce the transport boundary.
- Structural signing inspection is not cryptographic authorization. A validated
  `tesSUCCESS` with an unexpected delivery may have transferred funds even
  though it is denied the expected settlement effect; X1 must distinguish that
  exception from an ordinary unsuccessful ledger transaction.

## Offline test evidence

`tests/unit/test_xrpl_payment_readiness.py` covers:

1. successful validated Payment;
2. submission timeout followed by validated success;
3. crash after broadcast but before response;
4. duplicate submission of identical signed bytes and hash;
5. final validated transaction failure;
6. expiry with complete validated-history evidence;
7. missing ledger history;
8. wrong destination, asset, and delivered amount;
9. repeated reconciliation after restart;
10. competing reconciliation workers;
11. recorded hash inconsistent with signed bytes; and
12. failed submission with outcome still unknown.

Additional X0s tests cover contradictory and missing evidence with later
recovery, forged history starts, continuous range coverage, source disagreement,
sequence and ticket consumption, restart expiry, each outbox crash/replay
window, structural signing contradictions, XRPL hash equivalence, journal
uniqueness, owner-only database mode, and the absence of a SQLite write lock
during transport submission.

X0t tests use databases generated by the unmodified X0 code at
`aa236e1fbe1db88038673c08826f62248d738e50`. They cover claimed success,
validated failure, expiry, pending state, exact signed identities and evidence;
empty X0 journals; fresh version-2 initialization; version-2 reopen; unknown
versions; partial migration; and repeated refusal. Each refusal is checked for
byte-for-byte and logical-content preservation, including the prior settlement
claim and absence of a synthesized outbox event.

## Smallest viable live hackathon feature

The smallest new live feature is one provider payment on the pinned local XRPL
ledger after an existing verified inference delivery and Core accounting
settlement:

1. ResearchBot and the Credence policy signer authorize one direct tRLUSD
   Payment to the expected provider.
2. X1 cryptographically verifies the two expected signers, validated SignerList
   quorum, immutable Core settlement and authorized unsigned-transaction digest.
3. The journal persists the exact signed bytes before a separately authorized
   real transport submits them.
4. Reconciliation proves the final result by the original hash and validates
   destination, asset, amount, delivered amount, account, fee, sequence, and
   ledger evidence.
5. The integration idempotently consumes the durable `XRPL_SETTLED` outbox event.

That feature should be a separately authorized hackathon checkpoint. LoanPay
and new on-ledger lending behavior should remain outside its scope until their
own validated experiments exist.
