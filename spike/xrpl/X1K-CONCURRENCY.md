# Checkpoint X1k — pre-expiry evidence and reconciliation concurrency

## Outcome

X1k corrects Salvador X1j findings F1 and F2 inside XRPL-owned payment reconciliation and journal
code. No transaction was signed or submitted to an XRPL network, no ledger was written, and no
production payment authorization boundary was changed.

Base: `cdc67b37afc94a97fb607e1b79d142971528c247`.

## Independent reproductions at the base

- F1: after a response-loss timeout at ledger 101, reconciliations at ledger 105 rebroadcast the
  transaction even when the sender Sequence, network ID, configured source ID, node public key or
  build version changed, and even when the validated ledger regressed. Every case increased the
  transport call count from one to two.
- F2: two workers using the same journal could both pass the pending-state check before either
  submitted. Both called the transport with the same bytes and hash. One later overwrote the
  other's open attempt as `TRANSPORT_ERROR`, and the owner then received `JournalConflict` while
  completing it.

## Correction and adversarial results

- The submission anchor now durably records source ID, network ID, build version and node public
  key. A fresh pre-expiry observation must match all four, must not regress the validated ledger,
  and must reproduce the anchor hash when observed at the anchor index.
- Sequence and TicketSequence state is rechecked before the resubmission branch. Changed Sequence
  or a consumed ticket leaves the payment `UNKNOWN` and produces no transport call.
- Missing anchor identity and changed source, network, build, node or ledger identity each leave
  the payment unresolved. A bare or inconclusive `NOT_FOUND` never bypasses these checks.
- Schema version 4 gives each submission attempt an owner token and 30-second expiry. The atomic
  claim occurs before the transport boundary. Concurrent workers using distinct `PaymentJournal`
  instances observe the active `SUBMITTED` state; exactly one reaches the transport, and neither
  worker rewrites the other's attempt. A separate-process claim race likewise produces one lease
  owner and one observe-only result.
- After simulated process death, the live lease initially blocks another call. Once expired, one
  worker records the abandoned outcome as unknown and reacquires ownership. Both attempted
  submissions use exactly the original signed bytes and transaction hash. The stale owner is
  unable to complete the old attempt.

## Journal compatibility

Version 4 adds three durable submission-anchor identity columns and two attempt-lease columns.
Complete version-3 databases, including databases with no payment rows, are detected read-only,
preserved byte-for-byte and refused with `JOURNAL_VERSION_3_UNSUPPORTED`. Version 1 and version 2
refusal behavior remains unchanged. There is no automatic migration.

## Remaining limitations

- Node identity pinning remains an unauthenticated endpoint consistency check, not authenticated
  server identity or independent XRPL consensus verification.
- Expiry still requires literal bounded `searched_all=true` plus stable identity and complete
  history. An ordinary `txnNotFound` remains unresolved.
- Lease expiry provides crash recovery, not proof that a paused process cannot resume its network
  call. Its eventual journal completion is fenced; economic safety relies on resubmitting only the
  identical signed XRPL transaction. Production transport timeouts must remain below the lease.
- N3, N4, N5, trusted Core reservation authority, production custody, live signing and live
  submission remain outside X1k and disabled.
