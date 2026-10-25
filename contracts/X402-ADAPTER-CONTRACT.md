# x402 buyer adapter contract (X402-P0, remediated in X402-P2)

Owner: x402 workstream (X402-P0/P2, `maurathat/credence-x402-adapter`). Package: `credence/x402/`.
Status: offline adapter and non-paying discovery only. Nothing in this package signs, submits or pays.

Credence uses this adapter to evaluate and prepare purchases from XRPL x402-protected services. The first facilitator ecosystem is t54's. Core accounting, agent authorization, delivery verification and the XRPL payment journal are unchanged. Nebius and local Qwen are unaffected.

## Naming

Merchant payment terms are third-party data, recorded exactly as observed. A t54 merchant that asks for RLUSD on XRPL Mainnet (`xrpl:0`) is recorded as RLUSD on `xrpl:0` and assessed read-only. Credence's own approved execution asset is tRLUSD, a test token, on the configured local standalone network. Each `CompatibilityDecision` keeps the two in separate fields. No network is authorized for submission, Testnet included. `ApprovedExecution` refuses any public network ID (1024 or lower: Mainnet 0, Testnet 1, Devnet 2), so only a local standalone network can be approved at all.

## Modules

| Module | Responsibility |
| --- | --- |
| `transport` | The only path to the network. Origin allow-list (exact scheme/host/port; plain http loopback-only). Outbound headers are an explicit allow-list: `accept` and `content-type` only, case-insensitive, valid token names, clean printable values, `content-type` exactly `application/json` (optionally `; charset=utf-8`), no repeats. Everything else (auth, cookies, proxy credentials, payment headers, unknown or malformed names) is refused. `HttpRequest` copies headers into a tuple of `(str, str)` tuples and the body into `bytes` at construction, so later mutation of the caller's objects has no effect. `GuardedClient.send` and `StdlibTransport.send` both re-validate (`validate_request`) immediately before sending, so a frozen-dataclass bypass cannot reach the wire (X402-P3, G1). Never follows redirects. Keeps only permitted response headers and a bounded body. |
| `mock` | Deterministic in-memory transport for tests. Records every attempted request. |
| `challenge` | Version-aware typed 402 parser, `request_digest`, `check_live`, `parse_settlement_response`. |
| `compatibility` | `FacilitatorProfile` (from `/supported`, plus multisig knowledge), `assess` → `CompatibilityDecision`. |
| `discovery` | Directory listings (advertised), `probe` and `reprobe` (observed), endpoint pinning. |
| `procurement` | Maps an x402 offer onto Core `ExecutionProfile` / `ServiceClass` / `Quote` so that Core's own `policy_engine.evaluate` decides it (through the unchanged `inference.market.qualify_and_select`). |
| `prepare` | `offer_from` a probed candidate (compatibility recomputed); `x402_provider_id`; `proposal_terms` / `prepare_proposal` from a Core-evaluated purchase, recomputing every fact. |
| `proposal` | `ProposalTerms`, `PaymentAuthorizationProposal`, unsigned Payment and digest, `verify_binding` (full reconstruction), `PaymentAuthorityBook` (in-memory). |
| `lifecycle` | Offline payment-before-delivery state model. |
| `discover` | Operator-run non-paying discovery CLI; JSON report. |
| `errors` | `X402Refusal` codes and `X402Refused`. |

## Supported wire protocol

Taken from t54's facilitator docs, its live `/supported` and `/openapi.json`, its buyer SDK (`x402-xrpl` 0.3.4), and two observed merchant challenges. Sources are in `credence/x402/X402-P0-READINESS.md`.

