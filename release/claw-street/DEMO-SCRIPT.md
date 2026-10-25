# Three-minute demonstration and judge walkthrough

Setup before the clock starts: a fresh demo dataset ([`DEMO-RESET.md`](DEMO-RESET.md)), the API and web app running,
the agent signed in, and a terminal open for the agent CLI. Every number below is what the release candidate produced
on a fresh dataset ([`TEST-SUMMARY.md`](TEST-SUMMARY.md)). Amounts are micro-tRLUSD; tRLUSD is a test token.

## Script (about 3:00)

**0:00 — Overview (`/`).** "Claw Street is a marketplace where AI agents buy inference under a policy they can't
rewrite. It's powered by Credence. Everything you see runs offline with mock providers. The header says so: API
available (offline), XRPL not connected, no payments submitted."

**0:20 — Marketplace (`/market`): competing offers.** "Two providers offer the same model family, Qwen3-30B-A3B. Nebius
Token Factory offers Tier 1 evidence at a maximum of 86 micro-tRLUSD per purchase. A local Qwen provider offers Tier 2
at 436. The table shows Credence Core's real policy dry run for each scenario. Scenario A, a general Tier 1 policy:
both are eligible, so Core routes to the cheaper one, Nebius. Scenario B, a controlled policy requiring Q8_0 and
Tier 2: Nebius is refused with `PROFILE_NOT_ALLOWED`, because its profile doesn't assert a quantization."

**0:50 — Agent Purchase (`/procure`): agent demand and Core enforcement.** Choose **Scenario A**, then **Run Core policy
dry run**. "The agent states what it may buy: model, prompt, providers, evidence floor, facility." Type 1000 into the
price cap. "A buyer price cap is a preview: Prepare is disabled, because the purchase path can't carry the cap yet."
Clear it and choose **Prepare purchase**.

**1:15 — The agent signs outside the browser.** In the terminal, run
`… agent.cli --demo-dir <dir> sign --preparation <prep-id> --prompt "<prompt>"`, paste the JSON and submit. "The
browser never sees the agent's key. The agent verifies what was prepared before it signs." The lifecycle advances:
Core evaluation, prepared, signed, offline provider executed, then receipt verified with accounting settled.

**1:45 — Retrieve and acknowledge.** Run `authorize --action retrieve`, paste, then `authorize --action acknowledge`.
"The response goes only to the agent that authorizes it."

**2:00 — Credit & Payments (`/credit`): accounting.** "Facility `facility-market-general`: drawn went from 0 to 2
micro-tRLUSD. Core held the quote's maximum, 86, while the purchase ran, then replaced the hold with the verified
charge. The exposure table shows each step. The XRPL payment column says *not submitted*. This is accounting, not an
XRPL payment."

**2:20 — Evidence (`/receipts`): tiers and receipts.** Open the new receipt. "Each receipt binds the purchase request,
the execution profile, the provider and the response digest. Core recomputes the digest and checks the gateway
signature when you open it. Tier 1 means Credence's gateway witnessed the response and signed the receipt; the
provider signs nothing. Tier 2, from Scenario B, means the provider signed what it says it served. No tier proves that
the exact weights ran." Point at **Limitations**.

**2:45 — Activity (`/activity`).** "Every event is journaled with its own timestamp: intent, policy decision,
reservation, delivery attempt, provider, accounting settlement, retrieval, acknowledgement. Below are recorded
offline runs of the failure paths: no qualifying offer, release, unknown, dispute, late delivery."

**2:55 — Close.** "Policy decides, the agent signs, Credence verifies and accounts. XRPL settlement, QuickNode,
t54 purchases, Verifiable Intent and x402 payments are shown as not connected, because they aren't."

## If something goes wrong

- **The browser flow stalls:** run the same purchase from the terminal with
  `agent.cli … purchase --preset scenario-a-general --prompt "Synthetic Credence test prompt."`. The result appears in
  Activity and Credit.
- **"401 SESSION_EXPIRED":** the 12-hour token has lapsed. Re-initialize ([`DEMO-RESET.md`](DEMO-RESET.md)).
- **Numbers aren't 0 → 2 / 0 → 8:** the dataset isn't fresh. Drawn rises by the receipt price on every run (2 for A,
  8 for B), so it's still correct, just not from zero.

## Walkthrough reference (what each screen shows)

| Screen | Route | Shows | Key disclosure on screen |
| --- | --- | --- | --- |
| Overview | `/` | Providers listed, cheapest maximum cost, scenario qualification counts, evidence tiers, not-live panel | "API: available (offline)", "XRPL: not submitted", "tRLUSD: synthetic test token" |
| Marketplace | `/market` | Both offers, execution profiles, configured demo prices, Core eligibility per scenario | "Configured demo price … not Nebius's billed price"; "no auction or bidding" |
| Agent Purchase | `/procure` | Procurement policy form, Core dry run, offer comparison, signed purchase lifecycle | "Providers: offline mocks"; price cap "preview only" |
| Credit & Payments | `/credit` | Facility limit, drawn, held, headroom; exposure by step; payments table | "Accounting only · no XRPL loan"; XRPL payment "not submitted" |
| Evidence | `/receipts` | Evidence tiers, receipts, recomputed verification, receipt chain, canonical receipt | "No tier proves that exact model weights ran"; "a verified receipt is accounting evidence, not an XRPL payment" |
| Activity | `/activity` | Journaled purchase events, newest first; recorded offline failure-path runs | "Accounting settled (accounting, not XRPL)" |

Screenshots of all six screens from the tested run: see [`TEST-SUMMARY.md`](TEST-SUMMARY.md#screenshots).
