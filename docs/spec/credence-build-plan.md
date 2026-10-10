# Credence Build Plan

Oct 9, 2026 · @Maura

## Summary

Credence is working capital for AI agents: an agent borrows from an XRPL lending vault, spends only on verified inference, and repays from what it earns. Credence is the Loan Broker, underwriter and policy gate between them. The owner's Ledger device holds root authority, so the agent operates freely but cannot rewrite its own credit.

**Pitch line:** the hardware wallet is the constitution, not the cashier.

**What makes it a product, not a demo:** real loans on a real XRPL lending ledger, real inference calls from two real providers, a real policy engine that computes every decision, signed receipts anyone can verify, and four working consoles for the four parties (owner, agent operator, lender, provider).

**Entries targeted**

| Entry | Fit | What judges see |
| --- | --- | --- |
| Lending & Borrowing track (primary) | Uses the Lending Protocol (XLS-66) and Single Asset Vaults (XLS-65) with Credence as Loan Broker doing the off-chain underwriting the protocol leaves open | Vault funded, loan originated, spent, repaid, credit history updated |
| Agentic Finance track (secondary, if dual entry is allowed) | An autonomous agent borrows, buys and repays with its own key | ResearchBot running jobs live |
| Ledger bounty, agentic payments track | The Ledger device is the account's master key and approves every material credit decision | Owner approves on the device; owner revokes the agent on the device |

Confirm at the Oct 14 briefing whether one project can enter a main track and the Ledger bounty together.

**Built on, not repeated:** AgentLevy's x402 payment flow, Sever's revocation pattern, and the Kessai serializer shared with the custody and dataset classes. Everything else in this plan is new work.

## Product scope

One product ships on Oct 25: **Credence Credit**, a purpose-bound inference credit facility, complete across all four parties. Spot buying is included because the facility spends through it. Forwards and secondary trading stay on the roadmap.

**Ships working**

- Lender deposits into a closed-ended Single Asset Vault during its subscription window and sees shares, utilization and yield as the vault moves to investment and then redemption.
- Credence posts first-loss cover as Loan Broker.
- Owner creates an agent, sets its keys on the Ledger, and approves a facility on the device.
- Credence underwrites the agent, prices the spread, and co-signs the loan.
- ResearchBot runs paid research jobs, buying inference from two providers through the policy gate.
- Every purchase produces a signed receipt; every rejection produces a reason code.
- ResearchBot repays from customer revenue; its credit history and next spread update.
- Owner revokes the agent on the Ledger; the next purchase fails on-ledger and at the gate.
- Provider console: execution profiles, capacity, receipts signed, reputation.
- Public receipt verifier: paste or upload a receipt, see each check pass or fail.
- Attack lab: the full attack suite runs live against the real engine.

**Ships as clearly labeled previews (no live money, no trading)**

- Reservations: book a quantity of inference units for a future window, physically delivered. Preview data only.
- Price index and forward curve: computed from real Credence purchase data plus dated public reference prices, labeled illustrative.

**Roadmap only (slide, not app)**

- Forwards, secondary market, AMM capacity pools, cash settlement.
- Escrow-backed settlement, ideally with Smart Escrows (XLS-100) checking receipt conditions on-ledger.
- Financed batch inference: a loan sized to a customer contract and a batch job, likely the first commercial product.
- Tier 3 evidence (measured loaded artifact) via attested GPU execution.
- UOR-ADDR conformance against the reference vectors.
- Delegated sub-agents and sub-accounts once XRPL supports them.

## Actors and roles

Six parties, each with its own key and its own screen. No party holds another party's key.

| Actor | Holds | Can do | Cannot do |
| --- | --- | --- | --- |
| Owner (human or company) | Ledger device = master key of the agent's XRPL account | Create the agent, set or revoke its working key, approve facilities and limit changes | Spend day to day (the agent does that) |
| ResearchBot (agent) | One of two required signatures on the borrower account (Credence holds the other) | Borrow under an approved facility, buy eligible inference, receive customer payments, repay | Take on new debt without owner approval; disable the master key; buy outside policy |
| Credence | Loan Broker account key and its signing service key | Underwrite, price, co-sign loans, enforce purchase policy, issue credit history | Move the agent's funds alone; sign for the agent |
| Lenders | Their own XRPL accounts | Deposit tRLUSD into the vault, withdraw available liquidity, see utilization and losses | Choose individual borrowers |
| Providers (two at launch) | Provider signing key, published execution profiles | Accept purchases, serve inference, sign receipts, get paid | Be paid for an unbound or non-conforming receipt |
| Customers of ResearchBot | Their own XRPL accounts | Pay ResearchBot for completed research jobs | Anything inside Credence |

For the demo, Customers and Lenders are scripted accounts you control; Providers are real services (see Inference providers).

## System architecture

All decisions live in one Python core; the web app only displays them. This follows the rule from Sever: a thin server wrapper over real logic, never a JavaScript reimplementation, so the demo cannot drift from the repo.