- **x402 version 2 only.** Any other `x402Version` returns `IncompatibleChallenge(UNSUPPORTED_VERSION)`. v1 fields (`maxAmountRequired`) inside a v2 entry are `CONTRADICTORY_CHALLENGE`.
- **Scheme `exact` only.** `upto` and `batch-settlement` (both advertised by t54) and any unknown scheme return `UNSUPPORTED_SCHEME`.
- **Networks:** CAIP-2 `xrpl:{network_id}`. Other chains are ignored and listed in `ignored`. Non-canonical spellings (`xrpl-testnet`, `xrpl:01`) are not XRPL.
- **Challenge location:** the `PAYMENT-REQUIRED` header (base64 JSON), the body, or both.
  - A body challenge is recognized by its content whatever the content type (absent, `application/json`, `text/plain`, `text/json`, any `+json` type, or another type when the body is JSON).
  - If both carry a challenge they must be *strictly* equal JSON (same keys, values and JSON types: `1`, `1.0` and `true` differ), else `CONTRADICTORY_CHALLENGE`. Neither representation is ever preferred.
  - Ambiguous representations are refused (`MALFORMED_CHALLENGE`): a challenge-shaped object nested inside the body, a non-UTF-8 charset, a UTF-16/32 byte-order mark, a UTF-8 BOM before JSON, or a body that is declared or looks like JSON but fails strict parsing.
  - Non-challenge error bodies (HTML, plain text, JSON without challenge keys) are ignored as before.
  - A repeated header is contradictory. JSON is strict: duplicate keys and NaN/Infinity are refused. The 64 KiB body limit is unchanged.
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
- `policy_evaluable`: the observed network, currency and issuer are exactly the approved ones, the network is not `xrpl:0`, and the requirement is not cross-currency;
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
| `destination`, `network` | Where, on which network |
| `ledger.source_account`, `.network_id`, `.fee_drops`, `.sequence`, `.last_ledger_sequence` | The ledger context of the unsigned Payment (bound into `proposal_id` since X402-P2) |
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

`verify_binding(proposal, terms)` fails closed unless the proposal is exactly what `terms` produce (X402-P2):

1. It rebuilds the unsigned Payment, its digest, the idempotency key and the proposal ID from the proposal's own stated terms. A destination changed with the transaction bytes left alone, a mutated transaction with a recomputed digest, or a replaced ID are all refused.
2. It rebuilds the whole proposal from the trusted `terms` and requires every field to be equal.

The first differing field is named in `BINDING_ALTERED`. Trusted terms come only from `prepare.proposal_terms`. The X1 mapping and its gaps are in request 0010.

`prepare_proposal` consumes the Core-evaluated purchase (`inference.market.Qualification`: the signed `PurchaseRequest`, its Core objects, the `PolicyDecision` and the headroom and nonce facts it was decided with), the trusted key registry, the approved execution, the facilitator profile and the ledger context. It accepts no caller-supplied intent, merchant identity, evidence tier or compatibility decision. It refuses when any of the following holds:

- the request is not the one challenged, or its body is not the prompt the signed request commits to (`RESOURCE_MISMATCH`);
- the challenge is not the pinned endpoint (`ENDPOINT_CHANGED`);
- the requirement is from another challenge (`BINDING_ALTERED`);
- recomputed compatibility makes the requirement not policy-evaluable (its first reason, e.g. `MAINNET_READ_ONLY`);
- the ledger context or the requirement is not on the approved network (`NETWORK_NOT_APPROVED`);
- the challenge has expired (`CHALLENGE_EXPIRED`);
- the purchase's provider is not the endpoint's reserved x402 identity `x402.<host>.<12 hex of sha256(url, method, network, payTo)>` (`PROVIDER_NAMESPACE`), so a Nebius, local Qwen or other merchant's decision cannot be reused;
- the decision is not a policy-stage acceptance (`POLICY_REJECTED`);
- re-running Core's pure `evaluate` with the re-verified request signature does not reproduce the supplied decision record exactly (`CORE_DECISION_MISMATCH`). The record's input digests are those of this request, facility, quote, service class, profile and procurement classes;
- the quote, service class and profile are not exactly this requirement's flat-price projection, or Core's approved maximum, its reservation amount, the projected price and the merchant amount are not the same micro-tRLUSD integer (`CORE_DECISION_MISMATCH`);
- the paying account is not the facility's borrower account (`AGENT_MISMATCH`).

