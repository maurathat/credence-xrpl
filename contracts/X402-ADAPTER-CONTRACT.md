# x402 buyer adapter contract (X402-P0)

Owner: x402 workstream (X402-P0, `maurathat/credence-x402-adapter`). Package: `credence/x402/`.
Status: offline adapter and non-paying discovery only. Nothing in this package signs, submits or pays.

Credence uses this adapter to evaluate and prepare purchases from XRPL x402-protected services. The first facilitator ecosystem is t54's. Core accounting, agent authorization, delivery verification and the XRPL payment journal are unchanged. Nebius and local Qwen are unaffected.

## Naming

Merchant payment terms are third-party data, recorded exactly as observed. A t54 merchant that asks for RLUSD on XRPL Mainnet (`xrpl:0`) is recorded as RLUSD on `xrpl:0` and assessed read-only. Credence's own approved execution asset is tRLUSD, a test token, on the configured local standalone network. Each `CompatibilityDecision` keeps the two in separate fields. Under X402-P0 no network is authorized for submission, Testnet included.

## Modules

| Module | Responsibility |
| --- | --- |
| `transport` | The only path to the network. Origin allow-list (exact scheme/host/port; plain http loopback-only). Refuses payment, credential and cookie headers. Never follows redirects. Keeps only permitted response headers and a bounded body. |
| `mock` | Deterministic in-memory transport for tests. Records every attempted request. |
| `challenge` | Version-aware typed 402 parser, `request_digest`, `check_live`, `parse_settlement_response`. |
| `compatibility` | `FacilitatorProfile` (from `/supported`, plus multisig knowledge), `assess` → `CompatibilityDecision`. |
| `discovery` | Directory listings (advertised), `probe` and `reprobe` (observed), endpoint pinning. |
| `procurement` | Maps an x402 offer onto Core `ExecutionProfile` / `ServiceClass` / `Quote` so that Core's own `policy_engine.evaluate` decides it (through the unchanged `inference.market.qualify_and_select`). |
| `prepare` | `offer_from` a selectable candidate; `prepare_proposal` after Core accepts. |
| `proposal` | `PaymentAuthorizationProposal`, unsigned Payment and digest, `verify_binding`, `ProposalBook`. |
| `lifecycle` | Offline payment-before-delivery state model. |
| `discover` | Operator-run non-paying discovery CLI; JSON report. |
| `errors` | `X402Refusal` codes and `X402Refused`. |

## Supported wire protocol

Taken from t54's facilitator docs, its live `/supported` and `/openapi.json`, its buyer SDK (`x402-xrpl` 0.3.4), and two observed merchant challenges. Sources are in `credence/x402/X402-P0-READINESS.md`.

- **x402 version 2 only.** Any other `x402Version` returns `IncompatibleChallenge(UNSUPPORTED_VERSION)`. v1 fields (`maxAmountRequired`) inside a v2 entry are `CONTRADICTORY_CHALLENGE`.
- **Scheme `exact` only.** `upto` and `batch-settlement` (both advertised by t54) and any unknown scheme return `UNSUPPORTED_SCHEME`.
- **Networks:** CAIP-2 `xrpl:{network_id}`. Other chains are ignored and listed in `ignored`. Non-canonical spellings (`xrpl-testnet`, `xrpl:01`) are not XRPL.
- **Challenge location:** the `PAYMENT-REQUIRED` header (base64 JSON), the JSON body, or both. If both are present they must be JSON-equal, else `CONTRADICTORY_CHALLENGE`. A repeated header is also contradictory. JSON is strict: duplicate keys and NaN/Infinity are refused.
- **`resource.url`** must equal the requested URL exactly (`RESOURCE_MISMATCH`).
- **Per requirement:**
  - `amount`: integer drops for XRP; a positive XRPL decimal value string (no exponent, at most 16 significant digits) for an issued currency.
  - `asset`: `XRP`, a 3-character code, or a 40-hex code.
  - `extra.issuer`: required for an issued currency, forbidden for XRP.
  - `payTo`: classic address.
  - `maxTimeoutSeconds`: 1..3600. This is the validity window; expiry is observation time plus the window.
  - `extra.invoiceId`: required, printable ASCII, 1..128 characters.
  - Optional uint32 fields: `extra.sourceTag` and `extra.destinationTag`. Optional boolean: `extra.crossCurrency`. Optional `extra.facilitator`: `{id, name}`.
  - Any other `extra` key is kept verbatim as `advertised_extra`. The exact entry is kept as `raw`.
