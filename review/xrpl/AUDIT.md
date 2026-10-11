# XRPL spike audit (Phase B)

Reviewer: XRPL review workstream (Claude Code). Mode: adversarial, fail closed.
The implementer's work was read only, from a `git archive` export of commit `d4eb874`.
Nothing under `spike/xrpl/` was modified, repaired or rerun. Nothing was sent to the
implementer.

## Sequencing (independence record)

| Step | UTC | Evidence |
| --- | --- | --- |
| `EXPECTATIONS.md` completed and hashed, before any spike file was opened | 2026-10-11T07:18:46Z | sha256 `4ee2eb94b161232474942c02f764f5b7f4d29485fedfd0f2de9c4a681dfd81ee` (unchanged since) |
| Spike branch found and its files first read | after 07:18:46Z | `maurathat/xrpl-credence-codex` |

## What was audited

| Item | Value |
| --- | --- |
| Implementer branch | `maurathat/xrpl-credence-codex` (local; not pushed to `origin`) |
| Commits | `5c2cab1` "Spike XRPL lending protocol composition", `d4eb874` "Fix XRPL amendment-state validation" |
| Paths changed vs `checkpoint-0` | 18 files, all under `spike/xrpl/` (`git diff --stat 810c787 d4eb874`) |
| Scope actually executed | **E1 only** (server start, amendment state, two `ledger_accept` closes). No protocol transaction was ever submitted. |
| E2 to E9 | Not run. `RESULTS.json` labels them `NOT_APPLICABLE` |

The audit therefore has two parts: (1) checking the E1 claims against independent evidence,
and (2) a static review of the never-executed experiment script for defects that would produce
false results once it runs. Part 2 is recorded for the next run. Nothing was fixed.

---

## 1. E1 audit: claims checked independently

| E1 claim | Reviewer check | Outcome |
| --- | --- | --- |
| Debian package `xrpld_3.4.0-1_amd64.deb` sha256 `4dc40a21…8339` | Downloaded from the same URL and hashed | **Matches** |
| Installed binary sha256 `9b4d3dd2…c417` | Extracted `usr/bin/xrpld` from that package and hashed | **Matches** |
| Source commit `4a4fded2…a7d17` = tag `3.4.0` | `git rev-parse 3.4.0` in the upstream repo | **Matches.** `3.4.0` is a published, non-prerelease GitHub release (2026-09-17) |
| `xrpld --version` reports 3.4.0 / that commit | Not re-executed (amd64 binary). The commit string is not stored as plain text in the binary | Not independently reproduced; the package and binary digests above are |
| The 8 amendment IDs in `config/xrpld.cfg` are the real IDs | Recomputed SHA-512Half(name) for each | **All 8 match** |
| The original gate's 8 IDs were wrong | Recomputed against `evidence/unexpected-result.json` | **All 8 wrong.** Each shares only the first 4 hex digits with the real ID; the rest was not derived from the name. The root-cause claim is correct |
| All 8 required amendments enabled, before and after closes | Parsed `feature-raw-e1-{before,after}.json` independently | **Confirmed**: 106 amendments, 9 enabled (the 8 plus `fixRemoveNFTokenAutoTrustLine`) |
| Raw capture digests `3f9caa62…cd8` (before = after) and `73e1a325…f1e7` (original) | `shasum -a 256` | **Match** |
| 6 regression tests pass | Ran `pytest` on the exported tests | **6 passed** |
| Two `ledger_accept` closes, validated 2 → 3 → 4 | Read `evidence/e1-validation.json` | Consistent (close times 845016430 → 845017070 → 845017071) |
| No secrets committed | gitleaks v8.21.2 with the repo config, over the exported tree and over all 4 commits of the branch history | **No leaks found** |

