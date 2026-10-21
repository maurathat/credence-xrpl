# INTEGRATION-FLOW

Owner: Integration (San Antonio, Claude Code). Status: Integration I1c, remediated in I1e, I1g and I1i. Only the owner edits this file. Other workstreams file a request in `contracts/requests/`.

This contract describes `credence/integration/` as implemented. The code is authoritative. `tests/integration/test_i1_contract.py` checks the lists below against the code. The decisions are `docs/decisions/0008-integration-i1c-inference-accounting.md` and `docs/decisions/0009-integration-i1e-security-remediation.md`. What a recorded attempt, a claimed execution, provider execution, captured evidence, verified delivery, accounting settlement and XRPL settlement each mean is set out in `contracts/SETTLEMENT-COORDINATOR.md`, "What each record means".

It connects Inference delivery (`contracts/INFERENCE-GATEWAY-CONTRACT.md`, `Gateway.deliver`) to Core accounting settlement (`contracts/SETTLEMENT-COORDINATOR.md`).

**Accounting settlement is not XRPL settlement.** No XRPL transaction is built, signed or submitted here. The drawn amount is Credence's record of verified spending, not confirmed loan principal. Amounts are integer micro-tRLUSD (tRLUSD is a test token).

## Ownership boundaries

| Area | Owns |
| --- | --- |
| Core (`credence/core/`) | Reservation, policy, delivery attempts, the verifier, accounting settlement, the persisted receipt chain, disputes, no-delivery release, late-delivery evidence |
| Inference (`credence/inference/`) | Provider calls, the receipt and signed evidence (`Gateway.deliver`), market qualification and routing |
| Integration (`credence/integration/`) | Process ownership, per-facility serialization, the operation journal, response retention and release, response authorization, recovery, and the API used by the agent |

**The API surface is narrow.** `IntegrationService` has no purchase-capable gateway, no ledger and no writable coordinator. Reads go through `accounting` (`AccountingView`), which exposes only exposure, chain head, settlement, dispute, evidence, attempt, release and reservation lookups.

## Lifecycle

`IntegrationService.purchase(job, signer)`, for one job:

1. **Ownership.** The service owns the database (`ProcessOwnership`, an OS `flock` on `<database>.owner`). A second owner, in any process, is refused (`OwnershipError`).
2. **Serialization.** The facility's in-process lock is held from this step until the operation is settled or terminally reconciled. There is one in-flight purchase per facility; a facility with an unreconciled journal entry refuses new purchases (`FACILITY_BUSY`).
3. **Registration.** The facility must be registered in Core (otherwise `UNREGISTERED_FACILITY`).
4. **Accounting.** Core's authoritative exposure is read. The presented limit must equal the stored limit (otherwise `FACILITY_LIMIT_MISMATCH`), and drawn is taken from Core.
5. **Signing.** One purchase request per offer is built against that accounting and signed by the agent's signer. The service never holds the agent's key.
6. **Qualification.** Core qualifies every offer (`Gateway.select`), and the cheapest qualifying offer is chosen. If none qualifies, the operation ends as `no_qualifying_offer`.
7. **Intent, then reservation.**
   - The chosen signed purchase is journaled as `intent`, with its deterministic reservation ID.
   - Core then reserves (`SettlementCoordinator.reserve`).
   - A policy rejection ends the operation as `policy_rejected`.
