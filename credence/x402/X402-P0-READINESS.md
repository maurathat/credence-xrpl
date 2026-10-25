# X402-P0 readiness: t54 x402 buyer adapter

Branch `maurathat/credence-x402-adapter`, base `726f643` (I2b). Offline adapter and non-paying protocol discovery only. No payment was made, no payment header was sent, no key or signature was created, and no facilitator `/verify` or `/settle` was called. Interface contract: `contracts/X402-ADAPTER-CONTRACT.md`.

"RLUSD" and "Mainnet" in this document describe third-party merchants' observed terms. Credence's own execution asset is tRLUSD, a test token, on the configured local standalone network, and Credence does not run on Mainnet.

## Sources (accessed 2026-10-25 UTC)

| Source | Category | Accessible | Used for |
| --- | --- | --- | --- |
| https://xrpl-x402.t54.ai/docs/overview, `/docs/xrpl-scheme`, client and merchant guides | Facilitator doc | yes | Wire format, invoice binding, verification rules, error codes |
| https://xrpl-x402.t54.ai/ | Facilitator doc | yes (marketing page) | The host's `/supported`, `/openapi.json` and `/health` all return 404; it is not a facilitator API |
| https://xrpl-facilitator-mainnet.t54.ai `/supported`, `/openapi.json` | Facilitator (live, read-only) | yes; `/health` 404 | Versions, kinds, `/verify` and `/settle` shapes |
| https://xrpl-facilitator-testnet.t54.ai `/supported`, `/openapi.json` | Facilitator (live, read-only) | yes; `/health` 404 | Same, Testnet |
| https://docs.t54.ai/docs/xrpl/x402-facilitator | Facilitator doc | yes | Overview. Its example `facilitatorUrl` is the 404 host above |
| https://www.t54.ai/docs/trustline/overview | Facilitator doc | yes | Risk-product overview only; no wire format |
| https://xrpl-ai.org/directory (`asOf` 2026-10-25T02:28:26Z, 129 services) | Directory metadata | yes | Candidate listings |
| https://xrpl-ai.org/faq, `/build` | Facilitator doc (t54-run site) | yes | Idempotency, `/verify` side effects, multisig prose |
| https://github.com/t54-labs/rlusd-cli @ `155ab72` | SDK source | yes | Network IDs, RLUSD constants |
| PyPI `x402-xrpl` 0.3.4 (sdist sha256 `58c51e03…`) and 0.4.0b1 | SDK source (t54 buyer/merchant SDK) | yes | Payment construction, header envelope, binding derivation |
| https://github.com/t54-labs/monid-x402-xrpl @ `cc6ed20` | SDK source (t54-built merchant) | yes | Exact `accepted` echo, invoice handling |
| https://github.com/XRPLJapan/x402-xrpl @ `46ac0c0` | Third-party implementation | yes | Disagreements with t54 (multisig rejected, InvoiceID-only) |
| x402-foundation `specs/schemes/exact/scheme_exact_xrpl.md` | Generic spec, not t54 | yes | v1/v2 comparison only |

The public examples do **not** describe one identical wire protocol. They disagree on:

- invoice binding: t54 accepts Memos and/or InvoiceID; the generic spec requires InvoiceID only and forbids Memos;
- whether `sourceTag` is required;
- `payload.invoiceId`;
- `SendMax`;
- multisig;
- whether the challenge lives in the header or the body.

Where they disagree, the adapter follows the observed merchant challenges and t54's SDK, and refuses rather than guesses.

## Supported protocol versions

x402 **version 2**, scheme **`exact`**, XRPL networks `xrpl:{id}`.

- v1, any other version and the `upto` / `batch-settlement` schemes are explicit incompatibility results.
- Evidence for v2-only: the facilitator OpenAPI fixes `x402Version` to 2, `/supported` lists only v2, the SDK rejects anything else, and both live challenges are v2.

## Actual merchant challenge observations

Each was one unpaid, unauthenticated request with no payment header and no redirects followed.

