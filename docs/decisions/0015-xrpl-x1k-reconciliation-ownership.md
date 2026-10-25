# 0015. XRPL X1k pre-expiry evidence and reconciliation ownership

- Status: Proposed. Awaiting Salvador's targeted X1k acceptance.
- Date: 2026-10-24
- Scope: XRPL payment reconciliation and journal concurrency only; no ledger writes.
- Base: `cdc67b37afc94a97fb607e1b79d142971528c247`.

## Decisions

1. **A rebroadcast needs fresh typed evidence.** Before a pre-expiry rebroadcast, the validated
   observation must reproduce the durable submission anchor's configured source ID, XRPL network
   ID, rippled build version and node public key. Its validated ledger cannot regress, and an
   observation at the anchor index must reproduce the anchor hash. An incomplete or contradictory
   observation leaves the payment `UNKNOWN`.
2. **Sender state must still admit the original transaction.** A Sequence payment requires the
   recorded sender Sequence. A TicketSequence payment requires that exact ticket to remain
   available. This check does not prove non-inclusion; it only prevents contradictory sender state
   from authorizing a rebroadcast.
3. **Absence is never affirmative rebroadcast evidence.** An inconclusive `NOT_FOUND` can preserve
   liveness only after the source and sender checks succeed. It does not authorize a replacement,
   new signature, changed amount or changed destination. The only eligible payload is the exact
   signed transaction already persisted under its derived hash.
4. **Submission ownership is a durable SQLite lease.** Journal schema version 4 stores a random
   owner token and expiry on every attempt. `BEGIN IMMEDIATE` makes the claim atomic across threads
   and processes sharing the journal. A live lease makes all other workers observe-only, and only
   the matching owner token can complete that attempt.
5. **Abandoned attempts recover without replacement payments.** A lease expires after 30 seconds.
   The next writer atomically records the abandoned attempt as outcome-unknown before it may claim
   another attempt. A stale owner cannot complete the abandoned attempt. Recovery still submits
   only the original persisted bytes and hash; identical XRPL transaction resubmission is the
   economic idempotency boundary.
6. **Old journals are not silently rewritten.** Complete version-3 journals are preserved and
   refused with `JOURNAL_VERSION_3_UNSUPPORTED`. Initializing an empty database creates version 4.
   Migration and recovery of earlier journals require separate authorization.

## Assurance boundary

The configured rippled source checks its expected network, build and node public key before it
constructs an observation. The persisted identity lets reconciliation detect source drift after a
restart. As documented in decision 0014, a self-reported node key over unauthenticated loopback
HTTP is endpoint consistency evidence, not cryptographic server authentication or independent
consensus verification.

A time-bounded lease cannot prove that an operating-system-paused former worker has ceased at the
network boundary. If it resumes after its lease expires, its completion is fenced out; a recovered
worker can only submit the same XRPL transaction bytes and hash. Production deployment must keep
the transport's maximum request time below the lease bound or add lease renewal before changing
that bound.