| Component | Language / tech | Responsibility |
| --- | --- | --- |
| Credence Markets web app | Existing React + TanStack Router app | Four consoles, receipt verifier, attack lab. Reads from the API; computes nothing |
| Credence API | Python, FastAPI | Thin HTTP and WebSocket layer over the core; streams live events to the UI |
| Policy engine | Python library, pure functions | Decides every purchase: accept or reject with a reason code |
| Reservation ledger | Python, in the state store | Internal accounting holds against a facility for pending purchases; handles concurrent requests and failed deliveries. Not funds locked on XRPL |
| Verifier | Python library | Checks receipts: signatures, bindings, replay, profile match. Produces an immutable decision record |
| Settlement coordinator | Python | Consumes verifier decisions and executes the permitted payment: a co-signed provider payment, or release of the reservation |
| Repayment scheduler | Python | Turns revenue earmarked for repayment into the LoanPay amounts the ledger will actually accept |
| Kessai serializer | Python, general `kessai-canon` package extracted from custody (JCS (RFC 8785) + NFC) | Canonical bytes and digests for every request, profile and receipt |
| Underwriting service | Python | Scores agents, prices spreads, sets limits and terms |
| Loan Broker signer | Python, key in 1Password | Supplies the counterparty signature on a loan only when the owner's Ledger approval is present and valid |
| XRPL adapter | Python, `xrpl-py` | Vault, loan, payment and key transactions; ledger stream watcher |
| Inference gateway | Python, x402-style flow carried over from AgentLevy | Quotes prices, runs the policy check, forwards the call, collects the receipt |
| Provider A adapter | Python | Calls Nebius Token Factory's public API (no partnership claimed); Credence witnesses the exchange |
| Provider B | Local model on the Mac mini M4 behind a signing sidecar | Serves inference and signs its own receipts |
| ResearchBot | Python agent | Takes jobs, buys inference, delivers reports, invoices customers, earmarks repayment |
| Ledger bridge | TypeScript in the browser, WebHID, Ledger's XRP app library | Asks the device to sign owner transactions; never sees the agent's key |
| State store | SQLite for the hackathon (Postgres later) | Facilities, reservations, receipts, used nonces, events, credit history |
| Ledger network | Local standalone `rippled` in Docker, pinned release build, lending amendments enabled | Real ledger with real Lending Protocol and vault transactions |

Provider A and Provider B deliberately sit at different evidence tiers, so the demo shows the policy engine telling them apart (see Domain model).

&#91;embedded content: money path · who signs what\]

Money enters from lenders and customers, leaves only to providers and back to the vault, and every exit from the borrower account carries two signatures.

## Domain model

Six typed objects carry the whole product. Each is serialized with the Kessai serializer and identified by its digest, so any party can recompute and check it.

**Money rule:** every amount is an integer in the smallest unit (micro-units: 1 tRLUSD = 1,000,000 micro-units). On the local ledger the currency is a test token issued by a Credence-controlled issuer account, labeled **tRLUSD** everywhere in the UI. Never call it RLUSD in the demo.

### Execution profile

Defines exactly what inference is being bought. It is the technical identity a receipt is checked against; it carries no delivery terms and no price.

| Field | Example | Notes |
| --- | --- | --- |
| `model_artifact_digest` | `sha256:…` of the weights file | Required |
| `tokenizer_digest` | `sha256:…` | Required |
| `adapter_digest` | `sha256:…` or null | Required field, may be null |
| `chat_template_digest` | `sha256:…` | Required |
| `runtime` | engine name and version | e.g. llama.cpp build tag |
| `precision` / `quantization` | `BF16` / `none` | Both required |
| `context_min_tokens` | 32768 | Integer |
| `sampling` | allowed temperature range, seed policy |  |

### Service class, procurement class and quote

Three separate objects, so identity, delivery terms and price never get tangled.

| Object | Defines | Key fields | Changes when |
| --- | --- | --- | --- |
| Service class | What a provider promises to deliver | execution profile digest, delivery mode (`interactive`, later `batch`, `reserved`), service tier (`fast` or `base`), jurisdiction and data residency, evidence tier, latency and availability commitments | The provider changes its promise |
| Procurement class | What a buyer will accept as a substitute | model family, minimum precision, minimum evidence tier, delivery mode, residency, allowed providers | The buyer changes its requirements |
| Quote | The price, for a limited time | service class, price per 1 million input tokens, price per 1 million output tokens, valid until | Every time the price moves |

A Verified Inference Unit (VIU) is a purchase under one service class, counted in input and output tokens. Facilities point at procurement classes, so several providers with different execution profiles can compete for the same purchase; receipts are still checked against the exact execution profile that was quoted.

### Reservation

An internal accounting hold against a facility for one pending purchase: facility, quote, maximum amount, created, expires, status (`held`, `settled`, `released`). It is not money locked on XRPL; provider payment still depends on Credence co-signing.

### Evidence tiers (canonical; the app must use exactly these)

| Tier | Name | What is proven | Who signs | At launch |
| --- | --- | --- | --- | --- |
| 0 | Requested | What the agent asked for, bound to a profile | Agent | Every purchase |
| 1 | Witnessed | Tier 0, plus the provider's response and declared model, captured by the Credence gateway | Gateway | Provider A (Nebius) |
| 2 | Bound and signed | Tier 1, plus the provider's own signature binding request, profile, response and token counts | Provider + gateway | Provider B |
| 3 | Measured | Tier 2, plus an attestation that the loaded weights match the profile | Hardware attestation | Roadmap only |

A provider that signs a Tier 2 receipt and served something else has made a provable false statement: a breach recorded against its reputation, not a fraud the system prevents. Say this plainly to judges.

### Facility

`facility_id`, borrower account, XRPL loan ID, limit, drawn amount, term and maturity date, spread in basis points, allowed providers, allowed procurement classes, minimum evidence tier, purpose (`inference`), owner approval digest, status (`pending`, `active`, `frozen`, `repaid`, `defaulted`).

### Purchase request

`facility_id`, provider, quote ID, maximum price, prompt digest (the prompt itself never leaves the agent and provider), nonce, expiry, agent signature.

### Receipt

Purchase request digest, profile digest, provider, response digest, input and output token counts, price, timestamps, nonce, evidence tier, provider signature (Tier 2), gateway signature, XRPL payment transaction hash, and the digest of the previous receipt on the same facility. That last field chains each facility's receipts, so a deleted or reordered receipt is detectable.

## Policy engine and verifier

The policy engine runs before money moves; the verifier runs after inference returns. Both are pure functions over typed objects, so the same inputs always give the same decision, and the attack lab calls the real code, not canned answers.

