# INTEGRATION-FLOW

Owner: Integration (San Antonio, Claude Code). Status: Integration I1c, remediated in I1e and I1g. Only the owner edits this file. Other workstreams file a request in `contracts/requests/`.

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

## Key revocation (I1g, finding F1)

Revocation is durable and fail-closed. It survives restarts and overrides any registry supplied later.

- **Record.** `integration_key_revocations` holds the key ID, the revocation time, the trusted administrative actor (the `operator` the service was opened with), and how it was made. It is in the same database. A record never expires and is never deleted.
- **Authority.** The service gives Core and the gateway a `DurableKeyRegistry`. Keys come from the operator's registry; a persisted revocation reports the key as `revoked` whatever that registry says. Re-registering the key, or a freshly built registry that lists it as current, does not restore it.
- **The existing workflow is routed through persistence.** While the service is open, `KeyRegistry.revoke(key_id)` on the registry it was opened with commits the revocation record before the revocation takes effect. A revocation already present in that registry when the service opens is persisted at open. Closing the service restores Core's own `revoke` on that object.
- **No public revocation operation.** Revoking requires the operator's in-process registry object. Agents and remote callers cannot revoke.
- **Fail closed.**
  - An unreadable or malformed revocation record refuses opening the service, purchase admission, retrieval and acknowledgment (`RevocationStateError`).
  - If a revocation cannot be persisted, the key is still refused in this process and `RevocationNotPersisted` is raised.
- **Ordering.**
  - Retrieval and acknowledgment re-read the revocation row inside their access transaction, so a revocation committed first refuses them (`REVOKED_KEY`) without consuming a nonce.
  - Purchase admission (Core's reservation) and the pre-execution check below run under the same in-process gate as revocation commits.
- **Purchases already admitted when a revocation commits.**
  - Not yet executing (no Core delivery attempt): the provider is not called. The hold is released through Core's audited no-delivery release, and the operation ends `released_undelivered` with detail `agent key revoked before execution`.
  - Executing (Core recorded the attempt): the delivery may be payable, so it continues to accounting settlement. The revoked key cannot retrieve the response, which stays retained (see Known failure windows).
- A revoked agent key's new purchases are refused before any reservation: Core qualifies no offer (`KEY_REVOKED`).

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
- **Revocations made before the service opens.** These are read from the operator's registry through its internal key table, because Core's `KeyRegistry` exposes no listing. While the service is open, `revoke` on that registry is replaced by an instance-level override; Core's class is unchanged.
- **Ownership is advisory.** It binds every `IntegrationService`. Code that opens the SQLite file directly is not prevented.
- **Enforced interfaces only.** Provider execution is authorized by Core only through the supported interfaces: Core's coordinator, `Gateway.deliver` with Core's authority, and this service. A program that calls a provider adapter directly, or writes the SQLite file, is outside them. Credence does not claim execution is impossible there; such a delivery has no Core attempt and cannot settle.

## Remote agent signing (I2a, CR-1, provisional)

Status: implemented on the I2a offline integration branch; subject to Integration, Core and security review. No canonical object, C3A signature definition or Kessai serialization changed.

`purchase(job, signer)` needs an in-process signer. An agent behind HTTP signs in two phases instead:

1. **Prepare.** `prepare(market, agent_key_id=, prompt_digest=, idempotency_key=None)` → `Preparation | PurchaseRefused`. `Market` (facility terms, procurement classes and offers, in order) is supplied by the embedding process, never by the agent. The service checks that the authenticated key is a registered, current `agent` key for the facility's borrower, reads Core's exposure, builds one request per offer with the same builder as `purchase` (`purchase_requests`), and runs Core's qualification and the routing rule (`qualify_and_select`) against this database's real holds and used nonces. The agent has not signed yet, so each request is signed for that evaluation by an ephemeral sandbox key registered only in a throwaway registry; it is never stored and authorizes nothing. The chosen request is stored in `integration_preparations` with an opaque preparation ID (128 random bits), the agent key and subject, the facility, a digest of the market (Kessai: JCS (RFC 8785) + NFC), the offer's position, the exact canonical request and approved objects, the prompt digest, and a 300-second expiry (also the request's `expires_at`). Nothing is journaled or reserved and no provider is called.
2. **Sign.** The agent verifies every field itself and signs the request's existing C3A statement (`signature_message`). The server, browser and journal never hold the agent's key.
3. **Submit.** `submit_prepared(market, preparation_id=, signature=, prompt=, agent_key_id=)`. Under the facility lock, in order: an identical resubmission (same request, prompt and signature bytes) returns the journaled operation (`PREPARATION_CONSUMED` otherwise); the prompt must hash to the prepared digest (`PROMPT_MISMATCH`); the signature must be over the prepared request (`REQUEST_MISMATCH`) by the prepared key (`WRONG_AGENT`); the key must still be a current agent key for the borrower (`AGENT_NOT_AUTHORIZED`, `AGENT_REVOKED`) and the signature must verify (`BAD_SIGNATURE`); the preparation must not have expired (`PREPARATION_EXPIRED`); the market digest must be unchanged, the request must rebuild identically, and Core's qualification with the routing rule must still choose exactly the prepared request (`PREPARATION_STALE`). Another offer is never substituted. Only then does the prepared request, with the agent's signature, enter steps 6–15 of the lifecycle above, where Core verifies the signature again, reserves, and checks nonce, spending and the delivery attempt as for `purchase`.