- **Consistency:** two requirements for one network and asset with different amount or payTo are `CONTRADICTORY_CHALLENGE`, and so are two different requirements sharing one invoice ID.
- **Settlement response:** a `PAYMENT-RESPONSE` header must decode as base64 JSON with a boolean `success`, a 64-hex upper-case `transaction`, an `xrpl:N` `network` and a classic-address `payer`. Anything else is `SETTLEMENT_RESPONSE_MISMATCH`. Even a well-formed response is the merchant's *claim*; it never confirms a payment.

## Advertised versus observed

- `X402Challenge.observed` (`ObservedFacts`) holds only facts Credence saw itself: origin, request method, URL and digest, HTTP status, where the challenge was carried, and observation time.
- `requirements`, `resource_*`, `error`, `extensions` and `advertised_extra` are merchant claims.
- A `DirectoryListing` is advertised metadata. `claims_inference` is the directory's category claim.
- `VerificationStatus` records what was observed: `UNVERIFIED`, `NOT_ALLOWED`, `UNREACHABLE`, `NOT_X402`, `CHALLENGE_INVALID`, `CHALLENGE_INCOMPATIBLE`, `CHALLENGE_COMPATIBLE` or `ENDPOINT_CHANGED`.
- Only `CHALLENGE_COMPATIBLE` is `selectable`. None of these statuses means "delivery works": X402-P0 never pays, so it never observes an x402 delivery.

## Compatibility decision

`assess(requirement, approved, facilitator)` returns `CompatibilityDecision`:

- the observed network, currency, label, issuer and amount;
- the approved network, currency, label and issuer;
- `policy_evaluable`: the observed network, currency and issuer are exactly the approved ones, and the requirement is not cross-currency;
- `read_only`: the network is `xrpl:0`;
- `executable`: always False;
- `reasons`, in order:
  - `MAINNET_READ_ONLY` or `NETWORK_NOT_APPROVED`
  - `ASSET_NOT_APPROVED` or `ISSUER_NOT_APPROVED`
  - `CROSS_CURRENCY_UNSUPPORTED`
  - `FACILITATOR_KIND_UNSUPPORTED`
  - `MULTISIGNATURE_UNSUPPORTED` or `MULTISIGNATURE_UNKNOWN`
  - `INVOICE_BINDING_NOT_IN_X1_TEMPLATE`
  - `SUBMISSION_NOT_AUTHORIZED`

For both t54 hosted facilitators, multisig support is `UNKNOWN`.

## Procurement integration

`procurement.project` builds Core objects that Core's real policy engine evaluates beside every other candidate:

- **`ExecutionProfile`:** only the model identifier is filled in, taken from the merchant's advertised model if any. Every other field is NOT ASSERTED.
- **`ServiceClass`:** model family is the operator's mapping. Jurisdiction and residency are taken only from `OperatorAssertions`, never from merchant or directory data. Evidence tier is at most Tier 1 (`WITNESSED`), because an x402 payment adds no evidence.
- **`Quote`:** a *policy projection* of the flat per-request price.
  - Input price = the flat amount per 1,000,000 units, input cap 1,000,000; output price 0, output cap 0.
  - Core's `max_total_cost_value` of the projection equals the flat amount exactly (asserted), so `PRICE_ABOVE_MAX` and `OVER_LIMIT` are decided on the true price.
  - It is marked `billing_faithful = False` and must never reach billing or the verifier (request 0009).
- **Money:** only the approved tRLUSD asset, as an exact whole number of micro-units, becomes `MicroTrlusd`. Any other asset is refused before Core is asked; it is never converted.

## Payment-authorization proposal schema

`PaymentAuthorizationProposal` (immutable; `proposal_id` is the sha256 of the Kessai canonical form, JCS (RFC 8785) + NFC, of the body below with `protocol` `credence.x402.payment_authorization_proposal` and `version` `1`):