### Pre-purchase checks (policy engine), in this order

The first failing check stops evaluation and returns its reason code.

1. `SIG_INVALID` — the purchase request is not signed by the agent's current working key.
2. `KEY_REVOKED` — that key is no longer on the borrower account's signer list on the ledger.
3. `FACILITY_NOT_ACTIVE` — the facility is pending, frozen, repaid or defaulted.
4. `FACILITY_EXPIRED` — past maturity, or the request's own expiry has passed.
5. `PURPOSE_MISMATCH` — the request is not an inference purchase.
6. `PROVIDER_NOT_ALLOWED` — the provider is not on the facility's list.
7. `PROFILE_NOT_ALLOWED` — the quoted service class does not satisfy the facility's procurement class.
8. `TIER_TOO_LOW` — the provider cannot deliver the facility's minimum evidence tier.
9. `PRICE_ABOVE_MAX` — the quote exceeds the request's maximum price.
10. `OVER_LIMIT` — the quote would take drawn plus reserved amounts past the limit.
11. `NONCE_REUSED` — the nonce was already used on this facility.

Passing all eleven creates a reservation for the quoted maximum in one database transaction, so two simultaneous requests cannot both spend the last headroom. Reservations expire if delivery does not complete in time.

### Post-delivery checks (verifier)

1. `RECEIPT_SIG_INVALID` — the provider or gateway signature fails.
2. `BINDING_MISMATCH` — the receipt's request digest or profile digest differs from what was approved.
3. `RESPONSE_DIGEST_MISMATCH` — the delivered response does not hash to the receipt's response digest.
4. `TOKEN_COUNT_EXCEEDS_QUOTE` — billed tokens exceed the quoted maximum.
5. `CHAIN_BROKEN` — the previous-receipt digest does not match the facility's last receipt.
6. `REPLAYED_RECEIPT` — the receipt was already settled.

On pass, the settlement coordinator has ResearchBot and Credence co-sign a direct payment to the provider for the billed amount, then settles the reservation and releases any unused headroom. On fail, the reservation is released, the provider is not paid, and a dispute record is opened.

### Settlement and dispute policy

- **Settlement timeout:** a reservation expires 10 minutes after creation (tune it); a late delivery is not paid automatically and goes to dispute.
- **Credence offline after delivery:** the payment is owed; the coordinator settles it when it comes back, oldest first. Providers trust Credence to pay; say so plainly.
- **Verifier wrongly rejects a valid receipt:** the provider can resubmit with evidence; a manual override is itself a signed decision record.
- **Provider never delivers:** reservation expires; nothing was paid.

Credence is a trusted settlement party in this design: the ledger enforces the two signatures, not the receipt. Escrow-backed settlement is on the roadmap.

### Decision record

Every decision, accept or reject, is itself serialized, digested and stored with: inputs' digests, rule that fired, engine version, timestamp, and, for an accepted payment, the digest of the exact unsigned XRPL transaction (destination, amount, fee, sequence, network ID, last ledger sequence) that the policy key is then allowed to sign. The UI shows these records; it never shows a decision that does not have one.

## Credit and underwriting model

Credence prices each agent from evidence it can verify itself: settled receipts, on-ledger revenue and repayment history. All numbers below are starting parameters to tune, not claims.

### Score inputs

| Input | Source | Why it matters |
| --- | --- | --- |
| Verified spend volume | Settled receipts | Shows real, policy-compliant activity |
| Customer revenue | Payments into the agent's account, on-ledger | The repayment source |
| Repayment record | LoanPay history | On time, late, missed |
| Revenue to spend ratio | Both of the above | An agent earning less than it spends is a growing risk |
| Dispute rate | Verifier failures on its purchases | Signals a misbehaving agent or provider choice |
| Account age | Ledger | Thin-file penalty for new agents |

### Pricing

- Loan rate = vault base rate + risk spread. The spread comes from the score band.
- Bands to start with: **Proven** (base + 200 bps), **Established** (base + 600 bps), **New** (base + 1,200 bps, smallest limit).
- Limit = the lower of the band cap and 3 times trailing 30-day verified revenue. A new agent with no revenue gets the New band's fixed starter limit.
- Term: fixed and short. For the demo, configure payment intervals in minutes so a full loan life plays out live, and make the final payment fall inside the vault's investment phase.

### Repayment

Revenue allocation and loan payment are separate steps:

1. **Allocate:** when a customer payment lands, a set share (start with 50%) is earmarked for repayment inside the borrower account.
2. **Schedule:** the repayment scheduler reads the loan's state and computes what the ledger will accept now: the scheduled payment when due, or an early or full payment only if the loan's flags allow it.
3. **Pay:** ResearchBot and Credence co-sign the LoanPay for exactly that amount. Anything earmarked but not yet payable stays in the account until the next permitted payment.

A spike before Oct 12 confirms the rules: originate a loan, make an early partial payment, and inspect principal, interest, fees and the next amount due.

### Unit economics (say this to investors)

A 500 tRLUSD loan for 30 days at 12% a year earns about 4.93 in interest, which does not pay for underwriting, verification and losses. The agent facility is the wedge that proves the system. The business is a fee on every verified receipt plus larger financing: batch jobs tied to customer contracts, and provider or enterprise capacity commitments.

### Delinquency and default

1. Payment date missed: facility frozen (no new purchases), agent and owner notified.
2. Grace period passes: Credence marks the loan impaired, then defaulted, through the Lending Protocol's loan management transaction.
3. Loss absorbed by Credence's first-loss cover before lenders lose anything.
4. The default is written into the agent's credit history and its next score.

### Credit history record

Each agent has an append-only history of credit events (facility opened, draw, repayment, late, default, closed), each serialized and digested, so a future lender can verify it without trusting Credence's database.

## XRPL integration