**Independent corroboration of reviewer expectations (from the implementer's raw data):**
- EXPECTATIONS Q2 predicted that a fresh `--start` genesis enables exactly the supported amendments
  that are voted Up: `DefaultYes` plus `[amendments]` entries. In 3.4.0 the only `DefaultYes`
  amendments are `fixCleanup3_1_3` and `fixRemoveNFTokenAutoTrustLine`. The observed 9 enabled
  amendments are exactly that union. **Corroborated.**
- EXPECTATIONS Q4 predicted `close_time = max(round(wall clock), parent + 1 s)`. Ledger 3 closed at
  a 10 s boundary (845017070) and ledger 4, accepted immediately afterwards, at exactly **parent +
  1 s** (845017071). **Corroborated.** Rapid scripted closes advance ledger time by 1 s each,
  independent of wall clock.
- The implementer's choice of eight amendments matches the reviewer's independent Q2 list
  (4 gating amendments + 4 cleanup fixes).

**E1 caveats (overstatement and reproducibility):**
- **A1. Not a fresh run.** The Checkpoint 1 report said the minimum next experiment was to run
  the corrected validator "against a fresh standalone ledger created from the identical pinned
  image". Checkpoint 1A instead reused the already running server: ledger 2 has the same hash
  `E27EDC09…4979` in `server-info.json` (stopped run) and `e1-validation.json` (recovery run), with
  ledger age 190 s then 636 s. A fresh start was therefore not repeated.
- **A2. `ledger_close_reproducible: true` is a hardcoded literal** in `validate_environment.py`,
  not a measurement. The evidence shows two consecutive closes in one process, not
  reproducibility across starts.
- **A3. Data path mismatch.** `ENVIRONMENT.md` says the named volume is mounted at
  `/var/lib/xrpl`, while `xrpld.cfg` writes the node store and `wallet.db` to `/var/lib/xrpld/…`.
  The "new empty named data volume" therefore says nothing about where state was actually
  stored. No `docker run`/compose invocation is checked in, so the run cannot be reproduced
  exactly from the repo.
- **A4. Amendment state was read only through the `feature` RPC** (the amendment-table view).
  The on-ledger `Amendments` object (`ledger_entry`) was not captured. For a `--start` genesis
  these should agree, but the plan asks for the enabled list on the ledger.
- **A5. Network parity.** Only 9 of 106 amendments are enabled. Other fixes that public networks
  may have enabled are off, so behaviour can differ from Devnet. Not a defect for a standalone
  spike; it matters if the demo moves to Devnet.
- **A6. Release choice.** 3.4.0 was pinned. 3.4.1 (2026-10-10T00:04Z) existed before the run.
  The lending and vault transactors are byte-identical between the two. 3.4.1 adds `fixBatchV1_2`
  and overflow guards in `TokenHelpers.cpp` on XRP and MPT paths. Pinning 3.4.0 is acceptable.
  3.4.0 is also the first release where `LendingProtocolV1_1` is supported (EXPECTATIONS Q1).

**E1 reviewer conclusion: PASS as scoped** (pinned release, standalone, network ID, required
amendments enabled, ledgers close). Caveats A1 to A4 limit the reproducibility claim.

---

## 2. Falsification targets A to H

No protocol transaction exists, so no claim A to H was made, and none can be confirmed or falsified.

| Target | Implementer claim | Reviewer finding |
| --- | --- | --- |
| A. Vault phase | none (E3 not run) | NOT TESTED |
| B. Loan creation | none (E6/E7 not run) | NOT TESTED |
| C. Borrower 2-of-2 authorization | none (E5/E6 not run) | NOT TESTED |
| D. Borrower / broker authorization separation | none | NOT TESTED. The required amendment `fixCleanup3_4_0` *is* enabled (E1), so the role-separated prefix is active |
| E. Principal disbursement | none | NOT TESTED |
| F. LoanPay effect | none | NOT TESTED |
| G. Early-payment behaviour | none | NOT TESTED |
| H. Redemption behaviour | none | NOT TESTED |

## 3. False-positive checklist against the evidence and the unexecuted script

| Hazard | Executed evidence | Unexecuted `run_experiments.py` (static review) |
| --- | --- | --- |
| Master key signing instead of multisign | n/a | **LoanSet: avoided.** Borrower side uses `multisign()` with the two signer wallets (`loan_set`). **LoanPay: hazard present.** Every LoanPay is `runner.single(..., borrower)`, i.e. signed by the borrower's **master key**. The script would never test a 2-of-2 LoanPay (EXPECTATIONS Q11), although the plan requires a co-signed LoanPay |
| Wrong account treated as borrower | n/a | Avoided: `Account = borrower`, `Counterparty = loan_broker`. (Upstream shows that omitting `Counterparty` with `Account = broker owner` makes the broker the borrower; the script does not do that.) But no state check asserts `Loan.Borrower == borrower` |
| Stale ledger state | n/a | State is read with `ledger_index="validated"` after a close. Acceptable |
| Amendment configuration assumptions | Original false negative (wrong IDs) found and fixed | Gate now strict and correct |
| tesSUCCESS without the claimed state | n/a | **Hazard present.** Every experiment's `"result": "PASS"` and `"overall": "PASS"` are literals written whenever no exception occurred. Ledger snapshots are recorded but never asserted (no check of principal delta, `DebtTotal`, `AssetsAvailable`, `Loan.Borrower`, `PaymentRemaining`). This is exactly "PASS because tesSUCCESS" |
| Standalone time vs wall clock | E1 data shows +1 s/close | `wait_until` closes ledgers until `close_time > target`. Correct for the parent-close clock. Phase dates are derived from ledger time, not wall time. Acceptable |
| Test setup bypassing the authorization question | n/a | Borrower master key stays enabled and the same wallet signs `SignerListSet` (the owner role). Acceptable, but combined with the LoanPay hazard above, E8 would not exercise 2-of-2 |
| Broker signature over a different representation | n/a | Checked offline: xrpl-py 5.2.0 `compute_signature` uses prefix `0x43505400` (= rippled `CPT`), and the bytes the broker signs (pre-multisign `filled`) equal the counterparty signing bytes of the final multisigned tx (`SigningPubKey` is `""` in both). **No defect** |
| Issued-currency vs micro-unit confusion | n/a | Amounts are decimal strings ("2000", "1100"). No micro-unit mapping exists. `loan_a_scheduled_payment` pays the **unrounded** `PeriodicPayment` string. With interest 0 that is exact, but with non-zero interest it can fall below `roundUp(PeriodicPayment, LoanScale)` and return `tecINSUFFICIENT_PAYMENT` |
| Test identities or secrets committed | gitleaks clean (tree and history). The genesis seed is read from the environment, actors are generated in memory | Evidence sanitization strips `SigningPubKey`, `TxnSignature`, `Signers`, `CounterpartySignature` (see S2) |
| Result classification overstating evidence | **Present.** `RESULTS.json` `"overall": "PASS"` with 8 of 9 experiments unexecuted. Unexecuted experiments labelled `NOT_APPLICABLE` instead of not run | See above |

### Latent script defects that would produce a false negative

- **L1. Altered-terms test cannot behave as coded.** `loan_set_altered_terms` expects engine result
  `tefBAD_SIGNATURE`. A counterparty signature over different bytes fails `STTx::checkSign`
  inside `checkValidity`, and the `submit` RPC answers `error: invalidTransaction`,
  `fails local checks: Counterparty: …` with no engine result (rippled
  `Submit.cpp:104-111`, `STTx.cpp:271-281`). xrpl-py 5.2.0 `submit` then **raises
  `XRPLRequestFailureException`** (`xrpl/asyncio/transaction/main.py:220-223`). That is not the
  script's `UnexpectedLedgerResult`, so the run would crash with no structured record. A correct
  protocol rejection would surface as a crash on the E6 composition question. (This is the
  outcome EXPECTATIONS Q10 predicts.)
- **L2.** `loan_a_early_partial_rejected` sets `tfLoanOverpayment` on a sub-installment amount.
  The expected `tecINSUFFICIENT_PAYMENT` is right per source, but the test mixes two variables
  (flag and amount). A pure sub-installment test should omit the flag.

---

## 4. Security and reproducibility findings

- **S1. Secrets:** none found (gitleaks, repo config, tree and full branch history). The genesis
  seed is taken from an environment variable. Actor keys are generated per run and not persisted.
- **S2. Evidence design removes the audit trail for authorization.** `sanitize()` deletes
  `SigningPubKey`, `TxnSignature`, `Signers` and `CounterpartySignature` from every recorded
  transaction and metadata. Public keys and signatures are not secrets (the repo's gitleaks rules
  target seeds and private keys). Without them a reviewer cannot tell from committed evidence
  whether a transaction was multisigned or master-signed (targets C, D). The standalone ledger is
  ephemeral, so the hashes cannot be looked up later either. At minimum, keep the signed blob or
  `Signers[].Account` with `SigningPubKey`, and the presence of a top-level `TxnSignature`.
- **S3.** Inline `# gitleaks:allow` on a public amendment ID. Harmless, but it suppresses the scanner
  on that line.
- **S4.** No checked-in container invocation (port, volume, name). See A3.
- **S5.** The spike pins its own image under `spike/xrpl/config/`. `infra/rippled/` (owned by XRPL
  Integration) still has the Checkpoint 0 placeholder with `--standalone` and no `--start`. Per
  EXPECTATIONS Q2, that invocation would build a genesis with **no** amendments unless
  `[features]` presets are used. Production infra must not copy the compose file as is.

## 5. Audit conclusion

The implementer's evidence demonstrates only the environment gate (E1): a pinned 3.4.0
standalone server with the eight required amendments enabled, closing ledgers. None of
the architectural claims (vault phases, broker and cover, 2-of-2 LoanSet composition with the
broker counterparty signature, disbursement, LoanPay, redemption) has run evidence.
The implementer's `RESULTS.json` `"overall": "PASS"` overstates this. Its own `README.md`
and `REPORT.md` describe the scope correctly as E1 only.
