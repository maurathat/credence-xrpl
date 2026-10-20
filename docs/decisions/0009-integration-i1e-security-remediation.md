# 0009. Integration I1e: close authorization and accounting bypasses

- Status: Accepted for the integration branch. **Provisionally blocked for acceptance** until independent Core and Inference owner reviews approve the remediation commit.
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

## Consequences

- No canonical object, enum value of a canonical member, C3A signature domain, policy rule, billing rule or verifier precedence changed.
- The deterministic vectors are unchanged. The C3B fixture builder now records each reservation's attempt before settling; that is not a transcript step, so the bytes are identical.
- Enforcement covers Credence's supported interfaces. A program calling a provider adapter or the SQLite file directly is outside them. Such a delivery cannot settle.