Design change from our earlier chat: if loan proceeds land in an account the agent controls alone, the agent can pay anyone and Credence's policy is only advice. So the borrower account requires **Credence's co-signature on every spend**, enforced by the ledger itself.

### Accounts and keys

| Account | Master key | Day-to-day signing | Purpose |
| --- | --- | --- | --- |
| ResearchBot credit account (borrower) | Owner's Ledger device | Signer list: ResearchBot key + Credence policy key, quorum 2 (both must sign); regular key disabled | Holds loan proceeds and customer revenue; every payment out needs both signatures |
| Credence Loan Broker account | Credence key (1Password) | Single signature | Loan Broker object, first-loss cover, co-signs loans |
| Credence policy signer | Credence policy key | Signs only after the policy engine passes | Second signature on the borrower's spends |
| Vault | Created by Credence | Protocol-managed | Pools lender deposits |
| Lender accounts (2–3, scripted) | Local keys | Single signature | Deposit and withdraw |
| Provider A and B payout accounts | Provider keys | Single signature | Receive payment for verified inference |
| Customer accounts (scripted) | Local keys | Single signature | Pay ResearchBot for jobs |
| tRLUSD issuer | Credence issuer key | Single signature | Issues the test stablecoin on the local ledger |

What this gives you:

- The agent cannot spend without Credence: policy is enforced on-ledger, not just checked.
- Credence cannot spend without the agent: it holds only one of the two required signatures.
- The owner overrides both: the master key on the Ledger can act alone, change the signer list, or remove the agent instantly.
- Trade-off to state openly: if Credence is offline, the agent cannot spend. The owner's master key is the escape hatch.

### Vault lifecycle

The Lending Protocol update (LendingProtocolV1\_1) restricts all loans to closed-ended vaults. Phases change automatically at dates set when the vault is created, and the dates cannot be changed later.

| Phase | Deposits | Withdrawals | New loans | Repayments |
| --- | --- | --- | --- | --- |
| Subscription | Yes | Yes | No | Yes |
| Investment | No | No | Yes | Yes |
| Redemption | No | Yes | No | Yes |

For the demo, create the vault with a subscription window of a few minutes, an investment phase long enough for the whole loan life, then redemption. Rehearse the timings: a vault cannot be reopened, so each run creates a fresh vault. Confirm in the spike how phase dates behave on a standalone ledger whose ledgers you close by script.

