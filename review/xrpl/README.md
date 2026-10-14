# review/xrpl

Owner: XRPL review workstream (Claude Code), independent of the spike.

Checkpoint 1R: independent review of the XRPL lending protocol composition and audit of the
spike in `spike/xrpl/` (read only; never modified).

| File | Contents |
| --- | --- |
| `EXPECTATIONS.md` | Phase A: what the protocol requires (Q1 to Q15), from rippled 3.4.1/3.4.0 source and the official docs, written and hashed before any spike output was read |
| `AUDIT.md` | Phase B: audit of the implementer's commit `d4eb874` (E1 claims checked independently; falsification targets A to H; false-positive checklist; latent defects in the unexecuted script) |
| `PLAN-DIFF.md` | Phase C: each relevant build-plan assertion classified against protocol source, with run evidence in a separate column |
| `RESULTS.json` | Machine-readable record keeping protocol expectation, implementer evidence and reviewer conclusion separate for every item |

**Checkpoint 1R verdict (commit `d4eb874`): BLOCKED.** The implementer's evidence covers only the environment gate (E1): a pinned
xrpld 3.4.0 standalone ledger with the lending amendments enabled. Nothing about vault phases,
the Loan Broker, the 2-of-2 LoanSet with the broker's counterparty signature, LoanPay or
redemption has run evidence. Source review finds the architecture likely viable with bounded plan
corrections (see `PLAN-DIFF.md`).

Upstream sources were inspected outside the repository and are not committed.

**Checkpoint 1R-B (audit of implementer commit `44fb089`): PASS WITH CONDITIONS.** The committed
evidence independently supports E3 (closed-ended vault deposit/withdraw phase gating) and E4 (Loan
Broker ownership, vault relationship, cover configuration). The frozen A-K oracle was unchanged
during the run. Conditions and the remaining plan correction (first-loss wording) are in
`AUDIT.md` (1R-B) and `PLAN-DIFF.md` (1R-B update). E5 and later remain untested.
