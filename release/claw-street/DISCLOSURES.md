# Sponsor and third-party integration disclosures

What this build does with each named technology, and what it does not. "Not connected" means no code path in this
release reaches the service.

| Technology | What this build does | What it does not do |
| --- | --- | --- |
| **XRP Ledger** | Shows XRPL status truthfully: every purchase reads "XRPL payment: not submitted". The UI and API state that no XRPL adapter is connected | No XRPL transaction is built, signed or submitted. No vault, loan, trust line or SignerList exists. XRPL payment code (2-of-2 multisign, payment journal) is under independent review on a separate branch and is **not in this release**. Nothing runs on mainnet |
| **tRLUSD** | The unit of account: integer micro-tRLUSD. tRLUSD is a test token | It is not RLUSD. No RLUSD is held, paid or settled by this build |
| **Nebius Token Factory** | Scenario A uses an **offline mock** of Nebius Token Factory; no call is made. Recorded evidence from a real 2026-10-19 run is shown and labelled "Recorded live evidence" | No live Nebius call in this build. When live calls are enabled, Credence uses Nebius Token Factory's public API and pays Nebius through normal billing. There is no partnership with Nebius, and Nebius does not accept XRPL payment. Tier 1 means Credence witnessed Nebius's response; Nebius signs and attests nothing |
| **Qwen (Qwen3-30B-A3B-Instruct-2507, Q8_0)** | Scenario B uses an **offline mock** of a local Qwen provider with a TEST signing key. A recorded real local run is shown as recorded evidence | No local model runs in the demo. Real-model tests are opt-in (`CREDENCE_LOCAL_QWEN=1`) |
| **x402 and t54** | Real x402 v2 payment challenges from two merchants listed in the xrpl-ai.org directory (Heurist, Monid) were **recorded** on 2026-10-25 UTC, from one unpaid, unauthenticated request each, and are kept as fixtures. t54's facilitator documentation and `/supported` responses were recorded the same way. The adapter parses them, assesses compatibility and prepares payment proposals offline | No paid inference and no t54 purchase. It never signs, submits, or calls a facilitator's `/verify` or `/settle`. The recorded challenges ask for RLUSD on XRPL Mainnet (`xrpl:0`); that is the merchants' observed term, assessed read-only and refused for execution. A prepared proposal is not authority to pay |
| **QuickNode** | Listed as not connected | No QuickNode endpoint is configured or called |
| **Ledger hardware signer** | The Credit page shows the intended authority split, labelled "planned" | No device, Speculos or owner approval flow is connected |
| **Verifiable Intent** | Listed as not connected | No credential is issued or checked |
| **Lovable** | The local web UI was built with Lovable; its source is the `creds-for-ai` branch `release/claw-street-ui`, pinned by the parent Git submodule | The public Lovable site (`creds-for-ai.lovable.app`) is a separate browser simulation with seeded data. It does not run this local backend or its signature verification |
| **Kessai serializer** | Canonical bytes and digests use the Kessai serializer: JCS (RFC 8785) + NFC, pinned at custody `5dc15ff` | It is not reimplemented here |

## Claims made by this release

- A working Claw Street marketplace with offline inference procurement and Credence Core accounting.
- Real x402 merchant challenges, identified as recorded observations.
- Offline merchant compatibility assessment and payment-proposal preparation.
- Verifiable signed agent requests and delivery receipts, within the implemented evidence tiers (Tier 0–2).
- One qualifying provider per class today; the mechanism handles many.
- Credence verifies delivery evidence before recording internal accounting settlement. No provider receives a
  payment in this build. Any future payout requires separate authority and implementation.

## Claims this release does not make

Live t54 paid inference. Trust-line or Verifiable Intent enforcement. Credence-governed XRPL settlement. Mainnet or
RLUSD settlement. Production-ready machine credit or lending. Any proof that exact model weights ran. Trustless
settlement. A busy market. Any exchange, trading, futures or derivatives. Any endorsement by a sponsor or by the UOR
Foundation.

Credence is a Kessai product. Maura also contributes to the UOR Foundation; the Foundation does not endorse Credence.