The agent identity is the request's signing key. The facility, purchase request and prompt come from the signed request. The required tier is the strictest of the facility's and its procurement classes'.

`PaymentAuthorityBook` is in memory only and not restart-safe. It refuses:

- a second proposal for one purchase request, ever, whatever happened to the first (`PURCHASE_ALREADY_AUTHORIZED`);
- a second proposal for one invoice, by any agent (`DUPLICATE_INVOICE`);
- re-admission of one idempotency key (`PROPOSAL_REPLAYED`);
- a second transaction hash for one proposal, or one hash for two proposals or two confirmations (`TRANSACTION_ALREADY_BOUND`).

## Payment-before-delivery lifecycle

`QUOTED → POLICY_ELIGIBLE → PAYMENT_AUTHORIZATION_PENDING → PAYMENT_PENDING → PAYMENT_CONFIRMED → DELIVERY_PENDING → DELIVERED`

| Rule | Behaviour |
| --- | --- |
| Quote refused / expired | `QUOTE_REFUSED` / `QUOTE_EXPIRED`, only before any payment could exist |
| Timeout | `PAYMENT_STATUS_UNKNOWN`, never `PAYMENT_REJECTED` |
| Provisional engine result (`ter…`, `tel…`, `tef…`, `tem…`, or `tes…` before validation) | `PAYMENT_STATUS_UNKNOWN`; never terminal |
| Validated evidence not authenticated by the configured `ValidatedEvidenceVerifier` (default `NoTrustedEvidenceSource` authenticates nothing; X1's result verifier is the intended one) | refused, `PAYMENT_EVIDENCE_UNAUTHENTICATED`; record unchanged |
| Validated evidence without a validated ledger index and hash, or not for the expected transaction (the hash bound at `PAYMENT_PENDING`, the unsigned digest, InvoiceID, memo, source and destination tags) | refused, `PAYMENT_EVIDENCE_MISMATCH`; record unchanged |
| Authenticated validated failure with a `tec…` result | `PAYMENT_REJECTED` |
| Validated failure claiming `tes`, `ter`, `tel`, `tef`, `tem`, or a missing or malformed code | refused, `PAYMENT_EVIDENCE_MISMATCH` |
| Validated success, but amount/asset/issuer/destination/network differs | `VALIDATED_UNEXPECTED_DELIVERY` |
| Merchant settlement response | Recorded, with mismatches named; never confirms or rejects |
| Confirmed payment | Never implies delivery; delivery needs its own bound observation |
| Delivery failed (no response, non-200) | `DELIVERY_FAILED`; payment kept; never pays again |
| Delivery below required tier | `DELIVERY_EVIDENCE_INSUFFICIENT`; payment kept |
| Unresolvable | `MANUAL_RECONCILIATION_REQUIRED`, from unknown, unexpected, failed or insufficient |
| Observation for another agent or proposal | `AGENT_MISMATCH` / `BINDING_ALTERED`; record unchanged |

From `PAYMENT_PENDING` on, `may_propose_payment` is False for good, and the authority book never admits another proposal for the same purchase request. Nothing resubmits or repays. This model is not Core's settlement lifecycle.

## Not in X402-P0 or X402-P2

- Payment submission and real `PAYMENT-SIGNATURE` headers.
- Signing, custody and facilitator `/verify` or `/settle` calls.
- Live paid inference and a second XRPL journal.
- Any Testnet or Mainnet execution.
- `upto` and `batch-settlement`.
- UI changes.

## Tests

- `tests/unit/test_x402_challenge.py`, `test_x402_discovery.py`, `test_x402_procurement.py`, `test_x402_prepare.py`, `test_x402_lifecycle.py`, `test_x402_discover_cli.py` and (X402-P2) `test_x402_p2_remediation.py`. Shared worlds: `x402_world.py`, `x402_core_world.py`.
- `tests/conformance/test_x402_p0_fixtures.py` (recorded observations and the deterministic fixture).
- Fixtures are in `contracts/fixtures/x402_p0/`. Regenerate with `uv run python contracts/fixtures/build_x402_p0_fixtures.py`.
