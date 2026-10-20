# API-CONTRACT

Owner: API/UI (Claude Code). Status: Product P0, offline adapter on baseline `a3ec0a3`. Only the owner edits this file; other workstreams file a request in `contracts/requests/`.

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

## Backend dependencies for replacing the adapter

The UI depends only on the routes above. Connecting the accepted integration (I1g/I1h) means serving them from the live service, with these additions, none of which exists today:

- `submitPurchase`: a supported entry point for a signed purchase from an authenticated agent session. Today `IntegrationService.purchase` takes an in-process signer, and it sets each request's maximum to the quote maximum; a buyer price cap would need to be carried.
- `getOperation` and `getAccountingExposure`: live reads of `IntegrationService.operation` and `AccountingView.exposure`, plus a per-step history that the journal does not keep (it keeps only the current state and timestamps).
- `retrieveAuthorizedResponse`: forward the agent-signed `ResponseAuthorization` unchanged to `IntegrationService.retrieve` and `acknowledge`. The API must not hold or create agent keys.
- `getPaymentStatus`: an XRPL settlement record. None is implemented (`SettlementState.XRPL_SETTLED` is reserved).
- `listProviders`: a live quote source and availability check. Quotes are configured demo values.
