# API-CONTRACT

Owner: API/UI (Claude Code). Status: Product P0, offline adapter on baseline `a3ec0a3`; I2a integrated offline service added provisionally (see "I2a" below). Only the owner edits this file; other workstreams file a request in `contracts/requests/`.

This contract describes `credence/api/` as implemented. The code is authoritative; `tests/unit/test_api_p0.py` exercises every route below.

**What P0 is.** A read-only HTTP adapter for the Credence Markets UI (`web/`). It makes no purchase, holds no agent key, accepts no signing secret and builds no XRPL transaction. It calls no private Core internals and changes no Core, Inference or Integration contract. Amounts are integer micro-tRLUSD (tRLUSD is a test token). Accounting settlement is not XRPL settlement.

Run: `uv run uvicorn --factory credence.api.app:create_app --port 8000`. The UI reads `VITE_CREDENCE_API_BASE_URL` (default `http://127.0.0.1:8000`). CORS allows loopback origins only.

## Operations

All routes are under `/v0`. Each client operation in `web/src/lib/credence-api/client.ts` maps to one route.

| Client operation | Route | Source of the answer |
| --- | --- | --- |
| `health` | `GET /health` | Static: mode `offline`, no ledger connection |
| `listProviders` | `GET /providers` | Committed market configuration (`contracts/fixtures/inference_c3/market-selection-deterministic.json`), Nebius catalog declarations and the live market observation's last recorded deliveries |
| `procurementPresets` | `GET /procurement/presets` | Scenario A and Scenario B from the committed market configuration |
| `evaluateProcurement` | `POST /procurement/evaluate` | **Real Core dry run** (see below) |
| `submitPurchase` | `POST /purchases` | No purchase. A preset opens its recorded offline run (`offline_replay`); anything else is `not_available` / `LIVE_PURCHASE_NOT_CONNECTED` |
| `listOperations` / `getOperation` | `GET /operations`, `GET /operations/{id}` | Recorded offline `IntegrationService` runs |
| `getAccountingExposure` | `GET /operations/{id}/exposure` | Core `AccountingView.exposure` recorded at each step of that run |
| `listReceipts` / `getReceiptEvidence` | `GET /receipts`, `GET /receipts/{digest}` | Recorded evidence, **re-checked on each request** with Core's digest and signature functions |
| `retrieveAuthorizedResponse` | `POST /operations/{id}/response` | Always `501 RESPONSE_RETRIEVAL_NOT_CONNECTED` |
| `getPaymentStatus` | `GET /operations/{id}/payment` | Always XRPL `not_submitted`, `transaction_hash` null |
| (fixtures) | `GET /xrpl/settlement-fixtures` | `contracts/fixtures/api_p0/xrpl-settlement-ui-states.json`, labelled UI fixtures |

Errors are `{"error": CODE, "detail": text}`: `UNKNOWN_OPERATION`, `UNKNOWN_RECEIPT` (404); `INVALID_PROCUREMENT`, `NO_PROVIDERS` (422). Request bodies are strict: integers only for amounts, and unknown members (for example a key) are refused.

## Procurement dry run

The form fields map onto Core objects: model family, the allowed precisions and quantizations, the minimum evidence tier, jurisdictions, residencies and providers become a `ProcurementClass`. Limit, drawn, allowed providers and minimum tier go on a `Facility` copied from Scenario A's. The maximum acceptable price becomes each `PurchaseRequest.max_total_cost_micro_trlusd`; when it is empty, each offer's own quote maximum is used, as `IntegrationService` does. The prompt's digest becomes the request's `prompt_digest`.

Each offer's request is signed in-process by an ephemeral sandbox agent key. That key is generated for one evaluation, registered only in that evaluation's registry, never stored and never returned. `credence.inference.market.qualify_and_select` then runs Core's policy engine against an empty temporary reservation ledger. Nothing is reserved and no provider is called.

The response carries Core's reason code and policy decision record for every offer. Plain-language reasons come from `credence/api/explanations.py`; the structural and tier details come from Core's own `structural_failures` and `required_tier`. The adapter explains Core's decision; it never makes one. `held` is 0 in the dry run.

The Scenario A and Scenario B presets reproduce the committed market selection exactly: the same qualification, reason codes, maximum costs and choice (tested).

