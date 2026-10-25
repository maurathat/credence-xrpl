# Narration cues (3:00)

The cues below match `stages.mjs`, which prints them in the terminal at each stage and uses them as captions in
recordings. Say them in your own words. Times are the planned stage lengths for `rehearse` and `record`.

| Time | Stage | On screen | Say |
| --- | --- | --- | --- |
| 0:00–0:22 | **1 · Open Claw Street and the Overview** | Animated logo on `/`, **Open terminal**, then Overview: utilization chart, market stack, the seeded-data notice | "Claw Street is a marketplace where AI agents buy inference under rules they can't rewrite. It's powered by Credence." · "This is the operator's overview: one credit facility, its utilization, and recent purchases." · "Everything on this site is a hackathon prototype with seeded demo data, and the page says so." |
| 0:22–0:52 | **2 · Marketplace** | Provider offers for ORD-2261; strategy switches **Cheapest → Verified → Credit-eligible**; the "Best under …" line | "Same order, five provider offers: price per verified inference unit, latency, evidence tier, residency." · "The routing strategy is the agent's procurement policy. Watch which offer becomes Best." · "Cheapest picks on price. Verified requires Tier 2-plus evidence. Credit-eligible only allows providers on the facility's allowlist: the policy, not the agent, decides." |
| 0:52–1:24 | **3 · Agent purchase** | Credit page: **Reset**, then **Simulate inference purchase**; four checks pass; "Facility drawn +10.46 tRLUSD (Credence accounting — not an on-chain payment)" | "Now the agent buys. This runs the site's purchase simulation in the browser; no provider is called and nothing is paid." · "Four checks: the agent's signature and limit, the provider-signed receipt, the request-response-profile binding, then settlement." · "All pass, and the facility is drawn 10.46 tRLUSD. The page says it plainly: Credence accounting, not an on-chain payment." |
| 1:24–1:50 | **4 · Credit & payments** | Facility panel 351.94 available / 148.06 outstanding; utilization 29.6%; **Simulate attack** (a random one of six cases) → "Rejected — no draw, balances unchanged."; Facility requests | "In the facility panel, available credit went from 362.40 to 351.94 tRLUSD, outstanding to 148.06, and utilization from 27.5% to 29.6%." · "Now an attack. The site picks one of six attack cases at random; whichever it is, it's rejected, nothing is drawn and balances don't change." Then read the case the terminal prints (`▸▸`), for example "Over limit: 900 units, 392.40 tRLUSD, is more than the available credit. Stopped at authorization." · "tRLUSD is a test token. These are Credence's accounting records, not XRPL payments." |
| 1:50–2:12 | **5a · Evidence** | Receipts table (digests, provider signature, evidence tier); Evidence ladder | "Every purchase leaves a receipt: request, response and execution-profile digests, the provider's signature, the evidence tier." · "Tier 2 means the provider signed what it says it served. It is not proof that exact weights ran; the page says that too." · "The evidence ladder shows which levels are present for these receipts and which are roadmap." |
| 2:12–2:24 | **5b · Activity** | Overview → Recent receipt activity (PASS / REJECT) | "And the history: each request with its outcome. Passes, and rejections where the policy said no: wrong profile, over limit." |
| 2:24–2:50 | **6 · XRPL Testnet proof** | testnet.xrpl.org → **Detailed**: "validated in ledger 21807427", 0.000001 XRP delivered, the memo | "Separately, we proved XRPL connectivity on the public Testnet: one native XRP payment of a single drop." · "Validated in ledger 21807427. The memo says what it is: a connectivity proof, not governed by Credence procurement authorization." · "It's not a Credence-governed marketplace settlement. That's the next integration step." |
| 2:50–3:00 | **7 · Close** | The animated Claw Street logo | "Claw Street, powered by Credence: policy decides, the agent signs, Credence verifies and accounts. Thank you." |

## Attack cases (stage 4)

**Simulate attack** picks one of these at random. The runner prints the matching line, and shows it as a caption
in recordings.

| Case on screen | Say |
| --- | --- |
| Over limit | "Over limit: 900 units, 392.40 tRLUSD, is more than the available credit. Stopped at authorization." |
| Wrong provider | "Wrong provider: Helios Grid isn't on the facility allowlist. Stopped at authorization." |
| Expired key | "Expired key: the agent's delegate key has expired, so only the root can re-delegate. Stopped at authorization." |
| Replayed receipt | "Replayed receipt: that receipt's request digest was already used by REQ-4815. Stopped at the receipt check." |
| FP8 vs BF16 | "Wrong precision: the receipt declares FP8, the facility approves BF16. Stopped at verification." |
| Wrong model | "Wrong model: the receipt declares OpenCoder-34B, the facility approves Meridian-70B-Instruct. Stopped at verification." |

## Don't say

- That a provider was paid, or that any payment settled on XRPL from this marketplace. The purchase is a browser
  simulation and the draw is Credence accounting.
- That the Testnet transaction is marketplace settlement or was authorized by Credence's policy engine.
- "RLUSD" for the facility's money: it's **tRLUSD, a test token**. Some site labels say "Target: RLUSD settlement on
  XRPL"; don't repeat that as a fact.
- Nebius or Qwen: they do not appear on this site.
- That the site's numbers are live. They are seeded demo data, as the page states.
- "Trading", "exchange", "futures" or "derivatives". The Forwards and Secondary Market pages are concept pages; the
  demo doesn't open them.
- That a receipt proves the exact model weights ran.

## If asked

- **"Is the purchase real?"** "On this site it's a simulation of the Credence Core flow. The offline Credence
  service runs real signed purchases with mock providers; we can show that locally."
- **"Who signed the Testnet payment?"** "A test account on the public Testnet, for a 1-drop connectivity proof. It's
  separate from Credence's 2-of-2 payment design, which is still under independent review."
