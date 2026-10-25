# 0009. Integration I1e: close authorization and accounting bypasses

- Status: Accepted for the integration branch, amended for I1g (§7–§10), I1i (§11), I1k (§12), I1m (§13) and I1o (§14). **Provisionally blocked for acceptance** until independent Core and Inference owner reviews approve the final I1g commit.
- Date: 2026-10-19
- Owner: Integration (San Antonio)
- Source: Maura's I1e authorization, and the I1d owner reviews of commit `cfc6224`:
  - Inference findings I-1 to I-5 (reproductions R1, R2 and R3);
  - Core findings C-1 to C-4.

  Requests: `contracts/requests/0003-core-attempt-enforcement.md`, `contracts/requests/0004-inference-authorized-delivery.md`. Amends decisions 0007 and 0008.

## Decisions

1. **Settlement requires an authorized delivery (C-1).**
   - `settle` requires Core's persisted delivery attempt for the reservation and request (`NO_DELIVERY_ATTEMPT`, `ATTEMPT_MISMATCH`). The check is inside the same transaction, before the verifier, so verifier precedence and atomicity are unchanged.
   - Late-delivery handling requires it too. An unauthorized late delivery is refused `NO_DELIVERY_ATTEMPT` and records nothing.
2. **Core authorizes each provider call (I-1).**
   - `claim_execution` checks a supplied `DeliveryAttempt` against Core's persisted record: it must exist, match exactly, be unclaimed and not unknown, and its reservation must be held and live. It records a one-shot claim.
   - `Gateway.deliver` requires Core's delivery authority, claims immediately before calling the provider, and fails closed (`DeliveryNotAuthorized`) with no authority or on any refusal.
   - A fabricated attempt calls no provider.
3. **No standalone path on accounted facilities (I-2, C-2).**
   - The ledger's public `reserve` and `reserve_in_transaction` refuse a registered facility (`USE_SETTLEMENT_COORDINATOR`), as `settle` and `release` already did.
   - The coordinator reserves through an internal, transaction-scoped path after computing signature facts itself. No public method takes a caller's assertion of authorization.
   - `Gateway.purchase` returns `ACCOUNTED_FACILITY` there, before any provider call.
   - `IntegrationService` no longer exposes its gateway, ledger or coordinator, only a read-only `AccountingView`.