## Recorded offline runs

`contracts/fixtures/build_api_p0_fixtures.py` writes `contracts/fixtures/api_p0/offline-operations.json`; `tests/conformance/test_api_p0_fixtures.py` regenerates it byte-for-byte. Each run is a real `IntegrationService` run over the I1 offline test world. It uses the real NebiusProvider adapter over a fake transport, the fake signing provider standing in for local Qwen, TEST keys, a deterministic clock and a temporary SQLite file.

| Run | Final journal state |
| --- | --- |
| Scenario A | `acknowledged` (Tier 1, accounting settled, retrieved, acknowledged) |
| Scenario B | `acknowledged` (Tier 2) |
| No qualifying offer | `no_qualifying_offer` |
| Provider not connected | `released_undelivered` |
| Provider failure after the attempt | `unknown` |
| Interrupted after the attempt | `unknown` (`interrupted`) |
| Interrupted before any attempt | `released_undelivered` |
| CHAIN_BROKEN (test-harness manipulation) | `disputed` |
| Late delivery | `late` |

The UI names the lifecycle `intent → reserved → executing → delivered → accounting_settled → retrieved → acknowledged`. `accounting_settled` is the journal state `settled`, which the service enters only on the coordinator's `accounting_settled` outcome.

## Real versus simulated

| Real (computed by Core code at request time) | Recorded (real code, offline run) | Fixture or configured |
| --- | --- | --- |
| Procurement qualification and reason codes; receipt digests; gateway and provider signatures; attestation, request and profile bindings | Lifecycle transitions, exposure, chain heads, disputes and releases; the live Nebius Tier 1 and local Qwen Tier 2 receipts of 2026-10-19 | Quote prices (configured demo); XRPL settlement states other than `not_submitted` |

## Integration mapping (for the accepted IntegrationService)

The routes, request bodies and response shapes above stay as they are. Connecting the accepted integration (I1g/I1h) changes only what serves them. Below, each route is mapped to the backend calls that exist at baseline `a3ec0a3`; names are from `credence/integration/service.py`, `credence/integration/authorization.py` and `credence/core/settlement_coordinator.py`. If the accepted commit renames or changes them, this section must be re-checked against it before any wiring.

Where a backend capability is missing, it is listed as a **gap**. The adapter must not stand in for one. Until the backend provides it, the route keeps its current P0 answer (`not_available`, `501`, `not_submitted`).

Every route answers errors as `{"error": CODE, "detail": text}`. Codes are passed through from the backend unchanged; the API never invents a decision code.

### Signed purchase submission: `POST /v0/purchases`

| | |
| --- | --- |
| Backend call | `IntegrationService.purchase(job: Job, signer: AgentSigner) -> PurchaseReport \| PurchaseRefused` |
| `Job` | `idempotency_key: str`, `facility: Facility`, `procurement_classes: tuple[ProcurementClass, ...]`, `offers: tuple[Offer(quote, service_class, execution_profile), ...]`, `prompt: bytes`, `request_expires_at: Timestamp` |
| Response, success | `PurchaseReport`: `operation_id`, `state` (`OperationState`), `detail`, `reservation_id`, `provider_id`, `receipt_digest`, `reason_code`. Never the response bytes |
| Response, refusal | `PurchaseRefused(refusal: Refusal, detail)` → HTTP 409 for `FACILITY_BUSY` and `IDEMPOTENCY_CONFLICT`, 422 for `PROMPT_NOT_UTF8`, `NO_OFFERS`, `FACILITY_LIMIT_MISMATCH` and `UNREGISTERED_FACILITY`, with `error` = the `Refusal` value |
| Replay | Resubmitting an `idempotency_key` returns the journaled `PurchaseReport` and never buys again; the API returns it with HTTP 200 |
| Policy outcomes | `no_qualifying_offer` and `policy_rejected` are states in a 200 `PurchaseReport`, not HTTP errors |
| Other errors | `ServiceClosed` → 503 `SERVICE_UNAVAILABLE`; `JournalInconsistency` → 500 `JOURNAL_INCONSISTENCY` (the service has failed closed; nothing is retried). `OwnershipError` is raised when the service opens, not per request: the API must refuse to start rather than serve |