8. **Reserved.** The journal records `reserved`.
9. **Chain head.** Core's persisted chain head is read. No other process can move it, because of steps 1 and 2.
10. **Durable intent before chargeable execution.** In order:
    - the journal records `executing`;
    - Core records the delivery attempt (`begin_delivery`);
    - the gateway (`Gateway.deliver`, holding Core's delivery authority) claims that persisted attempt (`claim_execution`), one claim per attempt;
    - only then is the provider called.

    If the chosen provider is not connected, there is no attempt: Core releases the hold with audit (`released_undelivered`). No SQLite transaction is open during the provider call. A refused claim calls no provider. An expired claim ends as `expired`; any other refusal is left `executing` and reconciled as `unknown`.
11. **Evidence.** The gateway builds and signs the receipt on Core's chain head (`Gateway.deliver`), with no verifier and no chain movement.
12. **Captured.** The exact evidence and the exact response bytes are journaled as `delivered`.
13. **Settlement.** The evidence is submitted to `SettlementCoordinator.settle`. Only Core's committed settlement advances the chain.
14. **Release.** Only on `accounting_settled` does the operation become `settled`. Only then can the agent retrieve the response (`retrieve`, which records `retrieved`), and only with an agent-signed authorization (see "Response authorization").
15. **Retention.** The response is kept until the agent acknowledges it, with a separate signed authorization (`acknowledge`, which records `acknowledged` and deletes the bytes).

A provider failure after the attempt began is `unknown`; it is never retried and never released. Settlement outcomes map as follows:
- a terminal verifier failure is `delivery_failed`;
- any other rejection or refusal is `disputed`;
- a refusal at or after expiry is `late`.

`purchase` never returns the response. A resubmitted idempotency key returns the journaled state and never buys again.

## States

`OperationState` values, in lifecycle order: `intent`, `reserved`, `executing`, `delivered`, `settled`, `retrieved`, `acknowledged`, `no_qualifying_offer`, `policy_rejected`, `released_undelivered`, `expired`, `abandoned`, `unknown`, `delivery_failed`, `disputed`, `late`.

In flight (the facility is busy): `intent`, `reserved`, `executing`, `delivered`.

Response available to the authorized agent: `settled`, `retrieved`.

| State | Meaning | Core state |
| --- | --- | --- |
| `intent` | Chosen signed purchase journaled | Possibly no reservation yet |
| `reserved` | Core reservation confirmed | Held, no attempt |
| `executing` | About to begin, or began, the delivery attempt | Held; attempt possibly recorded |
| `delivered` | Evidence and response bytes durable | Held, or settled before the journal caught up |
| `settled` | Accounting settled; response available | Settled; chain advanced |
| `retrieved` | Agent has received the response at least once | Settled |
| `acknowledged` | Agent confirmed; response bytes deleted | Settled |
| `no_qualifying_offer` | Core qualified no offer | Nothing |
| `policy_rejected` | Core's reserve rejected | No reservation |
| `released_undelivered` | Released with no attempt (audited) | Released; `no_delivery_releases` row |
| `expired` | Hold lapsed before any attempt | Released as expired |
| `abandoned` | Interrupted before Core reserved | No reservation |
| `unknown` | Attempt begun, no evidence captured | Held until expiry; attempt marked unknown |
| `delivery_failed` | Authenticated terminal verifier failure | Released; dispute and evidence |
| `disputed` | Other rejection (for example `CHAIN_BROKEN`) or refusal | Held until expiry; dispute and evidence |
| `late` | Submitted at or after `expires_at` | Released as expired; late-delivery dispute and evidence |

## Response authorization

Knowing an operation ID and an agent's public key ID is not enough. `retrieve` and `acknowledge` each take a `ResponseAuthorization`: an Ed25519 signature by the agent's key over this statement:

- the domain string `credence/integration/response-authorization/v1`, length-prefixed. It is distinct from C3A's `credence/signature/v1`, so an authorization is never a signature over a canonical object, or the reverse;
- followed by the Kessai canonical form (JCS, RFC 8785, + NFC) of `protocol` (`credence.integration.response_authorization`), `version` (`1`), `operation_id`, `action` (`retrieve` or `acknowledge`), `agent_key_id`, `nonce` (128 random bits as 32 hex digits) and `expires_at`.

It is accepted only if all of these hold:
- the action and operation are the ones requested;
- the nonce is well formed;
- it has not expired, and expires at most 300 seconds ahead;
- the key is in the current trusted registry, is an `agent` key, and is `current` (not revoked);
- the signature verifies;
- the key, and its subject, are the ones that signed the operation's purchase request.

Each nonce is single-use per agent key, across actions, operations, processes and restarts. It is consumed in `integration_authorization_nonces` in the same transaction as the access it authorizes, so a refused access does not consume it.

**Atomic access (I1g, finding F2).** Each access is one SQLite transaction on its own connection.
- `retrieve`, in order: re-read the agent key's persisted revocation; check that the response is available (`settled` or `retrieved`, bytes present); consume the nonce; record the retrieval; read and return the exact bytes.
- `acknowledge`, in order: re-read the revocation; require `retrieved`; consume the nonce; delete the retained bytes.
- Retrieval and acknowledgment racing on separate connections therefore serialize. Either the retrieval returns the exact bytes first, or the acknowledgment commits first and the retrieval is refused `ResponseUnavailable(acknowledged)`.
- A refused access commits nothing and consumes no nonce. Acknowledging before any retrieval is refused (`ResponseUnavailable(settled)`).

So a retrieval authorization cannot acknowledge, an authorization for one operation cannot open another, and a replay is refused. A refusal raises `NotAuthorized` with a code and carries no authorization material. Nothing is logged.

`NotAuthorized` codes: `MALFORMED_AUTHORIZATION`, `WRONG_ACTION`, `WRONG_OPERATION`, `MALFORMED_NONCE`, `AUTHORIZATION_EXPIRED`, `AUTHORIZATION_TOO_LONG`, `UNKNOWN_KEY`, `BAD_SIGNATURE`, `WRONG_ROLE`, `REVOKED_KEY`, `WRONG_AGENT`, `UNKNOWN_OPERATION`, `NONCE_REUSED`.

## Key revocation (I1g finding F1, lifecycle closed in I1i)

A revocation never appears successful unless it is committed to `integration_key_revocations`. These cases are distinct:

| Case | What happens | Durable? |
| --- | --- | --- |
| Core in-memory revocation: `KeyRegistry.revoke` on a registry never bound to a service | Changes that object only | No. It is not a Credence administrative operation |
| Durable bound revocation: `revoke` on the operator's registry while its service is open | Commits the record, then marks the key revoked, then returns | Yes |
| Revocation attempted after the service closed: `revoke` on that registry after `close()` | Marks the key revoked in that object (fail closed in-process) and raises `RevocationNotPersisted`; nothing is committed. Closing twice keeps this. Binding the same object to a new service persists what it marks | No, and the operator is told so |
| Trusted offline administrative revocation: `revoke_key(database, key_id, operator)` | Commits the record with no service running; success means committed | Yes |
| Durable state on restart | Persisted revocations override any registry supplied later, including a fresh one listing the key as current, and re-registration | — |
| Unexpected in-process revocation: a key revoked in the supplied registry with no record (for example `KeyRegistry.revoke(registry, key_id)`, which bypasses the binding) | Refused at once in this process; not persisted | No |

- **Record.** `integration_key_revocations` holds the key ID, the revocation time, the trusted administrative actor, and how it was made (`via`: `key_registry_revoke` or `offline_admin`).
  - For a bound revocation the actor is the `operator` the service was opened with. For an offline one it is the operator name plus the OS user ID.
  - It is in the same database. A record never expires and is never deleted, and revoking again keeps the first record.
  - An I1g database is upgraded in place on first use, keeping its records.
- **`lookup` performs no persistence and no database I/O on any branch.** Core calls it inside its own write transactions (settlement verifies signatures in-transaction). It reads an in-memory mirror of the persisted revocations and the set of keys refused in this process. Persistence happens only in the bound `revoke`, at binding (when the service opens), or in `revoke_key`, never inside Core's transactions.
- **Authority.** The service gives Core and the gateway a `DurableKeyRegistry`. Keys come from the operator's registry; a revoked key is reported `revoked` whatever that registry says.
- **One binding at a time.** A registry bound to an open service cannot be bound by another (`RegistryAlreadyBound`). A later service's binding replaces the closed one's, and a stale `close()` of the earlier service does not disturb it.
- **Trusted offline revocation.** `revoke_key` (command line: `python -m credence.integration.revocation --database PATH --key-id ed25519:... --operator NAME`) runs only when all of these hold:
  - the database already exists (it is never created: `DATABASE_MISSING`) and is an integration database (`NOT_AN_INTEGRATION_DATABASE`);
  - it is owned by the calling OS user (`NOT_OWNER`) and is owner-only (`INSECURE_PERMISSIONS`);
  - the caller takes its exclusive ownership lock (`OwnershipError` while a service runs).

  It fails loudly (`RevocationNotPersisted`) if the commit fails. It is not an HTTP endpoint, and agents and remote callers cannot revoke.
- **Emergency revocation.**
  - While the service runs: call `revoke` on the operator registry it was opened with. It returns only after the commit.
  - During downtime: run `revoke_key` as the OS user owning the database (the command above). Exit status 0 means committed; any refusal exits 2 with the reason. The next service start refuses the key whatever registry it is given.
- **Fail closed.**
  - An unreadable or malformed revocation record refuses opening the service, purchase admission, retrieval, acknowledgment and offline revocation (`RevocationStateError`).
  - A failed bound persistence raises `RevocationNotPersisted` and the key stays refused in-process.
- **Ordering.**
  - Retrieval and acknowledgment re-read the revocation row inside their access transaction, so a revocation committed first refuses them (`REVOKED_KEY`) without consuming a nonce.
  - Purchase admission (Core's reservation) and the pre-execution check below run under the same in-process gate as bound revocation commits. Offline revocation cannot race a running service; it needs the ownership lock.
- **Purchases already admitted when a revocation commits.**
  - Not yet executing (no Core delivery attempt): the provider is not called. The hold is released through Core's audited no-delivery release, and the operation ends `released_undelivered` with detail `agent key revoked before execution`.
  - Executing (Core recorded the attempt): the delivery may be payable, so it continues to accounting settlement. The revoked key cannot retrieve the response, which stays retained (see Known failure windows).
- **New purchases by a revoked agent key are refused before any provider call.** Core qualifies no offer (`KEY_REVOKED`). If the revocation lands between qualification and reservation, Core's reservation rejects it (`policy_rejected`, `KEY_REVOKED`).
- **A revoked gateway or provider key** makes affected settlements fail verification (`RECEIPT_SIG_INVALID`, a dispute with the hold kept). Nothing is settled or falsely released, and nothing is re-executed.

## Journal

The `integration_operations` table lives in the same SQLite database as Core's tables. It is operational, not a canonical object. Each write is one short `BEGIN IMMEDIATE` transaction, and each state change is a compare-and-set.

It holds:
- the operation ID (derived from the idempotency key) and the idempotency key;
- facility, prompt digest, provider, reservation and request digest;
- the agent key ID and its registry subject at purchase time;
- the exact signed request and the approved quote, service class and execution profile;
- the exact receipt, gateway signature, attestation and provider signature;
- receipt and response digests, and the exact response bytes;
- the settlement outcome, verifier decision digest and reason code;
- the state, timestamps, and retrieval and acknowledgment times.

Prompts, credentials and private keys are never stored. Consumed authorization nonces are kept in `integration_authorization_nonces`, and key revocations in `integration_key_revocations`.

## Response retention

- **Scope.** This is authorized for the hackathon only, and only for synthetic, non-sensitive demo content.
- **File protection.** The database and the owner lock file are created owner-only (`0600`). This is not production-grade protection of confidential response data.
- **Deletion.** A response is deleted only when the authorized agent acknowledges it.
- **No expiry.** A response that never becomes available (`disputed`, `late`, `delivery_failed`) is kept with its evidence, with no automatic expiry.

## Recovery

`recover()` runs when a service opens, before it serves. First it checks that every `settled` or `retrieved` entry is settled in Core; otherwise it raises `JournalInconsistency`, fails closed and changes nothing. Then it reconciles each in-flight entry against Core's records, oldest first:

| Journal | Core | Result |
| --- | --- | --- |
| `intent` | No reservation | `abandoned` |
| `intent` | Reservation held | Continue as `reserved` |
| `reserved` or `executing` | Held, no attempt | Audited release (`abandoned_before_execution`), `released_undelivered` |
| `reserved` or `executing` | Expired, no attempt | `expired` |
| `executing` | Attempt recorded | Core marks the attempt unknown (`interrupted`); `unknown`. Never retried |
| `delivered` | Receipt settled | `settled`; nothing is settled twice |
| `delivered` | Dispute or late record exists | `disputed`, `delivery_failed` or `late`, with no duplicate dispute |
| `delivered` | Held, nothing recorded | Resubmit the stored evidence to `settle` |

Recovery never:
- calls a provider again;
- charges again;
- releases a hold whose delivery may have happened;
- fabricates a receipt.

Interruption points are tested at each of:

`after_intent`, `after_reserve`, `after_executing_recorded`, `after_attempt_begun`, `after_provider_returned`, `after_evidence_stored`, `after_settle_committed`

## Known failure windows

- **After the attempt, before the evidence is durable.** A crash between Core's `begin_delivery` and the journal's `delivered` loses the response. Core cannot prove the provider was not called, so the outcome is `unknown`:
  - the hold stays until the 600-second expiry;
  - nothing is charged;
  - it is routed to reconciliation.

  The providers expose no idempotency or result lookup, so a paid execution may have happened that Credence cannot account for.

  A crash after `begin_delivery` but before the claim is also reported as `unknown`. The absence of a claim does show the supported gateway made no call, but the release rule still requires no attempt at all; it was not widened in I1e. The same conservative classification applies to failures a provider adapter raises before sending anything (for example a missing or malformed Nebius credential). This fails closed.
- **`unknown` holds lapse by expiry.** That frees headroom. If the provider did execute, the cost is outside Credence's accounting until reconciled by hand.
- **A revoked key strands its settled response.** Retrieval needs the purchasing key, by design, so a response settled for a key that is later revoked stays retained but unreadable until an operator handles it.
- **Revocations in the operator's registry before binding.** These are read through the registry's internal key table, because Core's `KeyRegistry` exposes no listing. From binding on, `revoke` on that registry is an instance-level override (durable while open, raising after close); Core's class is unchanged. A Core in-memory revocation on a registry that is never bound is not durable.
- **Ownership is advisory.** It binds every `IntegrationService`. Code that opens the SQLite file directly is not prevented.
- **Enforced interfaces only.** Provider execution is authorized by Core only through the supported interfaces: Core's coordinator, `Gateway.deliver` with Core's authority, and this service. A program that calls a provider adapter directly, or writes the SQLite file, is outside them. Credence does not claim execution is impossible there; such a delivery has no Core attempt and cannot settle.

## Refusals

`Refusal` values: `UNREGISTERED_FACILITY`, `FACILITY_LIMIT_MISMATCH`, `FACILITY_BUSY`, `PROMPT_NOT_UTF8`, `NO_OFFERS`, `IDEMPOTENCY_CONFLICT`.
