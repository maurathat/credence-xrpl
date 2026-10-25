# Claw Street live demo (Playwright)

A guided, three-minute demonstration of the public Claw Street site, <https://creds-for-ai.lovable.app>, ending on
the XRPL Testnet connectivity proof
[`D26C5DDC…76DE79`](https://testnet.xrpl.org/transactions/D26C5DDC6F51877D745462BD7A6C8C8FE24972137637E2B62D1E269D0F76DE79).

It drives the real site. Every selector and expected value was taken from the live pages with Playwright on
2026-10-25, and `npm run check` re-verifies them before you present.

**It is read-only.** A network guard (`guard.mjs`) runs inside the browser:
- It allows only GET/HEAD/OPTIONS to four hosts: the site, Google Fonts and the Testnet explorer.
- It allows WebSockets only to the public XRPL Testnet nodes.
- It drops any XRPL `submit`, `sign`, `sign_for`, `wallet_propose` or other state-changing command.

`npm run selftest` proves this. The only things the demo clicks are the Marketplace strategy buttons, the Credit
page's **Simulate** and **Reset** buttons (a simulation in the browser, with no request sent), the explorer's
**Detailed** tab, and Lovable's badge **Dismiss** button.

## What the audience sees, and what it is

| Stage | Page | What is real |
| --- | --- | --- |
| 1 Overview | `/` then `/overview` | Prototype dashboard with seeded demo data (labelled on the page) |
| 2 Marketplace | `/market` | Seeded offers; the routing strategy re-selects the best offer in the browser |
| 3 Agent purchase | `/credit` → **Simulate inference purchase** | A client-side simulation: four checks, facility drawn +10.46 tRLUSD, labelled "Credence accounting — not an on-chain payment". No provider is called and nothing is paid |
| 4 Credit & payments | `/credit` | Facility panel 362.40 → 351.94 available, utilization 27.5% → 29.6%; **Simulate attack** (the site picks one of six cases at random) is rejected with no draw. The terminal prints the line for the case shown |
| 5 Evidence, activity | `/receipts`, `/overview` | Seeded receipts with evidence tiers; seeded request history with PASS/REJECT |
| 6 XRPL Testnet | testnet.xrpl.org | A **real**, validated Testnet Payment of 1 drop (ledger 21807427), memo "XRPL connectivity proof transaction. Not governed by Credence procurement authorization." It is not a Credence-governed marketplace settlement |
| 7 Close | `/` | The animated Claw Street logo |

tRLUSD is a test token. The site's purchase flow is simulated, and it shows no Nebius or Qwen provider.

## Setup (once)

```bash
cd release/claw-street/live-demo
npm ci                         # installs the pinned Playwright 1.60.0
npx playwright install chromium   # skip if Chromium for Playwright 1.60 is already installed
npm run selftest               # proves the network guard blocks writes (expect 5 PASS)
npm run check                  # verifies every stage against the live site (expect "All stage checks passed.")
```

Node 20.11 or newer.

## Run

| Command | Use |
| --- | --- |
| `npm run present` | **Presenter mode.** A maximized browser window; each stage animates, then pauses. Narration cues print in the terminal. Keys (in the terminal): **Enter / Space / →** next, **r** replay the stage, **b / ←** previous stage, **q** quit |
| `npm run rehearse` | Same browser, advancing on a timer (3:00 total), for practising the narration |
| `npm run record` | Headless backup recording with on-screen captions: `output/record-<time>/claw-street-demo.webm` plus `run.json` (timings, checks, guard log) |
| `npm run check` | Fast headless verification of every stage, with one screenshot per stage in `output/check-<time>/`. Exits 1 if anything is missing |

Options (after `--`, for example `npm run present -- --fullscreen`):

| Option | Effect |
| --- | --- |
| `--from <stage>` | Start at `overview`, `marketplace`, `agent-purchase`, `credit`, `evidence`, `activity`, `xrpl-testnet` or `close` |
| `--fullscreen` | Presenter window in fullscreen instead of maximized |
| `--captions` / `--no-captions` | On-screen narration captions (on by default for `record` only) |
| `--no-highlight` | Don't outline the element being discussed |
| `--slow <factor>` | Stretch the on-screen motion, for example `1.3` |
| `--keep-badge` | Keep Lovable's "Edit with Lovable" badge (dismissed by default, because it covers the Credit page's Reset button) |
| `--headless` | Run `present` or `rehearse` without a window (testing) |

## Files

| File | Purpose |
| --- | --- |
| `stages.mjs` | The seven stages: actions, expected text, timings, narration cues (the source of truth) |
| `demo.mjs` | Runner for the four modes |
| `guard.mjs` | Read-only network guard |
| `guard-selftest.mjs` | Proves the guard blocks writes |
| `NARRATION.md` | Script and cues with timings |
| `TROUBLESHOOTING.md` | What to do when something goes wrong |