Proposed request body, for when the gaps below are closed: `{idempotency_key, facility_id, prompt, procurement_class_digests, quote_ids, request_expires_at, signatures: [SignedObject per prepared PurchaseRequest]}`. The `ProcurementBody` of the dry run is not a purchase request.

**Gaps (no backend capability exists):**

1. **Remote agent signing.** `signer` is an in-process callable that the service calls once per offer, inside the facility lock, after reading Core's exposure. An agent behind HTTP cannot answer it. Closing this needs an Integration interface change: for example a prepare step that returns the unsigned `PurchaseRequest` per offer, and a submit step that accepts the agent's `SignedObject`s and verifies each is over exactly the prepared request. The API must not hold or create agent keys.
2. **Buyer price cap.** The service sets each request's `max_total_cost_micro_trlusd` to the quote maximum. A cap the agent chose cannot be carried, so the UI marks the cap preview-only.
3. **Offers and facility source.** `Job` takes the `Facility`, procurement classes and offers from the caller. A server-side source of current quotes and stored facility terms is needed; `AccountingView` exposes only `limit`, `drawn` and `held`.
4. **Authentication.** The agent session that may call this route is not defined.

### Live operation reads: `GET /v0/operations`, `GET /v0/operations/{id}`

| | |
| --- | --- |
| Backend call | `IntegrationService.operation(id) -> PurchaseReport`; `IntegrationService.journal_entry(id) -> Operation` for detail (canonical request, quote, service class, profile, receipt, signatures, `settlement_outcome`, `verifier_decision_digest`, timestamps) |
| Mapping | `state` → `state`; `OUTCOME_CLASS` and `STATE_TEXT` from `credence/api/explanations.py`; `lifecycle[].reached` from the linear order of the journal's compare-and-set transitions |
| Errors | `UnknownOperation` → 404 `UNKNOWN_OPERATION` |
| Must not expose | `Operation.response` (the response bytes): only `retrieve` releases them |

**Gaps:** the journal keeps only the current state and `created_at`, `updated_at`, `retrieved_at` and `acknowledged_at`, so `steps[]` (the per-step history) cannot be served live; the live route returns a single current step. There is no listing method for all operations: `pending_operations()` returns only in-flight ones.

### Exposure: `GET /v0/operations/{id}/exposure`

| | |
| --- | --- |
| Backend call | `IntegrationService.accounting.exposure(facility_id, now) -> FacilityExposure(limit, drawn, held)`; `headroom` is its property `limit - drawn - held` |
| Mapping | `limit_micro_trlusd`, `drawn_micro_trlusd`, `held_micro_trlusd`, `headroom_micro_trlusd` (integers, micro-tRLUSD) |
| Errors | `SettlementRefused("UNACCOUNTED_FACILITY")` → 404 `UNREGISTERED_FACILITY` |

**Gaps:** facility terms (minimum tier, allowed providers, status, maturity) are not readable through `AccountingView`. No exposure history is kept, so the live `timeline` would have one point.

### Agent-signed retrieve and acknowledge: `POST /v0/operations/{id}/response`

| | |
| --- | --- |
| Request body | The agent's `ResponseAuthorization`, unchanged: `{operation_id, action: "retrieve" \| "acknowledge", agent_key_id, nonce (32 lower-case hex), expires_at, signature (hex)}`. Signed outside the browser over `credence/integration/response-authorization/v1` and the Kessai canonical statement |
| Backend call | `action = retrieve` → `IntegrationService.retrieve(authorization) -> bytes`; `action = acknowledge` → `IntegrationService.acknowledge(authorization) -> None` |
| Response | retrieve: 200 with the response bytes (`application/octet-stream`); acknowledge: 204 |
| Errors | `NotAuthorized(code)` → 403 with `error` = the code (`MALFORMED_AUTHORIZATION`, `WRONG_ACTION`, `WRONG_OPERATION`, `MALFORMED_NONCE`, `AUTHORIZATION_EXPIRED`, `AUTHORIZATION_TOO_LONG`, `UNKNOWN_KEY`, `BAD_SIGNATURE`, `WRONG_ROLE`, `REVOKED_KEY`, `WRONG_AGENT`, `UNKNOWN_OPERATION`, `NONCE_REUSED`); `ResponseUnavailable(state)` → 409 `RESPONSE_UNAVAILABLE` with the state; `JournalInconsistency` → 500 |
| Must not | Log, store or echo the authorization, or retry it: each nonce is single-use |