| When (UTC) | Request | Status | Observed terms (merchant-advertised) | Adapter result |
| --- | --- | --- | --- | --- |
| 02:32:13 (research, curl) and 02:51:42 (adapter, `credence.x402.discover`) | `POST https://router.heurist.xyz/x402/xrpl/v1/chat/completions` body `{"prompt":"ping"}` | 402 | v2 `exact`, `xrpl:0`, 0.0005 RLUSD (`524C55…`, issuer `rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De`), payTo `rBi1QrVjwaNofisZAQnovoXdHkS73FG1tJ`, `maxTimeoutSeconds` 600, a fresh `invoiceId` per request, **no `sourceTag`**, advertised model `heurist/glm-5.3`, `discoveryProbe: true`; header and body JSON-equal | `challenge_incompatible`: `MAINNET_READ_ONLY`, `ASSET_NOT_APPROVED`, `MULTISIGNATURE_UNKNOWN`, `INVOICE_BINDING_NOT_IN_X1_TEMPLATE`, `SUBMISSION_NOT_AUTHORIZED` |
| 02:34:05 (research) and 02:51:42 (adapter) | `GET https://monid-x402-xrpl-production.up.railway.app/t/openai/v1/image/gpt-image-2.5-sunburst-edit` | 402 | v2 `exact`, `xrpl:0`, 0.001 RLUSD, `sourceTag` 804681468; header and body JSON-equal | same as above (image tool, not chat inference) |
| 02:34:05 (research) | `GET https://x402.greenhead.io/v1/ai/video` | 404 | Replit "app isn't live yet" page; no x402 data | would be `not_x402` |

- **Heurist is a candidate, not a selectable service.** It is the only OpenAI-compatible chat-completions listing in the directory, and it answers with a well-formed x402 v2 challenge. Its terms are RLUSD on Mainnet, which X402-P0 may only display and assess.
- **Probe prices are not real-request prices.** Heurist's 0.0005 RLUSD is for a probe body (`discoveryProbe: true`); the price for a real request depends on the request.
- **Heurist's own payments are batched.** The directory shows them as batch `CheckCash` settlements under t54's beta `batch-settlement` scheme, not `exact`.
- **No service is selectable.** With no approved network matching any live merchant, no service is selectable today. This is the expected X402-P0 outcome, not a defect.

The recorded challenges, `/supported` bodies, a directory excerpt and the ledger invoice-binding vectors are in `contracts/fixtures/x402_p0/`; their sha256 values are pinned in `x402-p0-deterministic.json`.

## Facilitator compatibility findings

| Question | Finding | Evidence category |
| --- | --- | --- |
| Protocol versions | v2 only | Facilitator OpenAPI and `/supported`, SDK, observed |
| Payment header | `PAYMENT-SIGNATURE`, base64 of `{x402Version: 2, accepted, payload: {signedTxBlob, invoiceId}, resource?, extensions?}`. `X-PAYMENT` unused. `accepted` must echo the challenge entry (Monid: full equality) | Doc and SDK. Not sent here |
| Payment model | The buyer presigns an XRPL Payment; the **facilitator submits** it; the buyer gets a `PAYMENT-RESPONSE` claim | Doc and SDK |
| Invoice binding | Required. `InvoiceID = SHA256(UTF-8(invoiceId))` and/or Memo `MemoData = HEX(UTF-8(invoiceId))`; the SDK default is both. Credence's derivation reproduces two validated Mainnet payments | Doc, SDK, ledger (conformance test) |
| `/verify`, `/settle` | Request `{paymentPayload, paymentRequirements, extensions?}`. Verify returns `{isValid, invalidReason, payer}`; settle returns `{success, transaction, network, payer?, errorReason?}`. `/settle` blocks until validated (60 s ceiling) | Facilitator OpenAPI. Not called |
| XRPL Payment serialization | Payment with no Flags, no Paths or DeliverMin; `SendMax = Amount` for an issued currency; `LastLedgerSequence` required (SDK: `+ceil(t/5)+2`); fee ≤ 0.01 XRP; no `NetworkID` on Mainnet | SDK, doc, ledger |
| Two-signature multisigning | **Unverified.** The SDK single-signs, every observed payment is single-signed, and the FAQ prose says a SignerList "should" work but states no verifier rule. XRPLJapan's facilitator rejects multisig. Compatibility with the ResearchBot + Credence 2-of-2 policy is not assumed | SDK, doc prose, third party |
| Networks and assets | Hosted facilitators: Mainnet `xrpl:0` (`exact`, `batch-settlement`) and Testnet `xrpl:1` (`exact`, `upto`, `batch-settlement`). No hosted Devnet. XRP and issued currencies; RLUSD Mainnet issuer `rMxCK…5De`. The Testnet RLUSD issuer appears only in t54 repo constants | Live `/supported`, docs, SDK |
| Idempotency and replay | Invoices are single-use (consumed after a successful settle, 900 s TTL by default). `/settle` retry returns the cached result; a concurrent duplicate gets 409. Unknown or consumed invoice: 400 `unknown_invoice_id`. Settle uncertainty: 409 `settlement_status_unknown` | FAQ, SDK |
| Non-paying compatibility check | An unpaid request to a merchant returns the full challenge. `GET /supported` and `/openapi.json` need no auth. `/verify` is described as side-effect free but needs a signed blob, so it was not used | Live, doc |