4. **Response access is cryptographically authenticated (I-3, C-3).**
   - `retrieve` and `acknowledge` take an agent-signed `ResponseAuthorization`. The statement is domain-separated (`credence/integration/response-authorization/v1`, distinct from C3A's), serialized by Kessai and signed with Ed25519.
   - It covers protocol, version, operation, action, agent key, a 128-bit single-use nonce, and an expiry at most 300 s ahead.
   - It is verified against the current registry (agent role, current status, the purchase's signing key and subject).
   - Nonces are consumed atomically with the access, persisted in SQLite, across processes and restarts.
   - No canonical object or C3A signature definition changed.
5. **Documentation is corrected (I-4, C-4).**
   - The contracts state what is enforced, and through which interfaces.
   - Core's no-delivery release no longer claims that absence of an attempt proves no provider is owed. It means no supported path called a provider.
   - Decision 0007 carries explicit supersession notes.
   - The meanings of recorded attempt, claimed execution, provider execution, captured evidence, verified delivery, accounting settlement and XRPL settlement are set out in `contracts/SETTLEMENT-COORDINATOR.md`.
6. **Uncertainty stays fail-closed (I-5).** Outcomes after an attempt remain `unknown` even where the missing claim shows the supported gateway made no call. The release rule (no attempt at all) was deliberately not widened.

7. **Registration requires a history-free facility (I1g amendment, Core finding B-1, 2026-10-20).**
   - `register_facility` refuses `FACILITY_HAS_HISTORY` when the `facility_id` already appears in any reservation, used-nonce, settlement, chain, dispute, attempt, no-delivery-release or late-delivery table. The check and the insert are one `BEGIN IMMEDIATE` transaction.
   - Without this, history made through the unaccounted C2 path could be adopted on registration: a forged reservation's held amount (2,458) above a 1,000 limit, a consumed nonce, or a standalone provider execution with no Core attempt that a later `release_undelivered` would falsely classify.
   - A refusal writes nothing. No migration of past history is performed, and none is implied; adopting a used facility needs a separately authorized protocol.
   - A rejected policy decision alone is not history: decision records carry no facility identity and create no state.

8. **B-1 policy decisions (Maura, 2026-10-20).**
   - **B1-D1:** digest-only `decision_records` are not scanned; a facility whose only history is policy rejections may register (they have no accounting or authorization side effects). The exception is documented and tested.
   - **B1-D2:** no `execution_claimed_at` settlement precondition in I1g. It is recorded as deferred hardening.
9. **Durable, fail-closed key revocation (I1g, Inference finding F1).**
   - Revocations persist in `integration_key_revocations`: key, time, operator actor. They are authoritative over any registry supplied later, never expire, and survive re-registration.
   - The existing `KeyRegistry.revoke` workflow on the operator's registry is routed through persistence while the service is open. Pre-open revocations are persisted at open.
   - There is no new public revocation operation.
   - Unreadable or malformed state fails closed, and a persistence failure still refuses the key in-process.
   - Revocation commits are ordered before later access, by an in-transaction re-check, and before purchase admission and execution, by an in-process gate.
   - An admitted purchase not yet executing is released through Core's audited no-delivery release. One already executing settles; its response is not released to the revoked key.
   - C3A signature formats and `KeyRegistry` are unchanged.
10. **Atomic retrieval (I1g, Inference finding F2).**
   - Retrieval reads the exact response bytes in the same transaction that re-checks revocation and availability, consumes the nonce and records the retrieval. Acknowledgment is atomic in the same way.
   - A refusal consumes nothing, and the reachable `AssertionError` is gone.

11. **Revocation lifecycle closure (I1i, 2026-10-20; Inference I1h finding, Core and Inference nested-write finding).**
   - A revocation never appears successful unless committed.
   - After `close()`, the bound registry's `revoke` raises `RevocationNotPersisted` (the key is marked revoked in that object only) instead of silently reverting to Core's in-memory method. This survives repeated closes and is replaced, not stacked, by a later binding.
   - Trusted offline administrative revocation: `revoke_key(database, key_id, operator)`, plus a command-line form.
     - Trust: the calling OS user owns the existing owner-only database and holds its exclusive ownership lock.
     - It never creates a database, is idempotent and irreversible, reports success only after commit, and fails loudly.
     - `via` widens to `offline_admin`; I1g tables are upgraded in place.
   - `DurableKeyRegistry.lookup` performs no database I/O on any branch. An unexpected in-memory revocation is refused in-process, not persisted from inside Core's write transactions. This removes the 31 s nested busy-timeout stall.
   - Frozen Core `KeyRegistry`, C3A/C3B formats and Core settlement rules are unchanged.

12. **Database identity and ownership (I1k, 2026-10-24; Skopje I1j finding).**
   - **The defect.** A symlink alias derived a different `.owner` lock. The offline revocation therefore committed while a service ran (and the service ignored it), and a second service could open the same database.
   - **Identity procedure.** Ownership and offline revocation now share one identity procedure:
     - the canonical path: `resolve(strict=True)` for an existing file, or the strictly resolved parent plus the name for a fresh one;
     - a lock derived from the canonical path, opened with `O_NOFOLLOW`;
     - under the lock, the database opened with `O_NOFOLLOW` (fresh: `O_CREAT | O_EXCL`) and its device and inode checked against the pre-lock inspection;
     - exactly one hard link, and the caller's path still resolving to the canonical path.
   - **Refusals.** Mismatches are typed `DatabaseIdentityError` refusals: `DANGLING_SYMLINK`, `DATABASE_MISSING`, `PARENT_MISSING`, `NOT_A_REGULAR_FILE`, `HARD_LINKED`, `IDENTITY_CHANGED`, `ALIAS_CHANGED`, `LOCK_FILE_UNSAFE`.
   - **While running.** The verified file stays open as an anchor, re-verified at service checkpoints and inside the integration's write transactions. A mismatch poisons the service.
   - **CLI.** A refused offline revocation, including `OwnershipError`, prints `REFUSED: <code>` and exits 2.
   - **Unchanged.** No new lock service or external database. Core, Inference, policy, billing, verifier and canonical formats are unchanged.
   - **Residual.** Identity is checked at checkpoints, not bound to SQLite's own file descriptor; a local POSIX filesystem is assumed. **Corrected in §13:** checkpoints detect a replacement only while it is observable; a swap-and-restore between checks can evade them.

13. **Trusted database directory boundary (I1m, 2026-10-24; Skopje I1l finding).**
   - **The finding.** A precisely timed replace-and-restore between two identity checks evades checkpoint detection. I1k also accepted databases in group- or world-writable directories, where another principal could do this.
   - **The boundary.** `check_trusted_location` (replaced by `resolve_trusted` in §14) runs before ownership and offline revocation, and the database directory is re-checked at every `verify()`:
     - the database directory must be ours, with no group or other write bit (sticky or not) and no write-granting ACL;
     - every ancestor must be ours or root's, with no write-granting ACL; if writable, only sticky with an owned next component;
     - symlinks on the caller's spelling must be ours or root's, in trusted directories;
     - the database and lock files must be ours;
     - deny-only ACLs are accepted, unreadable ACLs refused; macOS and Linux only.
   - **The contract correction.** Identity checks detect a replacement only while it is observable. A swap-and-restore completed between checks can evade them, and the window is not eliminated. Supported security assumes untrusted principals cannot manipulate the database or lock directory. Same-authority code is outside the boundary. Checkpoints are not a pinned descriptor.
   - **Verification limit.** No second OS account was available: foreign ownership is simulated through a uid seam, and ACL cases use real macOS ACLs.
   - **Unchanged.** Core, Inference, XRPL, canonical formats and the I1k identity protections are all unchanged.

14. **Nested symlink and file ACL closure (I1o, 2026-10-24; Skopje I1n findings V1, V2).**
   - **V1.** The I1m check inspected only the caller's spelling and the final canonical chain, so `private/chain → shared/link → private/data/db.sqlite3`, with `shared` world-writable and not sticky, was accepted.
     - `resolve_trusted` now walks every component actually traversed, from the root, on every link hop. Every traversed directory is checked: owner, mode, sticky rule, ACLs.
     - Loops (`SYMLINK_LOOP`), dangling targets and links changed while read (`PATH_CHANGED`) are refused, and the walk must match the OS resolution. `Path.resolve()` is no longer trusted alone.
   - **V2.** A `0600` database or lock file with an ACL granting `everyone` write or delete was accepted. Both files are now checked through their descriptors, before either is changed: owner, one link, no group or other write bit, no modifying ACL entry (`FILE_ACL`, `FILE_PERMISSIONS`; the lock reports `LOCK_FILE_UNSAFE`).
     - Read-only and compatible deny-only ACLs are accepted. An owner-blocking deny fails closed.
     - The lock path must still name the locked file, after `flock` and at every checkpoint (`LOCK_IDENTITY_CHANGED`).
   - **Revalidation.** `verify()` re-runs the walk, the database directory rule, the file checks and the lock identity at every checkpoint. An ancestor made writable or given an ACL after startup poisons the service.
   - **Not closed, and stated.**
     - A same-user replace-and-restore between checks.
     - A same-user deletion and recreation of the lock entry, which lets a second process lock a new entry (the original owner fails closed). Pinning the lock to the database inode was tested and rejected: `flock` on the database blocks SQLite's locking on macOS.
     - No second OS account: permission inspection is verified with real modes and ACLs, but cross-user execution was not.
   - **Unchanged.** Core, Inference, XRPL and the frontend.

## Consequences

- No canonical object, enum value of a canonical member, C3A signature domain, policy rule, billing rule or verifier precedence changed.
- The deterministic vectors are unchanged. The C3B fixture builder now records each reservation's attempt before settling; that is not a transcript step, so the bytes are identical.
- Enforcement covers Credence's supported interfaces. A program calling a provider adapter or the SQLite file directly is outside them. Such a delivery cannot settle.