The client's `retrieveAuthorizedResponse(operationId, authorization)` already forwards the authorization unchanged. The `action` field selects retrieve or acknowledge. No gap other than authentication of the session.

### Receipt evidence: `GET /v0/receipts/{digest}`

| | |
| --- | --- |
| Backend calls | `journal_entry(id)`: `receipt()`, `gateway_signature()`, `provider_attestation()`, `provider_signature()`, `request()`, `execution_profile()`, `receipt_digest`, `response_digest`, `settlement_outcome`, `verifier_decision_digest`, `reason_code`. `accounting`: `receipt_settled(digest)`, `chain_head(facility_id)`, `dispute_evidence(digest)`, `late_delivery_disputes()` |
| Checks | Unchanged: recomputed with Core's `digest()` and `verify_signed` |
| Errors | Unknown digest → 404 `UNKNOWN_RECEIPT` |

**Gaps:** there is no lookup from receipt digest to operation (the journal is keyed by operation), and no list of receipts. The trusted `KeyRegistry` is private to the service, so signature checks need a published read-only key list. The full verifier `DecisionRecord` is stored by Core and readable through `ReservationLedger.decision_record_bytes(digest)`, but `AccountingView` does not expose it, and the service does not expose its ledger.

### XRPL reconciliation status: `GET /v0/operations/{id}/payment`

| | |
| --- | --- |
| Today | Always `{"xrpl": {"state": "not_submitted", "transaction_hash": null}}`. True at this baseline: no XRPL transaction is built, signed or submitted |
| Target states | `not_submitted`, `pending_confirmation`, `validated_success`, `validated_failure`, `expired_not_included`, `unknown_reconciliation_required` (the fixture vocabulary). `validated_success` is the only state that may be shown as a confirmed payment |
| Target schema | `{state, transaction_hash, ledger_index, last_ledger_sequence, engine_result, settlement_record_digest}`, all null until observed |

**Gap:** everything. `SettlementState.XRPL_SETTLED` is reserved and unimplemented, `credence/xrpl_adapter` is a Checkpoint 0 stub, and `contracts/XRPL-ADAPTER-CONTRACT.md` is a placeholder. The canonical `credence.settlement_record` needs a real `xrpl_transaction_hash`, so none can be created. The UI keeps showing `not_submitted` until the XRPL workstream defines a reconciliation read.

### Providers: `GET /v0/providers`

**Gap:** there is no live quote source or availability check; quotes stay configured demo values.

### Raising these gaps

The gaps above need interface changes owned by Integration (San Antonio), Inference and XRPL Integration. They are not filed as interface change requests yet, because the accepted I1g/I1h commit may already change these interfaces. File them in `contracts/requests/` against the accepted commit.

## I2a: integrated offline service (provisional)

Status: implemented on `maurathat/credence-i2a-offline`, subject to security review (requests 0006–0008). The P0 routes above are unchanged and still serve the recorded runs. Tested by `tests/integration/test_i2a_api.py`, `test_i2a_agent_client.py` and `test_i2a_preparation.py`.

Run: `uv run python -m credence.api.offline_demo init <dir>`, then `uv run python -m credence.api.offline_demo serve <dir>` (loopback only). The `IntegrationService` is embedded in this one process, which owns the database (`ProcessOwnership`). Providers are offline mocks (`credence/api/offline_world.py`: the real Nebius adapter over a mock transport, and the fake signing provider standing in for local Qwen); no provider is called. Quotes are configured demo prices. Gateway and mock-provider keys are TEST keys derived from public labels.

**Authentication.** `Authorization: Bearer <agent session token>`. The server stores only each token's SHA-256 (`<dir>/server/agents.json`); the token and the agent's Ed25519 key live in `<dir>/agent/`, which the server never reads. The token authenticates API calls only; every purchase and response access needs the agent's own signature. A revoked agent key is refused on every route (`403 AGENT_REVOKED`).