Idempotency is durable: the preparation names one idempotency key, the journal admits one operation per key, and both are in the database, so a retry after a restart returns the journaled operation and never executes again. A `no_qualifying_offer` entry from this path records the verified agent key so the agent can read it.

Signing a request does not authorize execution by itself: execution still needs Core's committed `begin_delivery` and the gateway's claim.

## Agent-scoped reads (I2a, CR-3 and CR-4, provisional)

Read only; no journal or Core write.
- `operations_for_agent(agent_key_id, limit=, after=)`: operations whose purchase that key (and its registry subject) signed, oldest first, paged by the last operation ID of the previous page.
- `operation_for_receipt(receipt_digest)`: the operation that captured a receipt.
- `lifecycle_events(operation_id)`: each event whose time a record persisted: the journal's `created_at`, `retrieved_at`, `acknowledged_at` and latest transition (`updated_at`); Core's policy decision (`decided_at`), reservation (`created_at`), delivery attempt (`begun_at`, `execution_claimed_at`), no-delivery release, accounting settlement (`settled_at`), dispute and late-delivery records; the gateway-signed receipt's provider-reported `started_at` and `completed_at`; and the agent key's persisted revocation. Transitions whose time is not persisted (for example `delivered`) are not reported; new history persistence (CR-5) is not implemented.
- `public_key(key_id)`: the registry's public record of a key, with its durable status.
- `AccountingView` adds `accounting_settlement(receipt_digest)`, `policy_decision_digest(reservation_id)` and `decision_record(digest)`, backed by two read-only Core additions (`SettlementCoordinator.accounting_settlement`, `ReservationLedger.decision_record_digest`).

## Refusals

`Refusal` values: `UNREGISTERED_FACILITY`, `FACILITY_LIMIT_MISMATCH`, `FACILITY_BUSY`, `PROMPT_NOT_UTF8`, `NO_OFFERS`, `IDEMPOTENCY_CONFLICT`, `AGENT_NOT_AUTHORIZED`, `AGENT_REVOKED`, `NO_QUALIFYING_OFFER`, `ALREADY_SUBMITTED`, `UNKNOWN_PREPARATION`, `PREPARATION_EXPIRED`, `PREPARATION_STALE`, `PREPARATION_CONSUMED`, `PROMPT_MISMATCH`, `REQUEST_MISMATCH`, `WRONG_AGENT`, `BAD_SIGNATURE`.

The first six apply to `purchase`; the rest were added in I2a for `prepare` and `submit_prepared` (see "Remote agent signing"). Every refusal is returned before any journal entry, reservation or provider call.
