# Submission validation — 2026-10-25

Base: parent `8bb4a758fec312eddcf21575bb69b84005a320c0`, local web
`4a9a812167f015f98cb4d0458fb3b93981b1d48d`, custody
`5dc15ff6fb097dc5f4a47e72969fae4a296620a5`.
The release commit records the exact updated web pin. Core, signers, Integration, API,
Inference, XRPL adapter, contracts and vectors are unchanged from the parent base.

## Changes

- Local homepage: source/judge links and an explicit distinction between the local working
  backend and the hosted browser simulation.
- Local rendering: initialize the session-bearing app after browser hydration using the
  router's `ClientOnly` boundary. Full refreshes previously produced hydration mismatches on
  Credit, Evidence and Activity; complete browser checks now report zero errors.
- New local presenter/check/record runner: prepares and submits purchases through the UI,
  invokes the existing agent CLI outside the browser, retrieves and acknowledges the response,
  and verifies receipt checks and facility accounting. Only the selected loopback API and
  website are allowed; other browser network requests are blocked.
- Updated public walkthrough selectors and narration for the corrected hosted simulation.
- Submission copy, portrait founder script/cards source, clone instructions and disclosures.

## Completed checks

| Check | Result |
| --- | --- |
| Python tests | 1,630 passed, 30 skipped; one pre-existing Starlette deprecation warning |
| Python lint and format | Pass; 219 files already formatted |
| Python strict typecheck | Pass; 64 source files |
| Local frontend tests | 55 passed, zero failures |
| Local frontend typecheck | Pass |
| Local frontend production build | Pass |
| Headless local browser | Pass: scenarios A and B; charges 2 and 8 micro-tRLUSD; receipt checks pass; held exposure returns to zero; invalid session refused |
| Visible local browser | Same complete flow passes in headed Chromium |
| Response authorization | Retrieval and acknowledgement succeed; journal reaches acknowledged |
| Network and ledger boundary | Local runner reports zero blocked requests and zero browser errors; payment status remains not submitted with null transaction hash |
| Public website | 13 routes load, each with the browser-simulation disclosure; no bare test-unit mislabel or live-payment claim found |
| Public interactions | All three routing strategies, simulated purchase and rejected simulated attack pass |
| Public presenter runner | All eight stages pass, including read-only inspection of the separate historical Testnet transaction |
| Secret scan of base histories | gitleaks 8.21.2: parent 33 commits and frontend 24 commits; no leaks |

The initial sandboxed Python run could not open loopback test sockets. The same tests passed
with loopback access enabled; no application workaround was introduced. Existing Vite
configuration notices and chunk-size warnings are non-failing.

## Hosted frontend

Project `307a6bdc-1e63-4463-bff7-e79d2573943f`, commit
`a7bb600b43a8b7dc4e5ff297723810306861fac6`, published at
https://creds-for-ai.lovable.app. That commit is also present on the frontend repository's
`main` branch through Lovable sync. Its changes are frontend copy and labels only; no hosted
backend, database, provider calls or payment service was added. Lovable reports 16 existing
tests and typecheck passed; the independent live browser checks above verify the served site.

## Limits

- The recording is captioned and silent. Founder narration, founder camera footage and video
  hosting are manual submission steps. The generated release report records the file duration.
- Repository visibility is unchanged. Judges need access to both repositories or an approved
  source archive.
- `/v0/health` has a legacy baseline label; no API contract was altered for this release.
- Historical architecture/test documents retain their original base revisions.
- Core/X2-0R and native XRP payout work remain blocked and separate. No XRPL signing,
  submission, faucet use, provider spend or new ledger transaction occurred in this task.