## Procurement integration results

- **Same Core decision path.** Core's real policy engine decides an x402 offer through the unchanged `qualify_and_select`, beside Nebius and local Qwen.
- **Accepted.** A compatible mock merchant priced at 50 micro-tRLUSD is accepted and wins on price.
- **Rejected by Core.** Over the agent's signed cap: `PRICE_ABOVE_MAX`. Over the facility limit: `OVER_LIMIT`. Without operator-asserted location: `PROFILE_NOT_ALLOWED`. On a Tier-2 facility: `TIER_TOO_LOW` (local Qwen still wins). Not on the facility: `PROVIDER_NOT_ALLOWED`.
- **Nebius and Qwen unchanged.** Their outcomes are identical with and without an x402 candidate, in both the general and the controlled scenario.
- **Request 0009.** The faithfulness limits (flat-price projection, prepayment, money unit, location, evidence) are in `contracts/requests/0009-core-x402-flat-price-offers.md`.

## Offline test counts (all pass)

| File | Tests |
| --- | --- |
| `tests/unit/test_x402_challenge.py` | 77 |
| `tests/unit/test_x402_lifecycle.py` | 35 |
| `tests/unit/test_x402_discovery.py` | 19 |
| `tests/unit/test_x402_prepare.py` | 19 |
| `tests/unit/test_x402_procurement.py` | 14 |
| `tests/unit/test_x402_discover_cli.py` | 2 |
| `tests/conformance/test_x402_p0_fixtures.py` | 6 |
| **X402-P0 total** | **172** |
| `tests/unit/test_x402_p2_remediation.py` (X402-P2) | 113 |
| `tests/unit/test_x402_p3_g1.py` (X402-P3, G1) | 39 |
| **Total** | **324** |

Brief tests 1–20 map to these files as follows:

| Brief tests | File |
| --- | --- |
| 1–5, 7, 9 (parse level), 18 | challenge |
| 10, 11 | discovery |
| 5, 6, 20 | procurement |
| 6–10, 12, 13, 19 | prepare (end to end) |
| 12–17, 19 | lifecycle |

Every x402 test disables sockets except the X402-P2 F4 and X402-P3 G1 redirect tests, which run a loopback-only HTTP server to prove the real transport sends one request and never follows a redirect. No test signs, transfers or makes a paid request.

X402-P3 (Skopje G1): outbound requests are immutable from construction and re-validated by every sender immediately before forwarding, so a mutated header list, a mutated inner pair, a mutated bytearray body or an `object.__setattr__` bypass cannot put a credential or payment header (or a different body) on the wire.

## X402-P2 remediation (Skopje F1–F4, Ulaanbaatar E1–E4)