**Authorization.** An agent sees only its own facilities (borrower account = its key's subject), preparations and operations (signed by its key); anything else answers `404` as if absent. Idempotency keys are namespaced per agent.

| Client operation | Route | Backend |
| --- | --- | --- |
| `liveSession` | `GET /v0/live/session` | session principal and key status |
| `liveFacilities` | `GET /v0/live/facilities` | configured terms + Core `exposure` read now (no history) |
| `preparePurchase` | `POST /v0/live/purchases/prepare` | `IntegrationService.prepare`. Body: the dry-run procurement fields (no facility limit or drawn), `prompt_digest` (never the prompt), optional `facility_id`, `idempotency_key`. The procurement must be a class registered on one of the agent's facilities (`422 PROCUREMENT_NOT_REGISTERED`); a price cap is refused (`422 PRICE_CAP_PREVIEW_ONLY`) |
| `getPreparation` | `GET /v0/live/purchases/preparations/{id}` | the exact canonical request, digest, C3A statement, nonce, expiry, provider, quote, profile, price |
| `submitSignedPurchase` | `POST /v0/live/purchases/submit` | `IntegrationService.submit_prepared`. Body: `preparation_id`, `prompt`, `signature` (a C3A `SignedObject`) |
| `listLiveOperations` / `getLiveOperation` | `GET /v0/live/operations?limit&after`, `GET /v0/live/operations/{id}` | `operations_for_agent`, `journal_entry`, `lifecycle_events` (persisted timestamps only). Never the response bytes |
| `getLiveExposure` | `GET /v0/live/operations/{id}/exposure` | facility terms + current exposure |
| `getLiveReceipt` | `GET /v0/live/receipts/{digest}` | `operation_for_receipt`; checks recomputed with Core's functions against the service's public key records; accounting settlement row, verifier decision record, chain head, disputes |
| `getLivePayment` | `GET /v0/live/operations/{id}/payment` | `payment_status.NoXrplPaymentStatus`: always `not_submitted`, no hash or ledger fields |
| `postLiveResponse` | `POST /v0/live/operations/{id}/response` | the agent's `ResponseAuthorization` → `retrieve` (200, octet-stream) or `acknowledge` (204) |

Errors are `{"error": CODE, "detail": text}`. Integration refusals pass through unchanged with these statuses: `403` `AGENT_NOT_AUTHORIZED`, `AGENT_REVOKED`, `WRONG_AGENT`, `BAD_SIGNATURE`; `404` `UNKNOWN_PREPARATION`; `409` `FACILITY_BUSY`, `IDEMPOTENCY_CONFLICT`, `ALREADY_SUBMITTED`, `PREPARATION_STALE`, `PREPARATION_CONSUMED`; `410` `PREPARATION_EXPIRED`; `422` `UNREGISTERED_FACILITY`, `FACILITY_LIMIT_MISMATCH`, `PROMPT_NOT_UTF8`, `NO_OFFERS`, `NO_QUALIFYING_OFFER`, `PROMPT_MISMATCH`, `REQUEST_MISMATCH`. Adapter codes: `401` `AUTHENTICATION_REQUIRED`, `INVALID_SESSION`; `404` `UNKNOWN_FACILITY`, `UNKNOWN_OPERATION`, `UNKNOWN_RECEIPT`; `422` `PRICE_CAP_PREVIEW_ONLY`, `PROCUREMENT_NOT_REGISTERED`, `FACILITY_REQUIRED`, `INVALID_PROCUREMENT`, `INVALID_PROMPT_DIGEST`, `INVALID_IDEMPOTENCY_KEY`, `MALFORMED_SIGNATURE`, `INVALID_LIMIT`; `409` `RESPONSE_UNAVAILABLE`; `403` `NotAuthorized` codes; `500` `JOURNAL_INCONSISTENCY`; `503` `SERVICE_UNAVAILABLE`, `INTEGRATED_SERVICE_NOT_RUNNING`. Bodies are strict; unknown members (for example a key) are refused.

**Agent client.** `credence/agent/client.py` and `cli.py` (owner: Inference) prepare with the prompt's digest, independently verify every prepared field (digest, canonical bytes, C3A statement, own key and prompt, facility, provider, price ceiling, evidence floor, short expiry, quote/service/profile consistency), sign with `credence.core.signatures.sign`, and sign `ResponseAuthorization`s. The UI pastes their JSON output; it never holds the key.

**XRPL.** X0 is not merged. `payment_status.route_state` records the preflight state mapping for a future reviewed read-only adapter and is not wired.
