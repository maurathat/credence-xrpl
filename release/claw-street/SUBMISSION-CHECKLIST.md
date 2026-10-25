# Submission checklist

Submission branch: `maurathat/claw-street-release`, based on `8bb4a758fec312eddcf21575bb69b84005a320c0`.
The parent Git submodule pins the exact local frontend revision. See `SUBMISSION-VALIDATION.md`
and the delivered release report for final commit IDs and push verification.

## Deliverables

| Deliverable | State |
| --- | --- |
| README and startup instructions | Done: `README.md` |
| Three-minute demonstration script and judge walkthrough | Done: `DEMO-SCRIPT.md` |
| Screenshots of the six main views | Done; kept outside Git with the test evidence (see `TEST-SUMMARY.md`) |
| Architecture and integration status | Done: `ARCHITECTURE.md` |
| Reproducible test summary | Done: `TEST-SUMMARY.md` |
| Sponsor integration disclosures | Done: `DISCLOSURES.md` |
| Deployment readiness | Local backend plus video; separate public browser simulation |
| Demo dataset and reset/recovery | Done: `DEMO-RESET.md` |
| Testnet proof slot | Reserved, empty: `TESTNET-PROOF.md` |
| Demo video | Record with `npm run local:record`; delivered recording status is in the release report |
| Submission description text | Done: `SUBMISSION.md` |
| Founder portrait pitch | Script: `FOUNDER-PITCH.md`; rehearsal page: `founder-pitch.html`; record founder footage |

## Final manual steps

| # | Action | Owner |
| --- | --- | --- |
| 1 | Add your voice to the captioned local demo recording and upload it to the submission's video host | Presenter |
| 2 | Record the founder pitch in 9:16; use the supplied opening and closing cards | Maura |
| 3 | Give judges access to both source repositories, or supply an approved source archive. Visibility is unchanged | Maura |
| 4 | Paste the published website, accessible video URL and source URL into the submission form | Maura |

## Known limitations to disclose (not fixed in this release)

- `/v0/health` reports a stale `"baseline": "a3ec0a31…"` (API/UI).
- A second server on the same database fails with a traceback, not a clean message (Integration).
- No buyer price cap on the purchase path (CR-2), no exposure history (CR-5), no live offers (CR-6).

## Not in this release (do not present as working)

- **Core/X2-0R and native payout implementation:** blocked and separate; no promotion in this release.
- **XRPL marketplace payments, x402 payments, QuickNode, Ledger hardware, XRPL lending and Verifiable Intent:** not connected.
- **Separate Testnet connectivity:** the existing public walkthrough reads a historical transaction. It is independent of marketplace authorization and grants no payment authority.
