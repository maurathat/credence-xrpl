# Public browser simulation narration

These cues describe the hosted website. For the working local backend and its three-minute
recording, use `../DEMO-SCRIPT.md` and `local-demo.mjs`. The live runner prints its exact
current cues from `stages.mjs`.

| Time | View | Narration |
| --- | --- | --- |
| 0:00–0:22 | Logo and Overview | Claw Street, powered by Credence, explores rule-bound procurement for AI agents. This public site is a browser simulation with seeded data: no provider calls and no payments. |
| 0:22–0:52 | Procurement | Five fictional provider offers illustrate price, latency, evidence and residency choices. The strategy buttons select in the browser; Credence Core runs only in the separate local demo. |
| 0:52–1:24 | Credit simulation | The purchase button replays four simulated checks and adds 10.46 tRLUSD to in-browser accounting. No signature is verified, no provider is called and no funds move. tRLUSD is a synthetic test unit here. |
| 1:24–1:50 | Credit and attack | Available balance changes from 362.40 to 351.94; outstanding becomes 148.06. A randomly selected attack case is rejected by the simulation with balances unchanged. These are browser figures, not ledger records. |
| 1:50–2:12 | Receipts | These are seeded example receipts. Tier 1 means gateway-witnessed; Tier 2 means a provider signed what it says it served. Here those tiers are simulated, and receipts are not cryptographically verified. No tier proves exact weights ran. |
| 2:12–2:24 | Activity | The seeded request history illustrates passes and rejections. It is not live provider activity. |
| 2:24–2:50 | Separate Testnet transaction | The explorer displays an existing one-drop Testnet connectivity transaction, validated in ledger 21807427. Its memo explicitly says it was not governed by Credence procurement authorization. It is separate from the marketplace. The runner only reads the explorer. |
| 2:50–3:00 | Logo | Explore this browser simulation, then run the working local signed-purchase demo. Claw Street — Powered by Credence. |

The local demo uses real application signatures with offline mock providers. It verifies
delivery evidence and updates internal accounting; it does not sign or submit XRPL payments.
XRPL marketplace settlement, QuickNode, t54/x402 payments and Ledger hardware are not connected.