All eight findings were reproduced against `c56a5e0` (32 exploitable cases). Each is now refused, as shown by `tests/unit/test_x402_p2_remediation.py`. In summary:

- **F1:** body challenges are recognized whatever the content type; header and body must be strictly equal JSON; ambiguous representations are refused.
- **F2:** only an authenticated, validated `tec…` result is a terminal failure; `ter`/`tel`/`tef`/`tem` are provisional (unknown) or invalid evidence.
- **F3/E3:** `verify_binding` rebuilds the whole proposal from its stated terms and from the expected (consistency-checked) terms.
- **F4:** outbound headers are an allow-list.
- **E1:** a proposal needs a Core-evaluated purchase, a decision that Core's pure engine reproduces on the supplied inputs, the exact flat-price projection, one micro-tRLUSD amount throughout, and the endpoint-derived `x402.` identity. These are consistency checks on caller-supplied inputs, not authentication of Core state. The full trust boundary (X402-P3D) is in the contract's *Authority and trust boundary* section and request 0009 D1–D12.
- **E2:** compatibility is recomputed, Mainnet is never evaluable, and no public network can be approved.
- **E4:** one proposal per purchase request ever, one transaction hash per proposal, and confirmation only for the expected transaction and invoice binding. All in memory only; not restart-safe.

The adapter remains non-paying.

## Remaining change requests

- `contracts/requests/0009-core-x402-flat-price-offers.md` (Core), revised in X402-P2 as items C1–C12: prepaid procurement contract, exact flat-price accounting, durable reservation identity, one payment per reservation, exposure at validated payment, outstanding delivery obligation, receipt chain, facility concurrency, network fees, merchant namespace, Tier-1 receipt, signed or recomputable decisions.
- `contracts/requests/0010-x1-x402-prepayment-and-invoice-binding.md` (X1), revised in X402-P2 as items X1–X11: verified Core reservation input, independent policy-signer recomputation, network-specific NetworkID, exact transaction and invoice matching, durable uniqueness, transaction-hash replay protection, `LastLedgerSequence` bounds, facilitator submission, verified terminal outcomes, multisig, builder reuse.
- **Claims note for Maura (not resolved here):** the build plan's line "Providers are paid after verification" cannot hold for an x402 merchant, which is paid before it serves. X402-P0 pays nothing, so nothing contradicts it today.

## Open compatibility questions for t54 engineers

1. Does the hosted facilitator require `payload.invoiceId`, or only the SDK merchant middleware? The scheme-page examples omit it.
2. Is `extra.sourceTag` required? The docs say yes (default 804681468); SDK 0.3.4 injects none; Heurist omits it. Should a buyer stamp `SourceTag` when the challenge has none?
3. Settled Mainnet payments carry a `urn:x402:facilitator` memo, but neither probed challenge had `extra.facilitator`. Must buyers add it?
4. Does the hosted facilitator accept InvoiceID-only binding (no Memos)? Credence's X1 template would prefer one exact field.
5. Does the hosted `exact` verifier accept a multisigned Payment (`SigningPubKey ""` plus `Signers`, 2-of-2 SignerList)? Is there a Testnet path to prove it end to end?
6. Are issued currencies supported on the Testnet facilitator now, and is `SendMax = Amount` mandatory for them?
7. Must `accepted` byte-equal the challenge entry, including merchant-specific `extra` keys? Is that a facilitator rule or a merchant rule?
8. What `LastLedgerSequence` upper bound does the facilitator enforce relative to `maxTimeoutSeconds`?
9. What is the full `errorReason` vocabulary, how long are idempotent settle results kept, and is `settlementAttempted` exposed?
10. Is t54's `batch-settlement` scheme available on its Mainnet facilitator? `/supported` lists it, but the Mainnet OpenAPI has no session paths.
11. Is there a hosted Devnet (`xrpl:2`) facilitator?
12. If the `PAYMENT-REQUIRED` header and the body ever differ, which is authoritative? The adapter refuses the challenge.
13. Please confirm the Testnet RLUSD issuer the facilitator expects, and the canonical API hosts.
