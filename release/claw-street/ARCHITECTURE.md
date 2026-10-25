# Architecture and integration status

Release candidate: `maurathat/claw-street-release` at `9844530f7832d3f920a2219f27f26cbdc2a9f0fc`.
It merges `ab1bd50` (accepted I2b `726f643` plus web `4a9a812`) with the offline x402 adapter `4c57b91`.

## Components in this build

```
 browser (web/, React + TanStack Start)            agent CLI (credence/agent)
   │  session token only, no keys                     │  holds the agent's Ed25519 key; verifies, then signs
   ▼                                                  ▼
 Credence API (credence/api, FastAPI, loopback only) ◀──── signed purchase / response authorization
   │
   ▼
 IntegrationService (credence/integration): journal, preparations, single-process ownership, key revocation
   │                                  │
   ▼                                  ▼
 Credence Core (credence/core)      Inference gateway (credence/inference)
  policy engine · verifier ·         market selection · providers
  reservation ledger ·               (this build: offline mocks of Nebius Token Factory and local Qwen)
  settlement coordinator ·
  signatures · Kessai serializer (third_party/custody: JCS (RFC 8785) + NFC)
   │
   ▼
 SQLite database owned by the API process (<demo-dir>/server/credence-i2a.sqlite3)

 x402 adapter (credence/x402): offline and non-paying. Parses recorded merchant challenges, assesses compatibility,
 prepares payment proposals. Not wired into the purchase path, and not authority to pay.
```

**A purchase, end to end:**
1. The agent's request is evaluated by Core's policy engine (dry run).
2. The service prepares the purchase.
3. The agent verifies and signs it outside the browser.
4. Core reserves the quote's maximum cost against the facility.
5. The gateway runs the selected provider (a mock here) and records the delivery receipt.
6. Core's verifier checks the receipt.
7. The settlement coordinator replaces the hold with the verified charge in the same database transaction.
8. The agent authorizes retrieval and acknowledgement.

## Evidence tiers

| Tier | Meaning | In this build |
| --- | --- | --- |
| 0 · Requested | The purchase request names the execution profile; nothing about delivery is evidenced | Implemented |
| 1 · Witnessed | Credence's gateway witnessed the provider's response and signed the receipt; the provider signs nothing | Implemented (Scenario A) |
| 2 · Bound and signed | The provider signed what it says it served; the gateway also signs the receipt | Implemented (Scenario B, with a TEST provider key) |
| 3 · Measured | Measured execution evidence | Roadmap |

No tier proves that the exact model weights ran.

## Integration status

| Integration | Status in this release | Where the work is |
| --- | --- | --- |
| Credence Core, Inference gateway, IntegrationService, API | **In this release**, independently accepted (I2b `726f643`) | — |
| Claw Street web UI | **In this release** (web `4a9a812`, on `creds-for-ai` branch `release/claw-street-ui`) | — |
| x402 adapter (t54 merchants) | **In this release**, offline and non-paying: recorded challenges, compatibility assessment, proposal preparation | Accepted: Skopje (security, `a8c1e29`), Ulaanbaatar (economic docs, `4c57b91`) |
| Nebius Token Factory | Offline mock in the demo. Recorded live observations from 2026-10-19 are shown as recorded evidence | Live calls exist in the inference code, but are off in this build |
| Local Qwen (Q8_0 via ollama) | Offline mock in the demo; recorded live observation shown as recorded evidence | Opt-in real-model tests (`CREDENCE_LOCAL_QWEN=1`) |
| XRPL payments (2-of-2 multisign, journal, payment assurance) | **Not in this release. Not connected.** | `maurathat/claw-street-integration` `c8be7aa` (contested review; X1k remediation `bc82eb6` blocked on D1) |
| QuickNode XRPL reads | **Not connected** | Da Nang branch, X1u pending; incompatible with the newer lookup interface |
| XRPL lending (Vault, LoanSet, LoanPay) | **Not connected** | Spike only |
| Ledger hardware signer | **Not connected** (the Credit page shows the intended authority split as "planned") | Owner TBD |
| Verifiable Intent | **Not connected** | — |
| Public XRPL Testnet proof transaction | **Not done.** A separate slot is reserved: [`TESTNET-PROOF.md`](TESTNET-PROOF.md) | Addis-Ababa (planning brief not yet delivered) |
