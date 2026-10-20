# INTEGRATION-FLOW

Owner: Integration (San Antonio, Claude Code). Status: Integration I1c, remediated in I1e. Only the owner edits this file. Other workstreams file a request in `contracts/requests/`.

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

So a retrieval authorization cannot acknowledge, an authorization for one operation cannot open another, and a replay is refused. A refusal raises `NotAuthorized` with a code and carries no authorization material. Nothing is logged.

`NotAuthorized` codes: `MALFORMED_AUTHORIZATION`, `WRONG_ACTION`, `WRONG_OPERATION`, `MALFORMED_NONCE`, `AUTHORIZATION_EXPIRED`, `AUTHORIZATION_TOO_LONG`, `UNKNOWN_KEY`, `BAD_SIGNATURE`, `WRONG_ROLE`, `REVOKED_KEY`, `WRONG_AGENT`, `UNKNOWN_OPERATION`, `NONCE_REUSED`.

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

Prompts, credentials and private keys are never stored. Consumed authorization nonces are kept in `integration_authorization_nonces`.

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
- **Ownership is advisory.** It binds every `IntegrationService`. Code that opens the SQLite file directly is not prevented.
- **Enforced interfaces only.** Provider execution is authorized by Core only through the supported interfaces: Core's coordinator, `Gateway.deliver` with Core's authority, and this service. A program that calls a provider adapter directly, or writes the SQLite file, is outside them. Credence does not claim execution is impossible there; such a delivery has no Core attempt and cannot settle.

## Refusals

`Refusal` values: `UNREGISTERED_FACILITY`, `FACILITY_LIMIT_MISMATCH`, `FACILITY_BUSY`, `PROMPT_NOT_UTF8`, `NO_OFFERS`, `IDEMPOTENCY_CONFLICT`.
