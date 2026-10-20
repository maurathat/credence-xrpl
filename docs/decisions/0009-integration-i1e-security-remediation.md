# 0009. Integration I1e: close authorization and accounting bypasses

- Status: Accepted for the integration branch, amended for I1g (§7–§10). **Provisionally blocked for acceptance** until independent Core and Inference owner reviews approve the final I1g commit.
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

## Consequences

- No canonical object, enum value of a canonical member, C3A signature domain, policy rule, billing rule or verifier precedence changed.
- The deterministic vectors are unchanged. The C3B fixture builder now records each reservation's attempt before settling; that is not a transcript step, so the bytes are identical.
- Enforcement covers Credence's supported interfaces. A program calling a provider adapter or the SQLite file directly is outside them. Such a delivery cannot settle.