[Source: XRPL Single Asset Vaults](https://xrpl.org/docs/concepts/tokens/single-asset-vaults)

### Transaction flow, end to end

1. **Setup (owner, Ledger):** create the borrower account, set the signer list, leave no regular key. Signed on the device.
2. **Vault (Credence):** create a closed-ended Single Asset Vault (XLS-65) in tRLUSD with its phase dates; lenders deposit during subscription.
3. **Broker (Credence):** create the Loan Broker object and deposit first-loss cover.
4. **Facility approval (owner, Ledger):** the owner signs the facility terms. Preferred: the device signs the borrower side of the loan directly. Fallback if the Ledger XRP app cannot show LoanSet: the device signs a small marker payment whose memo carries the facility terms digest, and Credence verifies it on-ledger first.
5. **Loan (LoanSet), investment phase only:** two separate authorizations must both hold: the borrower account's own signing rules, and the Loan Broker's counterparty signature on the exact terms. Principal is disbursed to the borrower account.
6. **Purchase:** policy engine passes and a reservation is held; the gateway forwards the call.
7. **Settlement:** the verifier passes; ResearchBot and Credence co-sign a direct payment to the provider; the reservation settles.
8. **Revenue:** customers pay the borrower account for completed jobs; 50% is earmarked for repayment.
9. **Repayment (LoanPay):** the repayment scheduler pays the amount the ledger will accept, co-signed.
10. **Revocation (owner, Ledger):** the owner removes ResearchBot from the signer list. Every later spend fails on-ledger, before Credence is even asked.

### Network

- Local standalone `rippled` in Docker, a **pinned release build** that includes the vault and lending amendments (including LendingProtocolV1\_1), with those amendments enabled in a new genesis ledger. Ledgers closed by a small script so time-based phases and payments advance.
- If a transaction fails, first decide whether it is configuration, amendment availability or a protocol defect. Do not switch to a development build until that is answered.
- Record for every spike run: server version, enabled amendment list, transaction hashes and the resulting ledger entries.
- Check at the Oct 20–22 bootcamp whether Devnet has the lending amendments; if so, run the final demo there too, because a public explorer link is more convincing.
- Token escrow is no longer on the critical path (settlement is pay-after-verification).

## Ledger device integration

The Ledger signs every decision that changes what the agent is allowed to do, and nothing routine. That is what makes it a real trust layer, not wallet branding.

### What the device signs

| Owner action | XRPL transaction | What the screen should show |
| --- | --- | --- |
| Set up the agent's account | SignerListSet | The two signers and quorum 2 |
| Approve a facility | LoanSet (borrower side), or the fallback marker payment | Limit, rate, term, and the terms digest |
| Raise a limit or change providers | New facility approval (same as above) | The new limit and what changed |
| Revoke the agent | SignerListSet removing ResearchBot | That ResearchBot is removed |
| Emergency sweep | Payment from the borrower account to the owner | Amount and destination |

The agent's ordinary purchases and repayments never touch the device.

### Integration

- Browser connection over WebHID from the owner console (Chrome or Edge), using Ledger's XRP app library. The bridge builds the unsigned transaction with `xrpl.js`, sends it to the device, and returns the signature to the API for submission.
- Develop and run automated tests against Speculos, Ledger's device emulator, so the test suite does not need a physical device; use the real device for rehearsal and the demo.
- Bring two devices to New York in case one fails.

### Spikes to finish before Oct 14

1. Which transaction types the current Ledger XRP app can **clear-sign** (show readable fields), especially SignerListSet and LoanSet. LoanSet is new, so it is the most likely to be unsupported or blind-signed only.
2. Whether signing for a local standalone network works (network ID handling).
3. Whether memos are displayed on the device, which the fallback approval depends on.
4. Round-trip time per signature, to plan the demo pacing.

If LoanSet cannot be clear-signed, use the marker-payment fallback and say so to Ledger's judges as a concrete gap you found; that is itself useful to them.

## Inference providers and agent runtime

Two real providers serving the same open model, at different evidence tiers, give the policy engine a real choice to make on stage.

### Providers

|  | Provider A | Provider B |
| --- | --- | --- |
| Service | Nebius Token Factory public API (no partnership claimed) | Local model on the Mac mini M4 |
| Evidence tier | 1, Witnessed: Credence's gateway records request, response and declared model name | 2, Bound and signed: a signing sidecar signs every receipt |
| Execution profile | Declared: model name maps to the reference weights digest; runtime fields unknown and marked so | Full: the sidecar hashes the weights file at startup and publishes runtime, precision and quantization |
| Pricing | Mirror its published per-token rates, input and output separately, dated | Set by you, deliberately undercutting A |
| Payment | Internal record: XRPL payment to a Credence-held account labeled "Provider A settlement"; Nebius is actually paid through its own billing | Direct payment to its own payout account |

Pick a model that Nebius serves **and** that fits the Mac mini's memory at the stated precision; check Nebius's console catalog this week. If none overlaps, run two different VIU classes and show the policy engine refusing to treat them as interchangeable.

State plainly: Provider B hashing its weights file proves which file it loaded from disk, not what is in GPU memory. That is Tier 2, not Tier 3.

**Two facilities, two real outcomes.** On a facility whose procurement class accepts Tier 1, both providers qualify and the cheaper quote wins: real price competition. On a facility requiring Tier 2, only Provider B qualifies and wins on evidence. Where only one provider qualifies, the UI says "1 qualifying provider" rather than implying a market.

**Settlement gap with Provider A.** Credence pays Nebius off-ledger with its own account and records an on-ledger settlement for the agent. Those are two payments, and Credence carries the difference. A real launch needs provider agreements or a prefunded account with reconciliation.

### Purchase protocol (x402-style, carried over from AgentLevy)

1. ResearchBot asks the gateway for its facility's procurement class; the gateway answers **402 Payment Required** with a quote from each qualifying provider.
2. ResearchBot picks the cheapest qualifying quote and signs a purchase request.
3. The policy engine runs the pre-purchase checks; on pass, a reservation is held.
4. The gateway forwards the call to the provider and collects the response and receipt.
5. The verifier runs; on pass, the settlement coordinator pays the provider with a co-signed payment and stores and chains the receipt.

### ResearchBot

- A Python agent that sells research briefs: a customer submits a topic and a price, ResearchBot plans the work, buys inference through Credence for every model call (its own reasoning included), writes the brief, delivers it and invoices.
- Its key is fetched at runtime through 1Password, as in Sever, never written to disk.
- A job generator with three scripted customers keeps steady demand during the demo, so revenue and repayment happen live rather than being narrated.
- A spend budget per job keeps ResearchBot profitable on paper; if a job would cost more than it pays, ResearchBot declines it and logs why. Judges like seeing an agent say no.

## Serialization and receipts

The Kessai serializer, JCS (RFC 8785) + NFC, is the only serializer in Credence. No placeholder, no JavaScript copy. Today it exists only as a dataset-specific `canon.py` copied into the custody repo; before Credence Core starts, extract it into one general package (working name `kessai-canon`) exposing canonical bytes and a digest for any object, with `dataset_id` built on top and all existing vectors still passing.

- **Name it exactly.** Full name, once per document: "Kessai serializer: JCS (RFC 8785) with NFC normalization, bound using the SCITT Canonical Payload Binding `jcs` construction." Short form everywhere else: "JCS (RFC 8785) + NFC". Lowercase `jcs` only as the literal identifier value in CPB fields and code. Never UOR-ADDR, never `uor-jcs-nfc`. State in the spec that NFC is not part of RFC 8785: it is applied to every string value and key before JCS serialization, so a plain JCS implementation can produce different bytes for non-normalized input.
- **One library, many object types:** add Credence classes (execution profile, purchase request, receipt, decision record, credit event) beside the custody and dataset classes, sharing the one serializer.
- **Conformance vectors:** for each new class, commit input JSON, expected canonical bytes and expected digest. Cover NFC normalization, key ordering with non-ASCII keys, large integers, and integer amounts. These run in CI and are published with the verifier.
- **Signatures:** Ed25519 over the canonical bytes (PyNaCl, as in Sever). Each signed object carries the signer's public key identifier and the digest it signed.
- **Digest display:** the UI shows the first 8 hex characters with a copy button for the full digest; the verifier page always shows the full value.
- **Public verifier:** a standalone page and a command-line tool. Given a receipt, it recomputes every digest and checks every signature and binding, showing each check as pass or fail with the reason code. Anyone can run it without trusting Credence.
- **Disclosure line** (footer of the verifier and the deck): Kessai's serializer is related to the UOR Foundation's UOR-ADDR work but is not claimed conformant to it; Maura Clark leads Kessai and contributes to the UOR Foundation.

## User interface

Keep the existing Credence Markets design; rework its 13 pages into four role consoles plus shared tools. Every number on screen links to the record that produced it: a transaction hash, a receipt or a decision record. If a value has no record, it does not appear.

### Global rules

- **Status badges are computed, never typed.** Replace the current "Live MVP" and "XRPL connected" labels with a badge driven by the API's health check: network name (Local ledger or Devnet), last ledger index, seconds since last close.
- **tRLUSD** shown wherever currency appears; a tooltip explains it is a test token.
- **Preview banner** on any page with illustrative data: "Preview, illustrative data, not live."
- **Live feed** over WebSocket on the consoles, newest first, each row expandable to the full record.
- **Ledger prompt component**, one shared component with five states: connect device, open XRP app, review on device, signed, rejected or error.

### Screens

| Screen | For | Shows | Actions | From existing page |
| --- | --- | --- | --- | --- |
| Home | Everyone | One-line pitch, live network badge, the four roles as entry cards | Start the guided demo | Home |
| Overview | Judges | Architecture diagram, the money path, what is live vs preview | None | Overview |
| Owner console | Owner | Agents, their facilities, limit, drawn, status, recent activity | Create agent, approve facility, raise limit, revoke, emergency sweep, all through the Ledger prompt | New |
| Agent operations | Owner and Credence | Jobs, purchases, policy decisions with reason codes, facility gauge (limit, drawn, reserved), revenue vs spend | Pause agent | Machine Credit |
| Lender view | Lenders | Vault size, utilization, share value, yield, first-loss cover, loans, losses | Deposit, withdraw | Credit |
| Underwriting | Credence | Score, band, spread, each score input, credit history timeline | Approve or decline a facility request, freeze, mark impaired or defaulted | New |
| Inference market | Agents, judges | Service classes, procurement classes and live quotes per provider, with the count of qualifying providers, input and output prices, Fast or Base tier, evidence tier badge | Request a quote | Inference Market |
| Providers | Providers, judges | Execution profiles with full digests, evidence tier, receipts signed, disputes, reputation | Publish or update a profile | Providers + Reputation (merged) |
| Receipts | Everyone | Receipt explorer, chained per facility | Open the verifier on any receipt | Receipts |
| Verifier | Everyone | Each check with pass or fail and reason code, full digests | Paste or upload a receipt | New |
| Attack lab | Judges | The attack suite, each case's inputs and the engine's real decision | Run one case or all | New |
| Reservations | Judges | Preview of booking future inference windows | None (preview) | Reservations |
| Price index | Judges | Credence's own purchase prices over time plus dated public reference prices | None (preview) | Price Index |
| Roadmap | Judges | Forwards, secondary market, AMM, Tier 3 evidence, UOR-ADDR conformance | None | Forwards + Secondary Market (moved here) |
| Settings | Operator | Network endpoint, public keys of each actor, engine and serializer versions | Switch network | Settings |

### Guided demo mode

A stepper overlay that walks the money path in order (fund vault, approve on Ledger, borrow, buy, verify, earn, repay, revoke), each step triggering the real action and pausing on its result. It is the safety net if live narration goes wrong.

## Security and key management

Each key does one job, and no single compromised key can drain the borrower account.

| Key | Stored | If compromised |
| --- | --- | --- |
| Owner master key | Ledger device only | Owner's own risk; it can override everything, so it never touches a computer |
| ResearchBot key | 1Password, fetched at runtime | Attacker still needs Credence's signature to spend; owner removes it on the Ledger |
| Credence policy key | 1Password, used only by the policy service | Attacker still needs ResearchBot's signature; rotate via owner-approved signer list change |
| Credence Loan Broker key | 1Password | Can mismanage the broker and cover; cannot touch borrower funds |
| Provider B signing key | On the Mac mini, sidecar only | Forged receipts possible until rotated; disputes trace back to it |
| tRLUSD issuer key | 1Password | Test token only; never reuse this pattern for real money |

### Rules

- No private key in the repo, the environment file, logs or the browser. CI fails the build on a key-shaped string.
- The policy service signs only an XRPL transaction whose full unsigned form (destination, amount, fee, sequence, network ID, last ledger sequence) hashes to the digest in the engine's decision record, checked at signing time. A digest of the inference request alone is not enough. This stops a tampered transaction slipping through between decision and signature.
- Nonces and request expiry stop replay; the receipt chain stops silent deletion.
- Prompts and responses are stored only as digests by default; full text stays with the agent and provider unless the owner turns on retention.
- Rate limit the gateway per agent, so a looping agent burns its budget slowly and visibly.
- Every API route requires the caller's signature; the web app uses a session tied to the role it is showing.
- Dependency pinning and a lockfile for both Python and the web app; no new dependency during the judged window without a reason in the commit message.

## Testing

Four layers, all automated, all run before every checkpoint. The attack lab in the UI runs the same attack suite, so what judges see is what CI tests.

1. **Unit tests:** every policy rule and verifier check, accept and reject paths.
2. **Conformance vectors:** canonical bytes and digests for every serialized class (see Serialization and receipts).
3. **Integration tests:** against the local `rippled` and Speculos, every transaction in the flow from vault creation to revocation.
4. **End-to-end:** ResearchBot runs a full loan life (borrow, 20+ purchases, revenue, repayment, close) unattended.

### Attack suite

Each case names the layer that must stop it. "Ledger" means the transaction fails on-ledger even if Credence were bypassed.

| Case | Attack | Expected result | Stopped by |
| --- | --- | --- | --- |
| Valid purchase | Tier 2 purchase within every limit | Accepted, receipt chained, provider paid | (control case) |
| Cheapest qualifying wins | Tier 1 facility, both providers quote | Lower qualifying quote selected | Gateway + policy engine |
| Wrong provider | Buy from a provider not on the facility | `PROVIDER_NOT_ALLOWED` | Policy engine |
| Wrong artifact | Service class whose profile is not in the procurement class | `PROFILE_NOT_ALLOWED` | Policy engine |
| Wrong precision | Same model, different quantization | `PROFILE_NOT_ALLOWED` | Policy engine |
| Tier too low | Tier 1 provider on a facility requiring Tier 2 | `TIER_TOO_LOW` | Policy engine |
| Overspend | Purchase past the remaining limit | `OVER_LIMIT` | Policy engine |
| Race to the limit | Two purchases at once for the last headroom | One reservation held, one `OVER_LIMIT` | Reservation ledger |
| Replayed request | Same nonce resubmitted | `NONCE_REUSED` | Policy engine |
| Replayed receipt | Settle the same receipt twice | `REPLAYED_RECEIPT` | Verifier |
| Expired facility | Purchase after maturity | `FACILITY_EXPIRED` | Policy engine |
| Revoked agent | Purchase after the owner removed the agent | `KEY_REVOKED`, and the payment fails on-ledger | Policy engine and ledger |
| Agent goes around Credence | Agent submits a payment signed only by itself | Rejected for insufficient signatures | Ledger |
| Debt without approval | Loan requested with no owner Ledger approval | Credence refuses its counterparty signature; loan never exists | Loan Broker signer |
| Loan in the wrong phase | LoanSet during subscription or redemption | Rejected | Ledger |
| Tampered transaction | Payment altered after the engine approved it | Policy signer refuses: digest mismatch | Policy signer |
| Forged receipt | Receipt with an invalid provider signature | `RECEIPT_SIG_INVALID`, provider not paid, reservation released | Verifier |
| Swapped response | Response that does not match the receipt's digest | `RESPONSE_DIGEST_MISMATCH` | Verifier |
| Over-billing | Billed tokens above the quote | `TOKEN_COUNT_EXCEEDS_QUOTE` | Verifier |
| Deleted receipt | Remove a receipt from the middle of a chain | `CHAIN_BROKEN` | Verifier |
| Late delivery | Provider delivers after the reservation expired | Not paid automatically; dispute opened | Settlement coordinator |
| Impermissible repayment | Sweep tries to pay an amount the loan does not allow | Scheduler sends only the permitted amount | Repayment scheduler |
| Credence offline | Policy service down | Agent cannot spend; owner sweeps funds with the Ledger | Ledger (escape hatch) |

### Rehearsal

Three full timed rehearsals on the demo laptop, with Wi-Fi off, before Oct 24. The demo must run with no internet except Provider A, and the guided demo mode must recover if Provider A is unreachable by switching to Provider B.

## Timeline and milestones

Every risky integration is proven by Oct 14, so the last ten days go to building and the judged window goes to adding and polishing, not discovering.

&#91;embedded content: build timeline · 10 workstreams and milestones\]

The foundation baseline is tagged on Oct 19 and a second immutable tag is taken just before the Oct 24 start, so judges can see exactly what was built in the window.

### Exit criteria per milestone

- [ ] **XRPL network spike (Oct 12):** local `rippled` (pinned release) runs a closed-ended vault through all three phases, Loan Broker and cover, LoanSet with a 2-of-2 borrower and the broker's counterparty signature, and both scheduled and early LoanPay, from a script; hashes and ledger entries recorded.
- [ ] **Ledger device spike (Oct 14):** device signs SignerListSet and the facility approval (LoanSet or the marker payment) on the local network; findings written down.
- [ ] **Policy engine and verifier (Oct 18):** all rules and checks implemented; attack suite passes; conformance vectors committed.
- [ ] **XRPL money flows (Oct 19):** a full loan life runs from a script with real co-signed provider payments, revenue earmarking and scheduler-driven repayment.
- [ ] **Foundation baseline tag (Oct 19):** tagged, dated, pushed.
- [ ] **Consoles and ResearchBot (Oct 22):** all four consoles live on the real API; ResearchBot completes a loan life unattended.
- [ ] **Rehearsals (Oct 23):** three timed runs, backup video recorded, pre-judging tag taken.
- [ ] **Hackathon window (Oct 24–25):** underwriting console and credit history polish, provider disputes and reputation, price index and reservations previews, attack lab polish; feature freeze at hour 28.

## Demo script and judging alignment

Open with the agent's problem, not the mechanism: an AI agent with paying customers can't get working capital, and nobody can safely lend to it. Then show one loan's whole life, live. Timings assume a 4-minute slot; confirm the real length at the Oct 14 briefing.

| Time | Beat | On screen | Says |
| --- | --- | --- | --- |
| 0:00 | The problem | Home | Agents earn money but can't borrow; lenders can't see what they'd be financing |
| 0:20 | Lenders fund the pool | Lender view | Vault funded in tRLUSD during subscription, then it locks for investment; Credence's first-loss cover in front of lenders |
| 0:40 | Owner approves on the Ledger | Owner console + device on camera | The device shows limit, rate and term; one press |
| 1:10 | ResearchBot borrows and works | Agent operations | Loan lands; jobs arrive; purchases flow; each one a policy decision |
| 1:50 | Two providers, two tiers | Inference market | On a Tier 1 facility the cheaper qualifying quote wins; on a Tier 2 facility only B qualifies, so B wins on evidence |
| 2:20 | Attack lab | Attack lab | Run all cases: every attack stopped, and some stopped by the ledger itself |
| 2:50 | Earn and repay | Agent operations + Underwriting | Customer pays, sweep repays, score improves, next spread tightens |
| 3:20 | Revoke | Owner console + device | One press on the Ledger; next purchase fails on-ledger |
| 3:40 | Close | Overview | Constitution not cashier; what's live, what's next |

### What each judge is looking for, and where they see it

| Judge group | What they want | Where it lands |
| --- | --- | --- |
| XRPL Commons, Lending & Borrowing | Real use of the Lending Protocol and vaults; tooling the ecosystem lacks | Credence as Loan Broker doing underwriting the protocol leaves off-chain; full loan life on-ledger |
| Ledger | The device as a real trust layer | Owner approvals and revocation only possible on the device; the gap list if LoanSet can't be clear-signed |
| Agentic Finance audience, incl. sponsor Mastercard (judging role unconfirmed) | Controlled autonomous spending | Purpose-bound credit; every purchase provably authorized and limited |
| Investors | A business, not a demo | Four parties, a revenue model (fee per verified receipt, spread, and financed batch jobs next), a roadmap to forwards |

The recording: capture a clean full run on Oct 23 as a backup video, in case the live demo fails on stage.

## Claims discipline and regulatory boundary

Every sentence in the deck, UI and pitch must match what the code does on Oct 25.

### Say / don't say

| Say | Don't say |
| --- | --- |
| Kessai serializer: JCS (RFC 8785) + NFC | UOR-ADDR, or "UOR-compatible" |
| Tier 2: the provider signed what it says it served | "Proves the exact weights ran" |
| Tier 1: Credence witnessed Nebius's response | Anything implying Nebius signed or attested it |
| Runs on a local XRPL ledger with the lending amendments enabled | "Live on mainnet" |
| tRLUSD, a test token | RLUSD |
| Uses Nebius Token Factory's public API; Credence pays Nebius through normal billing | Any partnership with Nebius, or that Nebius accepts XRPL payment |
| Providers are paid after verification; Credence is a trusted settlement party | "Trustless settlement" |
| One qualifying provider per class today; the mechanism handles many | A busy market |
| Inference reservations, physically delivered (preview) | Futures, derivatives, an exchange, "trading" |
| Price index from Credence's own purchases plus dated public prices (illustrative) | A market price or benchmark |
| Credence is a Kessai product; Maura also contributes to the UOR Foundation | Anything implying the Foundation endorses Credence |

### Regulatory boundary for the hackathon

- No secondary trading, offsetting, cash settlement, margin or leverage on inference contracts. Reservations are bought by someone who wants the inference and delivered by someone who has it.
- Lending runs only on test tokens on a local ledger. A real launch needs counsel on lending licensing, the Loan Broker role, and whether VIU contracts are commodities under CFTC rules.
- Say once in the pitch that this is a prototype on test assets and that the production path includes legal review. Judges respect it; it costs ten seconds.

## Risk register

The four that could sink the demo are the lending flow on a runnable ledger (vault phases included), LoanSet with a 2-of-2 borrower, the Ledger's support for the owner's transactions, and repayment rules. The Oct 12 and Oct 14 dates are real stop/go gates.

| Risk | Severity | Check by | Fallback |
| --- | --- | --- | --- |
| Lending flow won't run end to end on the pinned build (vault phases, broker, loan, repayment) | Critical | Oct 12 | Diagnose configuration vs amendment vs defect; ask XRPL engineers; only then consider another build |
| 2-of-2 borrower can't sign LoanSet or LoanPay, or the broker counterparty signature doesn't compose | Critical | Oct 12 | Owner's Ledger signs loan transactions as master key; spends stay 2-of-2 |
| Vault phase dates misbehave on a script-closed standalone ledger | Critical | Oct 12 | Longer phase windows; fresh vault per rehearsal |
| Ledger XRP app can't clear-sign LoanSet or SignerListSet | High | Oct 14 | Marker payment with terms digest in the memo; report the gap to Ledger's judges |
| Repayment amounts rejected by loan rules | High | Oct 12 | Scheduler pays only scheduled amounts; earmark the rest |
| Provider A settlement gap (Nebius paid off-ledger) | High (commercial) | Now | Label it in the UI and pitch; production needs provider agreements |
| Providers can't be compared (no shared model) | Medium | Oct 12 | Show two service classes; the engine refuses to treat them as interchangeable |
| Provider A unreachable on venue Wi-Fi | Medium | Rehearsals | Guided demo switches to Provider B; phone hotspot |
| Ledger device fails on stage | Medium | Rehearsals | Second device; recorded backup video |
| Scope too large for the time | High | Daily | Gates are stop/go; feature freeze at hour 28; unfinished work goes to the Roadmap page |
| Overclaiming (UOR-ADDR, mainnet, weights proof, trustless settlement) | High | Before every rehearsal | Say / don't say table |
| Rules discount pre-built code | Medium | Oct 14 briefing | Dated commits and baseline tag show what was new |

## Roadmap slide and open questions

### Roadmap slide

1. **Financed batch inference:** loans sized to a customer contract and a batch job, the likely first commercial product.
2. **Escrow-backed settlement:** ideally Smart Escrows (XLS-100) checking receipt conditions on-ledger, so Credence stops being the party that releases payment.
3. **Credence Forward:** reservations become tradable forwards once counsel clears the structure.
4. **Capacity pools:** XRPL AMM pools for standardized service classes.
5. **Tier 3 evidence:** attested GPU execution proving the loaded weights.
6. **UOR-ADDR conformance:** run the Kessai serializer against the Foundation's reference vectors.
7. **Native agent accounts:** move to XRPL sub-accounts or agent identity objects if those proposals ship.
8. **Mainnet:** real RLUSD, once the Lending Protocol and Single Asset Vault amendments activate.

### Questions for the Oct 20–22 bootcamp

- [ ] Can a borrower account with a 2-of-2 signer list sign LoanSet and LoanPay, alongside the broker's counterparty signature?
- [ ] Can a regular key or signer list ever disable the master key, or only the master key itself?
- [ ] Are the lending and vault amendments, including LendingProtocolV1\_1, on Devnet yet?
- [ ] How do closed-ended vault phase dates behave on a standalone ledger closed by script?
- [ ] Which repayments (early, partial, overpayment) does a loan accept by default, and which flags change that?
- [ ] Which loan management actions (impair, default) does the Loan Broker control, and what timing rules apply?
- [ ] Given Permission Delegation and the sub-account and agent identity proposals, what should stay application-level when an agent spends from a loan?

### Questions for the Oct 14 briefing

- [ ] Can one project enter a main track and the Ledger bounty together?
- [ ] How is pre-existing code treated in judging?
- [ ] Demo slot length and format (live, recorded, both)?