| Field | Meaning |
| --- | --- |
| `intent.agent_id`, `intent.facility_id`, `intent.purchase_request_digest`, `intent.prompt_digest` | Agent identity and Credence purchase intent |
| `merchant_id`, `resource_url`, `request_method` | Service / merchant identity |
| `request_digest` | sha256 of `{method, url, content_type, body_digest}` of the original request |
| `requirement_digest` | sha256 of the exact `accepts[]` entry |
| `invoice.invoice_id`, `.invoice_id_field`, `.memo_data`, `.source_tag`, `.destination_tag` | The invoice binding: `InvoiceID = SHA256(UTF-8(invoiceId))` and `MemoData = HEX(UTF-8(invoiceId))` |
| `amount.currency`, `.issuer`, `.value` | Exact XRPL wire amount, as required |
| `destination`, `network`, `network_id`, `source_account` | Where, on which network, from which account |
| `expires_at` | Observation time plus `maxTimeoutSeconds` |
| `unsigned_transaction`, `unsigned_transaction_digest`, `unsigned_transaction_digest_scheme` | The unsigned multisign-ready Payment and its `xrpl-stx-sha512half-v1` digest |
| `idempotency_key` | sha256 of agent, purchase request, merchant, resource, request digest and requirement digest |
| `required_evidence_tier`, `payment_before_delivery` | Evidence and delivery terms |
| `x1_template_gaps` | Every departure from X1's restricted Payment template |
| `submission_authorized` | Always `false` |

The unsigned Payment follows t54's SDK shape:

- `Flags 0`, so never `tfPartialPayment`, and no `Paths` or `DeliverMin`;
- `SendMax = Amount` for an issued currency;
- `InvoiceID` and one invoice `Memo`;
- `SourceTag` and `DestinationTag` only when the merchant states them;
- `NetworkID` only above 1024;
- `SigningPubKey ""` and no signatures.

`verify_binding` recomputes the binding from the invoice ID and re-derives the digest; any alteration is `BINDING_ALTERED`. The X1 mapping and its gaps are in request 0010.

`prepare_proposal` refuses when any of the following holds:

- the request is not the one challenged (`RESOURCE_MISMATCH`);
- the challenge is not the pinned endpoint (`ENDPOINT_CHANGED`);
- the requirement or decision is from another challenge (`BINDING_ALTERED`);
- the requirement is not policy-evaluable (its first reason);
- the challenge has expired (`CHALLENGE_EXPIRED`);
- Core rejected the purchase (`POLICY_REJECTED`);
- the ledger context is on another network (`NETWORK_NOT_APPROVED`).

`ProposalBook` refuses a second proposal for one invoice, by any agent (`DUPLICATE_INVOICE`), and re-admission of one idempotency key (`PROPOSAL_REPLAYED`).

## Payment-before-delivery lifecycle

`QUOTED → POLICY_ELIGIBLE → PAYMENT_AUTHORIZATION_PENDING → PAYMENT_PENDING → PAYMENT_CONFIRMED → DELIVERY_PENDING → DELIVERED`

| Rule | Behaviour |
| --- | --- |
| Quote refused / expired | `QUOTE_REFUSED` / `QUOTE_EXPIRED`, only before any payment could exist |
| Timeout | `PAYMENT_STATUS_UNKNOWN`, never `PAYMENT_REJECTED` |
| Validated failure (with result code) | `PAYMENT_REJECTED` |
| Validated success, but amount/asset/issuer/destination/network differs | `VALIDATED_UNEXPECTED_DELIVERY` |
| Merchant settlement response | Recorded, with mismatches named; never confirms or rejects |
| Confirmed payment | Never implies delivery; delivery needs its own bound observation |
| Delivery failed (no response, non-200) | `DELIVERY_FAILED`; payment kept; never pays again |
| Delivery below required tier | `DELIVERY_EVIDENCE_INSUFFICIENT`; payment kept |
| Unresolvable | `MANUAL_RECONCILIATION_REQUIRED`, from unknown, unexpected, failed or insufficient |
| Observation for another agent or proposal | `AGENT_MISMATCH` / `BINDING_ALTERED`; record unchanged |

From `PAYMENT_PENDING` on, `may_propose_payment` is False for good. This model is not Core's settlement lifecycle.

## Not in X402-P0

- Payment submission and real `PAYMENT-SIGNATURE` headers.
- Signing, custody and facilitator `/verify` or `/settle` calls.
- Live paid inference and a second XRPL journal.
- Any Testnet or Mainnet execution.
- `upto` and `batch-settlement`.
- UI changes.

## Tests

- `tests/unit/test_x402_challenge.py`, `test_x402_discovery.py`, `test_x402_procurement.py`, `test_x402_prepare.py`, `test_x402_lifecycle.py` and `test_x402_discover_cli.py`.
- `tests/conformance/test_x402_p0_fixtures.py` (recorded observations and the deterministic fixture).
- Fixtures are in `contracts/fixtures/x402_p0/`. Regenerate with `uv run python contracts/fixtures/build_x402_p0_fixtures.py`.
