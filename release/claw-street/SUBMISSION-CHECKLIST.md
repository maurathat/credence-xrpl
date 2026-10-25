# Submission checklist

Release candidate: `maurathat/claw-street-release`, code at `9844530` (parents `ab1bd50` and `4c57b91`); web
`4a9a812`.

## Deliverables

| Deliverable | State |
| --- | --- |
| README and startup instructions | Done: `README.md` |
| Three-minute demonstration script and judge walkthrough | Done: `DEMO-SCRIPT.md` |
| Screenshots of the six main views | Done; kept outside Git with the test evidence (see `TEST-SUMMARY.md`) |
| Architecture and integration status | Done: `ARCHITECTURE.md` |
| Reproducible test summary | Done: `TEST-SUMMARY.md` |
| Sponsor integration disclosures | Done: `DISCLOSURES.md` |
| Deployment readiness | Done: `DEPLOYMENT-READINESS.md` (decisions open) |
| Demo dataset and reset/recovery | Done: `DEMO-RESET.md` |
| Testnet proof slot | Reserved, empty: `TESTNET-PROOF.md` |
| Demo video | **Not recorded** |
| Submission description text | **Not written**; draft it from `README.md` and `DISCLOSURES.md` |

## Must resolve before submission

| # | Blocker | Owner |
| --- | --- | --- |
| 1 | **The public site `creds-for-ai.lovable.app` serves the old UI** ("Credence Markets", bare RLUSD, "XRPL connected", "live MVP", "Forwards"). This breaks the claims rules wherever judges can see it | Maura (unpublish, or update `main`) |
| 2 | The Credence parent repository and `maurathat/claw-street-release` are not published (fresh clones of the release need them) | Maura (push authorization) |
| 3 | Choose a deployment: local demo plus video (recommended), or the reviewed public single-host option | Maura |
| 4 | Record the demo video | Maura / presenter |
| 5 | Claims decisions: the recorded x402 challenges name RLUSD as third-party data, against the rule "tRLUSD everywhere, never RLUSD"; and keeping "Providers are paid after verification" when no provider is paid in this build | Maura |
| 6 | Add `release/` to `docs/OWNERSHIP.md` | Maura |

## Known limitations to disclose (not fixed in this release)

- `/v0/health` reports a stale `"baseline": "a3ec0a31…"` (API/UI).
- A second server on the same database fails with a traceback, not a clean message (Integration).
- No buyer price cap on the purchase path (CR-2), no exposure history (CR-5), no live offers (CR-6).

## Not in this release (do not present as working)

- **XRPL payments:** `c8be7aa` on `maurathat/claw-street-integration` is contested.
  - Salvador confirmed findings F1 and F2 on `cdc67b3`.
  - The remediation `bc82eb6` is BLOCKED on D1.
  - Still open: N3, N4 and N5, production custody, Core payment authority and real found-transaction evidence.
- **x402 payments:**
  - Still required: Core request 0009 items C3, C4, C12, C13 and C14, and X1 request 0010 items X2, X5, X6, X9, X10
    and X12.
  - The replay guard is in memory only.
  - The merchant ID is 48-bit, below the 128-bit floor.
  - Facilitator acceptance of a multisigned Payment is unknown.
- **QuickNode:** X1u is pending, and the adapter is incompatible with the newer lookup interface.
- **Public Testnet proof:** the brief hasn't been delivered to Addis-Ababa.
- **Ledger hardware signer, XRPL lending, Verifiable Intent:** not connected.
